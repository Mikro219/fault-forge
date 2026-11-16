"""Stored MCP runs and the compact views of them that tools return (docs/PLAN.md v2.1 §3.3).

A run lives in <state>/runs/<run_id>/: the runner's report.json, report.md, requests.jsonl and trial
files, plus run.json with what the server knows about how it ran. Every view stays short: about 60
lines, with the rest paged through faultforge_get_run. Text the target could influence, such as a
captured response field or a probe's detail, is cleaned and cut short.

A run also keeps pinned/: the faultforge.yaml it ran with, BUSINESS_RULES.md if the snapshot had
one, and for a Python target the support package beside app.py. A fix is judged against those copies
only, never against the config or the package in a candidate's tree, so an edit that weakens a rule
or rewrites a test hook alongside the code can't pass the gate.
"""

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from faultforge import config as config_module, report as report_module, safety
from faultforge.errors import FaultForgeError
from faultforge.mcp_server.settings import private_dir
from faultforge.models import Contract, Report, RequestRecord
from faultforge.sandbox import SandboxResult, code_dir_sha256

RUN_META_NAME = "run.json"
SANDBOX_NAME = "sandbox.json"
PINNED_DIR = "pinned"
PACKAGE_DIR = "package"
RULES_NAME = "BUSINESS_RULES.md"
MAX_RULES_BYTES = 64 * 1024
MAX_SCOPE_FILES = 200
MAX_META_BYTES = 256 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_LINES = 100_000
MAX_EVIDENCE_LINE_BYTES = 4096
MAX_PAGE = 100
DEFAULT_PAGE = 50
MAX_LIST_LINES = 12
MAX_DISCLOSURES = 6
MAX_PROBES = 12
MAX_TEXT = 200
UNPRINTABLE = re.compile(r"[^\x20-\x7e]")

Section = Literal["summary", "violations", "hypotheses", "performance", "evidence"]
SECTIONS: tuple[str, ...] = ("summary", "violations", "hypotheses", "performance", "evidence")
OUTCOMES = ("accepted", "rejected", "unexpected", "transport_error")


class RunMeta(Contract):
    """What the server recorded about a run, apart from the report itself."""

    run_id: str = Field(pattern=r"^run_\d{8}_\d{6}_[a-f0-9]{4}$")
    project_dir: str
    scenario_id: str
    kind: Literal["python", "stack"]
    source: dict[str, object]
    runtime: Literal["local"] = "local"
    compare_to: str | None = None
    deployed_code_sha256: str | None = None
    containers_left: int | None = None
    limits_applied: bool | None = None
    state_verification: str | None = None
    notes: list[str] = Field(default_factory=list)
    # sha256 of pinned/faultforge.yaml. None for a run stored before configs were pinned.
    pinned_config_sha256: str | None = None
    # The support package pinned with a Python run, and the hash of pinned/package (sandbox.py's
    # code_dir_sha256). The hash is None for a stack run and for a run stored before packages were
    # pinned; the name is None when the project has no package beside app.py.
    pinned_package: str | None = None
    pinned_package_sha256: str | None = None
    # The files the host deployed for a Python target, relative to the project, each with its sha256.
    scope: dict[str, str] = Field(default_factory=dict, max_length=MAX_SCOPE_FILES)
    # "run" for faultforge_run_scenario. The runs faultforge_verify_fix stores are "verify" (the main
    # scenario) and "regression"; neither can be the baseline of a fix.
    role: Literal["run", "verify", "regression"] = "run"
    # Set on the run faultforge_verify_fix stores: the whole gate's answer, not the comparison alone.
    fix_gate: dict[str, object] | None = None


