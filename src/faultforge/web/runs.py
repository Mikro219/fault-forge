"""One worker thread and one run at a time, with every public limit (docs/PLAN.md V3.5).

An unexpected error becomes a structured result for that run. Logs carry fixed words and an
exception's type at most, because an exception's text can quote a target, a model or a header.
"""

import dataclasses
import logging
import os
import secrets
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from faultforge import fix_agent, sandbox
from faultforge.fix_agent import ApiKey, AttemptRecord, Proposal, TokenBudget
from faultforge.lifecycle import RunOptions
from faultforge.models import Report
from faultforge.sandbox import SandboxResult, TargetSpec
from faultforge.web import replays as replays_module, viewmodel
from faultforge.web.budget import BudgetLedger, LedgerBudget
from faultforge.web.limits import KeyedWindows, SlidingWindow
from faultforge.web.replays import ReplayStore
from faultforge.web.settings import AGENT_MODEL, AGENT_PRICES, AGENT_REASONING_EFFORT, WebSettings
from faultforge.web.targets import FixedTarget
from faultforge.web.viewmodel import AttemptView, EventView, FixView, ReplayView, RunStateView, RunView, StatusView

logger = logging.getLogger("faultforge.web")

MAX_EVENTS = 64
HEALTH_POLICY = dataclasses.replace(sandbox.DEFAULT_POLICY, docker_timeout_s=5.0)
MIN_DISK_FREE_MB = 512
MIN_MEMORY_AVAILABLE_MB = 256
# Room for the report and earlier attempts in a prompt, on top of the fixed source files.
PROMPT_OVERHEAD_BYTES = 12 * 1024

Stage = Literal["queued", "running", "done", "failed", "fix_queued", "fixing"]


