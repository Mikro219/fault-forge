"""Structured failures. They are returned to callers as data, never as tracebacks."""

from typing import Literal

ErrorClass = Literal[
    "unsafe_target",
    "target_unreachable",
    "k6_missing",
    "config_invalid",
    "budget_exceeded",
    "busy",
    "engine_failed",
    "run_not_found",
]


class FaultForgeError(Exception):
    def __init__(
        self,
        error_class: ErrorClass,
        message: str,
        fix_hint: str = "",
        suggested_plan: dict[str, int] | None = None,
    ):
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.fix_hint = fix_hint
        self.suggested_plan = suggested_plan

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": "error",
            "error_class": self.error_class,
            "message": self.message,
            "fix_hint": self.fix_hint,
        }
        if self.suggested_plan is not None:
            result["suggested_plan"] = self.suggested_plan
        return result