def clean(value: object, limit: int = MAX_TEXT) -> str:
    text = UNPRINTABLE.sub("?", str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def runs_root(state: Path) -> Path:
    return private_dir(state / "runs")


def run_path(state: Path, run_id: object) -> Path:
    """The format is checked before any path is built from it."""
    if not isinstance(run_id, str):
        raise FaultForgeError("invalid_argument", "run_id must be a string")
    safety.validate_run_id(run_id)
    root = runs_root(state)
    return safety.contained(root, root / run_id)


def _regular_file(path: Path, limit: int) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise FaultForgeError("run_not_found", f"{path.name} is missing for this run") from error
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise FaultForgeError("run_not_found", f"{path.name} isn't a readable file for this run")
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pin(destination: Path, source_dir: Path) -> None:
    """Copies the config a run used, and its business rules if there are any, into the run."""
    pinned = destination / PINNED_DIR
    pinned.mkdir(mode=0o700)
    for name, limit, required in (
        (config_module.CONFIG_NAME, config_module.MAX_CONFIG_BYTES, True), (RULES_NAME, MAX_RULES_BYTES, False),
    ):
        source = source_dir / name
        try:
            info = source.lstat()
        except FileNotFoundError:
            info = None
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            if required:
                raise FaultForgeError("engine_failed", f"{name} can't be pinned for this run")
            continue
        shutil.copyfile(source, pinned / name, follow_symlinks=False)
        os.chmod(pinned / name, 0o600)


def _pin_package(destination: Path, package: Path | None) -> tuple[str | None, str]:
    """Copies the support package a Python run deployed into pinned/package/<name>/ and returns its
    name and the hash of pinned/package. The source is a snapshot or another run's pinned copy, so
    it holds regular files only, with secrets already dropped; anything else is refused. Bytecode is
    left out, as sandbox.py leaves it out of what it deploys."""
    root = destination / PINNED_DIR / PACKAGE_DIR
    root.mkdir(mode=0o700)
    if package is not None:
        if package.is_symlink() or not package.is_dir() or not package.name.isidentifier():
            raise FaultForgeError("engine_failed", "the support package can't be pinned for this run")
        for directory, subdirs, files in os.walk(package, followlinks=False):
            subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
            target = root / package.name / Path(directory).relative_to(package)
            target.mkdir(mode=0o700)
            for name in [*subdirs, *files]:
                info = (Path(directory) / name).lstat()
                if not (stat.S_ISDIR(info.st_mode) if name in subdirs else stat.S_ISREG(info.st_mode)):
                    raise FaultForgeError("engine_failed", "the support package holds a link or special file")
            for name in files:
                if not name.endswith(".pyc"):
                    shutil.copyfile(Path(directory) / name, target / name, follow_symlinks=False)
                    os.chmod(target / name, 0o600)
    return (package.name if package is not None else None), code_dir_sha256(root)


def store_run(
    state: Path, work_dir: Path, report: Report, meta: RunMeta, result: SandboxResult, pin_from: Path | None = None,
    package_from: Path | None = None,
) -> Path:
    """Moves the runner's run directory out of the throwaway work dir, then records how it ran.
    pin_from is the directory holding the faultforge.yaml the run used. For a Python run,
    package_from is the support package it deployed, or None if it has none."""
    source = report_module.run_dir(work_dir, report.run_id)
    destination = run_path(state, report.run_id)
    # mkdir is exclusive, so a second server with the same state dir can't merge two runs into one.
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as error:
        raise FaultForgeError("engine_failed", f"{report.run_id} is already stored") from error
    os.rename(source, destination)
    if pin_from is not None:
        _pin(destination, pin_from)
        if meta.kind == "python":
            name, digest = _pin_package(destination, package_from)
            meta = meta.model_copy(update={"pinned_package": name, "pinned_package_sha256": digest})
    report_module.write_json(destination / RUN_META_NAME, meta.model_dump(mode="json"))
    report_module.write_json(destination / SANDBOX_NAME, result.model_dump(mode="json", exclude={"report"}))
    return destination


def stage_baseline(state: Path, work_dir: Path, run_id: str) -> None:
    """The runner reads the baseline from its own /out, so only its report goes there."""
    source = _regular_file(run_path(state, run_id) / report_module.REPORT_NAME, MAX_REPORT_BYTES)
    destination = report_module.run_dir(work_dir, run_id)
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination / report_module.REPORT_NAME, follow_symlinks=False)