class Refused(Exception):
    """A request the limits turned away. Every field is fixed text, safe to return."""

    def __init__(self, status: int, reason: str, message: str, retry_after: float | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.message = message
        self.retry_after = retry_after


class _Stopping(Exception):
    pass


@dataclass
class RunRecord:
    id: str
    target: FixedTarget
    client: str
    created_at: float
    project_dir: Path
    stage: Stage = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    events: list[EventView] = field(default_factory=list)
    result: RunView | None = None
    # Kept for the fix loop's baseline. Never returned: the page only sees views.
    report: Report | None = None
    code_sha256: str | None = None
    fix_attempts: list[AttemptView] = field(default_factory=list)
    fix_status: Literal["queued", "running", "verified", "not_verified", "failed"] | None = None
    fix_stop_reason: str | None = None


@dataclass(frozen=True)
class _Job:
    kind: Literal["run", "fix"]
    record: RunRecord


def probe_vm(data_dir: Path) -> viewmodel.VmView:
    """Facts about the machine, with no names or paths in them."""
    try:
        containers = sandbox.count_labelled(HEALTH_POLICY)
    except sandbox.SandboxError:
        containers = None
    try:
        load: float | None = os.getloadavg()[0]
    except OSError:
        load = None
    memory = _memory_available_mb()
    try:
        disk: int | None = shutil.disk_usage(data_dir).free // (1024 * 1024)
    except OSError:
        disk = None
    healthy = (
        containers is not None
        and (memory is None or memory >= MIN_MEMORY_AVAILABLE_MB)
        and (disk is None or disk >= MIN_DISK_FREE_MB)
    )
    return viewmodel.VmView(
        healthy=healthy, docker_reachable=containers is not None, load_1m=load,
        memory_available_mb=memory, disk_free_mb=disk, containers_left=containers,
    )


def _memory_available_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


class RunManager:
    def __init__(
        self,
        settings: WebSettings,
        targets: Mapping[str, FixedTarget],
        *,
        api_key: ApiKey | None = None,
        run_sandboxed: Callable[..., SandboxResult] = sandbox.run_sandboxed,
        fix_loop: Callable[..., fix_agent.LoopResult] = fix_agent.run_fix_loop,
        transport: httpx.BaseTransport | None = None,
        reap: Callable[[], int] = sandbox.reap_stale,
        vm_probe: Callable[[Path], viewmodel.VmView] = probe_vm,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.targets = targets
        self._api_key = api_key
        self._run_sandboxed = run_sandboxed
        self._fix_loop = fix_loop
        self._transport = transport
        self._reap = reap
        self._vm_probe = vm_probe
        self._clock = clock

        self.projects_dir = settings.data_dir / "projects"
        self.replays = ReplayStore(settings.data_dir / "replays", settings.replays_per_kind)
        self.ledger = BudgetLedger(
            settings.data_dir / "budget.json",
            daily_usd_cap=settings.daily_usd_cap, total_usd_cap=settings.total_usd_cap,
            daily_token_cap=settings.daily_token_cap, clock=clock,
        )

        self._condition = threading.Condition()
        self._queue: deque[_Job] = deque()
        self._current: _Job | None = None
        self._records: dict[str, RunRecord] = {}
        self._cooldowns: dict[str, float] = {}
        self._runs_window = SlidingWindow(settings.runs_per_hour, 3600.0)
        self._run_ip_windows = KeyedWindows(settings.runs_per_hour_per_ip, 3600.0)
        self._fix_ip_windows = KeyedWindows(settings.fixes_per_hour_per_ip, 3600.0)
        self._fix_ip_day_windows = KeyedWindows(settings.fixes_per_day_per_ip, 86400.0)
        self._fix_day_window = SlidingWindow(settings.fixes_per_day, 86400.0)
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._next_maintenance = 0.0
        self._vm_lock = threading.Lock()
        self._vm_cached: tuple[float, viewmodel.VmView] | None = None

    # Lifecycle

    def start(self) -> None:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.replays.load()
        self._maintain()
        self._thread = threading.Thread(target=self._work, name="ff-web-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """New work is refused, queued runs end as structured failures, and the current run gets
        time to finish and remove its containers."""
        with self._condition:
            self._stopping = True
            waiting = list(self._queue)
            self._queue.clear()
            for job in waiting:
                self._end_unstarted(job)
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=self.settings.shutdown_timeout_s)
            if self._thread.is_alive():
                logger.warning("the worker was still busy at shutdown; the reaper removes what it leaves")

    def worker_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _end_unstarted(self, job: _Job) -> None:
        record = job.record
        if job.kind == "run":
            record.result = viewmodel.internal_failure_view(record.target, "shutdown")
            record.stage = "failed"
        else:
            record.fix_status = "failed"
            record.fix_stop_reason = "shutdown"
            record.stage = "done"
        record.finished_at = self._clock()
        self._event(record, "shutdown")

    # Submitting

    def submit_run(self, target_id: str, client: str) -> RunRecord:
        target = self.targets.get(target_id)
        if target is None:
            raise Refused(422, "unknown_target", "That isn't one of the fixed targets.")
        now = self._clock()
        with self._condition:
            self._refuse_if_stopping()
            if any(job.record.client == client for job in self._active_jobs()):
                raise Refused(409, "already_running", "You already have a run queued or running.")
            wait = self._cooldowns.get(client, 0.0) - now
            if wait > 0:
                raise Refused(429, "cooldown", "Please wait before starting another run.", wait)
            wait = self._run_ip_windows.retry_after(client, now)
            if wait > 0:
                raise Refused(429, "run_ip_limit", "You've reached the hourly limit on runs. Replays still work.", wait)
            wait = self._runs_window.retry_after(now)
            if wait > 0:
                raise Refused(429, "hourly_limit", "The hourly run limit was reached. Replays still work.", wait)
            self._refuse_if_queue_full()
            run_id = f"r_{secrets.token_hex(8)}"
            record = RunRecord(
                id=run_id, target=target, client=client, created_at=now, project_dir=self.projects_dir / run_id,
            )
            self._event(record, "queued")
            self._records[run_id] = record
            self._cooldowns = {key: until for key, until in self._cooldowns.items() if until > now}
            self._cooldowns[client] = now + self.settings.cooldown_s
            self._runs_window.hit(now)
            self._run_ip_windows.hit(client, now)
            self._queue.append(_Job("run", record))
            self._evict_records()
            self._condition.notify_all()
            return record

    def submit_fix(self, run_id: str, client: str) -> RunRecord:
        now = self._clock()
        with self._condition:
            self._refuse_if_stopping()
            disabled = self._agent_disabled_reason()
            if disabled is not None:
                reason = "agent_disabled" if disabled in ("not_enabled", "not_configured") else "budget_exhausted"
                message = (
                    "The fix agent isn't enabled on this server. Replays still work."
                    if reason == "agent_disabled" else "The fix agent's budget is used up. Replays still work."
                )
                raise Refused(503, reason, message)
            record = self._records.get(run_id)
            if record is None:
                raise Refused(404, "not_found", "There's no such run.")
            if record.fix_status is not None:
                raise Refused(409, "fix_already_started", "A fix was already started for this run.")
            if not self._can_fix(record):
                raise Refused(409, "not_fixable", "Only a finished ticket-api run with a proven failure can be fixed.")
            limit = self._fix_limit(client, now)
            if limit is not None:
                reason, message, wait = limit
                raise Refused(429, reason, message, wait)
            self._refuse_if_queue_full()
            self._fix_ip_windows.hit(client, now)
            self._fix_ip_day_windows.hit(client, now)
            self._fix_day_window.hit(now)
            record.stage = "fix_queued"
            record.fix_status = "queued"
            self._event(record, "fix_queued")
            self._queue.append(_Job("fix", record))
            self._condition.notify_all()
            return record

    def _fix_limit(self, client: str, now: float) -> tuple[str, str, float] | None:
        """The fix limit that would refuse this visitor now. One place, so that what the page is
        offered and what submit_fix accepts can't disagree."""
        wait = self._fix_ip_windows.retry_after(client, now)
        if wait > 0:
            return "fix_ip_limit", "You've reached the hourly limit on fixes.", wait
        wait = self._fix_ip_day_windows.retry_after(client, now)
        if wait > 0:
            return "fix_ip_daily_limit", "You've reached today's limit on fixes.", wait
        wait = self._fix_day_window.retry_after(now)
        if wait > 0:
            return "fix_daily_limit", "Today's limit on fixes was reached.", wait
        return None

    def _refuse_if_stopping(self) -> None:
        if self._stopping or not self.worker_alive():
            raise Refused(503, "shutting_down", "The server isn't accepting new work right now.")

    def _refuse_if_queue_full(self) -> None:
        if len(self._queue) >= self.settings.queue_limit:
            raise Refused(429, "busy", "The queue is full. Try again soon, or watch a replay.", 30.0)

    def _active_jobs(self) -> list[_Job]:
        return [*self._queue, *([self._current] if self._current else [])]

    def _can_fix(self, record: RunRecord) -> bool:
        return (
            record.target.agent is not None
            and record.stage == "done"
            and record.report is not None
            and record.report.verdict == "FAIL"
            and record.project_dir.is_dir()
        )

    def _evict_records(self) -> None:
        active = {job.record.id for job in self._active_jobs()}
        for run_id in list(self._records):
            if len(self._records) <= self.settings.records_limit:
                return
            if run_id not in active:
                del self._records[run_id]

    # The worker

    def _work(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    timeout = self._next_maintenance - self._clock()
                    if timeout <= 0:
                        break
                    self._condition.wait(min(timeout, 1.0))
                if self._stopping and not self._queue:
                    return
                job = self._queue.popleft() if self._queue else None
                self._current = job
            if job is None:
                self._maintain()
                continue
            try:
                self._run_job(job)
            finally:
                with self._condition:
                    self._current = None
            self._maintain()

    def _maintain(self) -> None:
        """The reaper's timer and disk retention. A failure here must never stop the worker."""
        self._next_maintenance = self._clock() + self.settings.maintenance_interval_s
        try:
            self._reap()
        except Exception as error:
            logger.warning("the reaper failed (%s)", type(error).__name__)
        try:
            with self._condition:
                active = {job.record.project_dir.name for job in self._active_jobs()}
            replays_module.apply_retention(
                self.projects_dir, now=self._clock(), max_age_s=self.settings.retention_age_s,
                keep=self.settings.retention_keep, active=active,
            )
        except Exception as error:
            logger.warning("retention failed (%s)", type(error).__name__)

    def _run_job(self, job: _Job) -> None:
        record = job.record
        try:
            if job.kind == "run":
                self._execute_run(record)
            else:
                self._execute_fix(record)
        except _Stopping:
            self._finish_fix(record, "failed", "shutdown")
        except Exception as error:
            logger.error("a %s failed unexpectedly (%s)", job.kind, type(error).__name__)
            if job.kind == "run":
                self._finish_run(record, viewmodel.internal_failure_view(record.target, "internal_error"))
            else:
                self._finish_fix(record, "failed", "internal_error")

    def _execute_run(self, record: RunRecord) -> None:
        with self._condition:
            record.stage = "running"
            record.started_at = self._clock()
            self._event(record, "sandbox_started")
        target = record.target
        result = self._run_sandboxed(target.spec, record.project_dir, target.scenario_id, RunOptions())
        view = viewmodel.build_run_view(result, target, record.project_dir)
        with self._condition:
            if result.status == "completed":
                record.report = result.report
                record.code_sha256 = result.deployed_code_sha256
        self._finish_run(record, view)

    def _finish_run(self, record: RunRecord, view: RunView) -> None:
        with self._condition:
            record.result = view
            record.finished_at = self._clock()
            record.stage = "done" if view.status == "completed" else "failed"
            self._event(record, "finished" if view.status == "completed" else "failed")
        self._store_replay(record)

    def _execute_fix(self, record: RunRecord) -> None:
        agent = record.target.agent
        with self._condition:
            record.stage = "fixing"
            record.fix_status = "running"
            self._event(record, "fix_started")
        latest: dict[str, SandboxResult] = {}

        def sandboxed(spec: TargetSpec, project_dir: Path, scenario_id: str, options: RunOptions) -> SandboxResult:
            result = self._run_sandboxed(spec, project_dir, scenario_id, options)
            latest["result"] = result
            return result

        def on_attempt(attempt: AttemptRecord, proposal: Proposal) -> None:
            result = latest.pop("result", None)
            candidate = (
                viewmodel.build_run_view(result, record.target, record.project_dir) if result is not None else None
            )
            view = viewmodel.build_attempt_view(attempt, proposal, agent.app_source, candidate)
            with self._condition:
                record.fix_attempts.append(view)
                self._event(record, "attempt_passed" if attempt.fix_verified else "attempt_failed", attempt.attempt)
                if self._stopping:
                    raise _Stopping

        loop = self._fix_loop(
            model=AGENT_MODEL,
            api_key=self._api_key,
            budget=LedgerBudget(self.ledger, AGENT_PRICES),
            app_source=agent.app_source,
            support_sources=agent.support_sources,
            business_rules=agent.business_rules,
            baseline=record.report,
            baseline_code_sha256=record.code_sha256,
            support_dir=agent.support_dir,
            config_path=agent.config_path,
            project_dir=record.project_dir,
            scenario_id=record.target.scenario_id,
            run_sandboxed=sandboxed,
            options=RunOptions(),
            max_tokens=self.settings.agent_max_tokens,
            reasoning_effort=AGENT_REASONING_EFFORT,
            transport=self._transport,
            on_attempt=on_attempt,
        )
        self._finish_fix(record, "verified" if loop.success else "not_verified", loop.stop_reason)

    def _finish_fix(self, record: RunRecord, status: Literal["verified", "not_verified", "failed"], stop: str) -> None:
        with self._condition:
            record.fix_status = status
            record.fix_stop_reason = stop
            record.stage = "done"
            self._event(record, "fix_finished")
        self._store_replay(record)

    def _store_replay(self, record: RunRecord) -> None:
        """Only from a run that completed in this process. A storage failure doesn't fail the run."""
        if record.result is None or record.result.status != "completed" or record.finished_at is None:
            return
        try:
            self.replays.save(ReplayView(
                id=record.id, recorded_at=viewmodel.timestamp(record.finished_at), target=record.target.id,
                result=record.result, fix=self._fix_view(record),
            ))
        except Exception as error:
            logger.warning("couldn't store a replay (%s)", type(error).__name__)

    # Reading

    def _event(self, record: RunRecord, name: str, attempt: int | None = None) -> None:
        if len(record.events) < MAX_EVENTS:
            record.events.append(EventView(at=viewmodel.timestamp(self._clock()), name=name, attempt=attempt))

    def _fix_view(self, record: RunRecord) -> FixView | None:
        if record.fix_status is None:
            return None
        stop = record.fix_stop_reason
        return FixView(
            status=record.fix_status,
            model=AGENT_MODEL,
            reasoning_effort=AGENT_REASONING_EFFORT,
            max_attempts=fix_agent.MAX_ATTEMPTS,
            attempts=list(record.fix_attempts),
            stop_reason=stop if stop in viewmodel.STOP_TEXT else ("other" if stop else None),
            stop_text=viewmodel.STOP_TEXT.get(stop, "The fix stopped.") if stop else None,
            total_cost_usd=round(sum(item.cost_usd for item in record.fix_attempts), 6),
        )

    def run_state(self, run_id: str, client: str) -> RunStateView | None:
        with self._condition:
            record = self._records.get(run_id)
            if record is None:
                return None
            position = next((i + 1 for i, job in enumerate(self._queue) if job.record is record), None)
            return RunStateView(
                id=record.id,
                target=record.target.id,
                stage=record.stage,
                queue_position=position,
                created_at=viewmodel.timestamp(record.created_at),
                started_at=viewmodel.timestamp(record.started_at) if record.started_at else None,
                finished_at=viewmodel.timestamp(record.finished_at) if record.finished_at else None,
                events=list(record.events),
                result=record.result,
                fix=self._fix_view(record),
                fix_available=(
                    self._agent_disabled_reason() is None
                    and self._fix_limit(client, self._clock()) is None
                    and record.fix_status is None
                    and self._can_fix(record)
                ),
            )

    def _reserve(self) -> tuple[float, int]:
        """The worst case of one more model call. Below this the agent counts as out of budget."""
        agent = next((item.agent for item in self.targets.values() if item.agent is not None), None)
        if agent is None:
            return 0.0, 0
        size = len(agent.app_source) + len(agent.business_rules) + PROMPT_OVERHEAD_BYTES
        size += sum(len(source) for source in agent.support_sources.values())
        max_tokens = self.settings.agent_max_tokens
        return TokenBudget(1.0, AGENT_PRICES).cost(AGENT_MODEL, size, max_tokens), size + max_tokens

    def _agent_disabled_reason(self) -> str | None:
        if not self.settings.agent_enabled:
            return "not_enabled"
        if self._api_key is None:
            return "not_configured"
        return self.ledger.blocked_reason(*self._reserve())

    def _vm(self) -> viewmodel.VmView:
        """Cached, so that polling a public endpoint can't start a process for every request."""
        with self._vm_lock:
            now = time.monotonic()
            if self._vm_cached is None or now - self._vm_cached[0] >= self.settings.health_ttl_s:
                self._vm_cached = (now, self._vm_probe(self.settings.data_dir))
            return self._vm_cached[1]

    def status(self, client: str) -> StatusView:
        vm = self._vm()
        with self._condition:
            disabled = self._agent_disabled_reason()
            if disabled is None:
                limit = self._fix_limit(client, self._clock())
                disabled = limit[0] if limit is not None else None
            budget_reason = self.ledger.blocked_reason(*self._reserve())
            return StatusView(
                busy=self._current is not None or bool(self._queue),
                queue_length=len(self._queue),
                queue_limit=self.settings.queue_limit,
                cooldown_s=self.settings.cooldown_s,
                targets=[
                    viewmodel.TargetView(
                        id=item.id, title=item.title, description=item.description, fixable=item.agent is not None,
                    )
                    for item in self.targets.values()
                ],
                agent_enabled=disabled is None,
                agent_disabled_reason=disabled,
                budget=viewmodel.BudgetView(
                    enabled=self.settings.agent_enabled, exhausted=budget_reason is not None, **self.ledger.snapshot(),
                ),
                vm=vm,
                replays_available=len(self.replays),
            )
