"""Hard limits. Config and arguments can lower these but never raise them (docs/PLAN.md §4.6)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    max_vus: int = 50
    max_iterations_per_trial: int = 500
    max_trial_duration_s: int = 20
    max_trials: int = 5
    max_requests_total: int = 2000
    max_wall_s_total: float = 60
    trial_overhead_s: float = 3
    admission_margin_s: float = 5
    hook_timeout_s: float = 2
    settle_cap_s: float = 2


LIMITS = Limits()