def pinned_config(state: Path, meta: RunMeta) -> Path:
    """The config a run was stored with, checked against the hash recorded beside it. Fails closed:
    without it a fix can't be judged against this run."""
    hint = "Call faultforge_run_scenario again to make a new baseline, then fix against that run."
    if meta.pinned_config_sha256 is None:
        raise FaultForgeError("not_comparable", f"{meta.run_id} was stored without a pinned {config_module.CONFIG_NAME}", hint)
    path = run_path(state, meta.run_id) / PINNED_DIR / config_module.CONFIG_NAME
    try:
        info = path.lstat()
        usable = stat.S_ISREG(info.st_mode) and info.st_size <= config_module.MAX_CONFIG_BYTES
        usable = usable and file_sha256(path) == meta.pinned_config_sha256
    except OSError:
        usable = False
    if not usable:
        raise FaultForgeError("not_comparable", f"the pinned {config_module.CONFIG_NAME} of {meta.run_id} is missing or changed", hint)
    return path


def pinned_package(state: Path, meta: RunMeta) -> Path | None:
    """The support package a run deployed, checked against the hash recorded beside it, or None if
    the project had none. Fails closed like pinned_config: a fix deploys this copy, never the
    candidate's."""
    hint = "Call faultforge_run_scenario again to make a new baseline, then fix against that run."
    if meta.pinned_package_sha256 is None:
        raise FaultForgeError("not_comparable", f"{meta.run_id} was stored without a pinned support package", hint)
    root = run_path(state, meta.run_id) / PINNED_DIR / PACKAGE_DIR
    try:
        entries = sorted(entry.name for entry in root.iterdir())
        usable = not root.is_symlink() and entries == ([meta.pinned_package] if meta.pinned_package else [])
        usable = usable and not any(path.is_symlink() for path in root.rglob("*"))
        usable = usable and code_dir_sha256(root) == meta.pinned_package_sha256
    except OSError:
        usable = False
    if not usable:
        raise FaultForgeError("not_comparable", f"the pinned support package of {meta.run_id} is missing or changed", hint)
    return root / meta.pinned_package if meta.pinned_package else None


def pinned_rules(state: Path, run_id: str) -> str | None:
    """The business rules pinned with a run, or None. Written by the project, so untrusted text."""
    path = run_path(state, run_id) / PINNED_DIR / RULES_NAME
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RULES_BYTES:
            return None
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


def read_meta(state: Path, run_id: object) -> RunMeta:
    path = _regular_file(run_path(state, run_id) / RUN_META_NAME, MAX_META_BYTES)
    try:
        return RunMeta.model_validate_json(path.read_bytes())
    except ValidationError as error:
        raise FaultForgeError("run_not_found", f"run.json for {run_id} is invalid") from error


def read_report(state: Path, run_id: object) -> Report:
    path = _regular_file(run_path(state, run_id) / report_module.REPORT_NAME, MAX_REPORT_BYTES)
    try:
        return Report.model_validate_json(path.read_bytes())
    except ValidationError as error:
        raise FaultForgeError("run_not_found", f"report.json for {run_id} is invalid") from error


# Views


def _capped(lines: list[str], what: str, run_id: str, section: str) -> list[str]:
    if len(lines) <= MAX_LIST_LINES:
        return lines
    more = len(lines) - MAX_LIST_LINES + 1
    return [*lines[: MAX_LIST_LINES - 1], f"... {more} more {what}: faultforge_get_run({run_id!r}, section={section!r})"]


def _observed(values: dict[str, int | float]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(values.items()))


def proven_lines(report: Report) -> list[str]:
    lines = [
        f"{violation.id} {clean(violation.invariant or violation.type)}: violated in {violation.trials_violated} trials"
        + (f" ({clean(_observed(violation.observed), 160)})" if violation.observed else "")
        for violation in report.violations
    ]
    return _capped(lines, "violations", report.run_id, "violations") or ["No violations."]


def suspected_lines(report: Report) -> list[str]:
    lines = [
        f"{item.id} {item.suspected_cause} ({item.status}), explains {', '.join(item.explains)}"
        for item in report.hypotheses
    ]
    return _capped(lines, "hypotheses", report.run_id, "hypotheses") or ["Nothing suspected."]


def completeness_line(report: Report) -> str:
    completeness = report.completeness
    line = f"{report_module.verdict_line(report)}; serial control {completeness.serial_control}"
    if completeness.trials_skipped:
        line += f"; {completeness.trials_skipped} trials skipped"
    return line


