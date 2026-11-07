"""Inference spend that survives a restart, enforced before every model call.

The file holds only numbers. If it can't be read or written, the ledger fails closed: the agent
stops, and nothing resets it to zero.
"""

import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from faultforge.fix_agent import ModelPrice, TokenBudget

logger = logging.getLogger("faultforge.web")
MAX_LEDGER_BYTES = 1024 * 1024
DAYS_KEPT = 40


class BudgetLedger:
    def __init__(
        self, path: Path, *, daily_usd_cap: float, total_usd_cap: float, daily_token_cap: int,
        clock: Callable[[], float] = time.time,
    ):
        self.path = path
        self.daily_usd_cap = daily_usd_cap
        self.total_usd_cap = total_usd_cap
        self.daily_token_cap = daily_token_cap
        self._clock = clock
        self._lock = threading.Lock()
        self._unavailable = False
        self._total_usd = 0.0
        self._total_tokens = 0
        self._days: dict[str, dict[str, float]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > MAX_LEDGER_BYTES:
                raise ValueError("too large")
            raw = json.loads(self.path.read_bytes(), parse_constant=_refuse_constant)
            total_usd = _spend(raw["total_usd"])
            total_tokens = int(_spend(raw["total_tokens"]))
            days = {
                str(day): {"usd": _spend(value["usd"]), "tokens": _spend(value["tokens"])}
                for day, value in raw["days"].items()
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
            logger.error("budget ledger unreadable (%s); the agent stays off", type(error).__name__)
            self._unavailable = True
            return
        self._total_usd, self._total_tokens, self._days = total_usd, total_tokens, days

    def _today(self) -> dict[str, float]:
        day = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")
        return self._days.get(day, {"usd": 0.0, "tokens": 0})

    def blocked_reason(self, usd: float = 0.0, tokens: int = 0) -> str | None:
        """Why spending this much more isn't allowed. At zero, only a cap that's already met counts."""
        with self._lock:
            return self._blocked_reason(usd, tokens)

    def _blocked_reason(self, usd: float, tokens: int) -> str | None:
        if self._unavailable:
            return "ledger_unavailable"
        today = self._today()
        if self._total_usd + usd >= self.total_usd_cap:
            return "total_usd_cap"
        if today["usd"] + usd >= self.daily_usd_cap:
            return "daily_usd_cap"
        if today["tokens"] + tokens >= self.daily_token_cap:
            return "daily_token_cap"
        return None

    def record(self, usd: float, tokens: int) -> None:
        with self._lock:
            day = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")
            today = self._days.setdefault(day, {"usd": 0.0, "tokens": 0})
            today["usd"] += usd
            today["tokens"] += tokens
            self._total_usd += usd
            self._total_tokens += tokens
            for old in sorted(self._days)[:-DAYS_KEPT]:
                del self._days[old]
            try:
                self._write()
            except OSError:
                # What was just spent can't be made durable, so nothing more may be spent.
                self._unavailable = True
                raise

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        data = {"total_usd": self._total_usd, "total_tokens": self._total_tokens, "days": self._days}
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            today = self._today()
            return {
                "daily_usd_spent": round(today["usd"], 6),
                "daily_usd_cap": self.daily_usd_cap,
                "total_usd_spent": round(self._total_usd, 6),
                "total_usd_cap": self.total_usd_cap,
                "daily_tokens_spent": int(today["tokens"]),
                "daily_token_cap": self.daily_token_cap,
            }


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} isn't JSON")


def _spend(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("not an amount")
    return float(value)


class LedgerBudget(TokenBudget):
    """The fix agent's budget, backed by the ledger: propose_fix asks before each call and records
    after it, so the caps hold across attempts, runs and restarts."""

    def __init__(self, ledger: BudgetLedger, prices: Mapping[str, ModelPrice]):
        super().__init__(cap_usd=ledger.total_usd_cap, prices=prices)
        self.ledger = ledger

    def can_afford(self, model: str, input_tokens: int, max_tokens: int) -> bool:
        cost = self.cost(model, input_tokens, max_tokens)
        return self.ledger.blocked_reason(cost, input_tokens + max_tokens) is None

    def charge(self, model: str, input_tokens: int, output_tokens: int) -> float:
        cost = super().charge(model, input_tokens, output_tokens)
        self.ledger.record(cost, input_tokens + output_tokens)
        return cost