def performance_line(report: Report) -> str | None:
    if report.performance is None:
        return None
    performance = report.performance
    latency = performance.latency_ms
    return (
        f"p50 {latency.p50} ms, p95 {latency.p95} ms, error rate {performance.error_rate}, "
        f"rejection rate {performance.rejection_rate} (doesn't affect the verdict)"
    )


def comparison_lines(report: Report) -> list[str] | None:
    comparison = report.comparison
    if comparison is None:
        return None
    if comparison.status == "NOT_COMPARABLE":
        return [f"NOT_COMPARABLE with {comparison.baseline_run_id}: {clean(comparison.not_comparable_reason)}"]
    lines = [f"Compared with {comparison.baseline_run_id}: fix_verified={comparison.fix_verified}"]
    lines += [
        f"{clean(item.name)}: {item.result}" + (f" ({item.reason})" if item.reason else "") for item in comparison.invariants
    ]
    return _capped(lines, "invariants", report.run_id, "summary")


def _probe_lines(state: Path, run_id: str) -> list[str]:
    """What a target says it did is self-reported, so it's labelled that way."""
    try:
        path = _regular_file(run_path(state, run_id) / SANDBOX_NAME, MAX_REPORT_BYTES)
        probes = json.loads(path.read_bytes()).get("containment", {}).get("probes", [])
    except (FaultForgeError, OSError, ValueError, AttributeError, RecursionError):
        return []
    lines = []
    for probe in probes[:MAX_PROBES] if isinstance(probes, list) else []:
        if isinstance(probe, dict) and probe.get("phase") == "result":
            lines.append(f"{clean(probe.get('probe'), 40)}: {clean(probe.get('outcome'), 20)} (self-reported by the target)")
    return lines


def summary(state: Path, run_id: str) -> dict[str, object]:
    report, meta = read_report(state, run_id), read_meta(state, run_id)
    result: dict[str, object] = {
        "status": "ok",
        "run_id": report.run_id,
        "verdict": report.verdict,
        "verdict_reason": clean(report.verdict_reason, 400),
        "scenario_id": report.scenario_id,
        "project_dir": meta.project_dir,
        "kind": meta.kind,
        "runtime": meta.runtime,
        "source": meta.source,
        "completeness": completeness_line(report),
        "proven": proven_lines(report),
        "suspected": suspected_lines(report),
        "suspected_notice": report_module.BLACK_BOX_NOTICE,
        "performance": performance_line(report),
        "comparison": comparison_lines(report),
        "containment": {
            "containers_left": meta.containers_left,
            "limits_applied": meta.limits_applied,
            "state_verification": meta.state_verification,
            "deployed_code_sha256": meta.deployed_code_sha256,
        },
        "disclosures": [clean(item) for item in report.disclosures[:MAX_DISCLOSURES]],
        "notes": meta.notes,
        "more": "faultforge_get_run(run_id, section=violations|hypotheses|performance|evidence)",
    }
    probes = _probe_lines(state, run_id)
    if probes:
        result["probes"] = probes
    if meta.fix_gate is not None:
        # The comparison above is one gate of four; this is the whole answer, on one line.
        failed = meta.fix_gate.get("failed")
        names = ", ".join(clean(name, 40) for name in failed) if isinstance(failed, list) and failed else "none"
        result["fix_gate"] = (
            f"verified={meta.fix_gate.get('verified') is True} against {clean(meta.fix_gate.get('baseline_run_id'), 40)}; failed gates: {names}"
        )
    return result


def _page(items: list, offset: int, limit: int) -> tuple[list, dict[str, object]]:
    page = items[offset: offset + limit]
    following = offset + len(page)
    return page, {"offset": offset, "limit": limit, "total": len(items), "next_offset": following if following < len(items) else None}


def violations(state: Path, run_id: str, offset: int, limit: int) -> dict[str, object]:
    report = read_report(state, run_id)
    items = [
        {
            "id": item.id,
            "type": item.type,
            "invariant": clean(item.invariant) if item.invariant else None,
            "trials_violated": item.trials_violated,
            "observed": item.observed,
            "facts": {key: clean(value, 80) if isinstance(value, str) else value for key, value in item.facts.items()},
            "evidence_filter": item.evidence.filter if item.evidence else None,
            "samples": len(item.evidence.sample) if item.evidence else 0,
        }
        for item in report.violations
    ]
    page, paging = _page(items, offset, limit)
    return {"status": "ok", "run_id": run_id, "section": "violations", "items": page, **paging}


def hypotheses(state: Path, run_id: str, offset: int, limit: int) -> dict[str, object]:
    report = read_report(state, run_id)
    items = [
        {
            "id": item.id, "suspected_cause": item.suspected_cause, "status": item.status, "explains": item.explains,
            "basis": [clean(entry) for entry in item.basis], "how_to_confirm": clean(item.how_to_confirm, 400),
        }
        for item in report.hypotheses
    ]
    page, paging = _page(items, offset, limit)
    return {
        "status": "ok", "run_id": run_id, "section": "hypotheses", "notice": report_module.BLACK_BOX_NOTICE,
        "items": page, **paging,
    }


def performance(state: Path, run_id: str) -> dict[str, object]:
    report = read_report(state, run_id)
    return {
        "status": "ok",
        "run_id": run_id,
        "section": "performance",
        "performance": report.performance.model_dump(mode="json") if report.performance else None,
        "delta": report.comparison.performance_delta if report.comparison else None,
        "budget": report.budget.model_dump(mode="json"),
    }


def _records(path: Path) -> Iterator[RequestRecord | None]:
    with path.open("rb") as lines:
        for number, line in enumerate(lines):
            if number >= MAX_EVIDENCE_LINES:
                return
            if len(line) > MAX_EVIDENCE_LINE_BYTES:
                yield None
                continue
            try:
                yield RequestRecord.model_validate_json(line)
            except ValidationError:
                yield None


def evidence(
    state: Path, run_id: str, offset: int, limit: int, trial: int | None = None, outcome: str | None = None,
) -> dict[str, object]:
    if outcome is not None and outcome not in OUTCOMES:
        raise FaultForgeError("invalid_argument", f"outcome must be one of {', '.join(OUTCOMES)}")
    path = _regular_file(run_path(state, run_id) / report_module.REQUESTS_NAME, 64 * 1024 * 1024)
    items, total, invalid = [], 0, 0
    for record in _records(path):
        if record is None:
            invalid += 1
            continue
        if (trial is not None and record.trial != trial) or (outcome is not None and record.outcome != outcome):
            continue
        if offset <= total < offset + limit:
            # One line per request. captured comes from the target's response, so it's untrusted.
            captured = f" captured={clean(record.captured, 40)}" if record.captured is not None else ""
            items.append(
                f"t{record.trial} vu{record.vu} it{record.iter} {record.status} {record.outcome} "
                f"{record.latency_ms:.1f}ms start={record.t_start:.0f}{captured}"
            )
        total += 1
    following = offset + len(items)
    return {
        "status": "ok", "run_id": run_id, "section": "evidence",
        "format": "trial vu iteration status outcome latency start_epoch_ms [captured, untrusted]",
        "filters": {"trial": trial, "outcome": outcome}, "items": items,
        "offset": offset, "limit": limit, "total": total, "invalid_lines": invalid,
        "next_offset": following if following < total else None,
    }


def page_bounds(offset: object, limit: object) -> tuple[int, int]:
    if type(offset) is not int or offset < 0:
        raise FaultForgeError("invalid_argument", "offset must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise FaultForgeError("invalid_argument", f"limit must be 1 to {MAX_PAGE}")
    return offset, limit


STALE_AFTER_S = 2 * 3600


def sweep_stale(state: Path, now: float, max_age_s: float = STALE_AFTER_S) -> int:
    """Removes snapshots and work dirs a killed server left behind. Far older than any run, so a
    second server's live run is never touched."""
    removed = 0
    for name in ("snapshots", "work"):
        parent = state / name
        if parent.is_symlink() or not parent.is_dir():
            continue
        for entry in parent.iterdir():
            try:
                if now - entry.lstat().st_mtime >= max_age_s:
                    remove_tree(entry)
                    removed += 1
            except OSError:
                continue
    return removed


def remove_tree(path: Path) -> None:
    if path.exists() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.is_symlink():
        os.unlink(path)
