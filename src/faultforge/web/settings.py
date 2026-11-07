"""Every limit the public app enforces. Values come from the environment, never from a request."""

import math
import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from faultforge.fix_agent import DEFAULT_MAX_TOKENS, ApiKey, ModelPrice

KEY_NAME = "VULTR_INFERENCE_API_KEY"
AGENT_MODEL = "glm-5.3"
AGENT_REASONING_EFFORT = "low"
# USD per 1M tokens, from https://api.vultrinference.com/docs/models. Confirm in the console (V3.9).
AGENT_PRICES = {AGENT_MODEL: ModelPrice(0.75, 3.00)}
DEFAULT_DATA_DIR = Path(".faultforge") / "web"
DEMO_ROOT = Path(__file__).resolve().parents[3] / "demo"


@dataclass(frozen=True, kw_only=True)
class WebSettings:
    data_dir: Path
    agent_enabled: bool = False
    queue_limit: int = 3
    cooldown_s: float = 30.0
    runs_per_hour: int = 120
    runs_per_hour_per_ip: int = 20
    fixes_per_hour_per_ip: int = 3
    fixes_per_day_per_ip: int = 6
    fixes_per_day: int = 60
    daily_usd_cap: float = 2.0
    total_usd_cap: float = 15.0
    daily_token_cap: int = 1_000_000
    agent_max_tokens: int = DEFAULT_MAX_TOKENS
    records_limit: int = 50
    retention_age_s: float = 24 * 3600.0
    retention_keep: int = 20
    replays_per_kind: int = 5
    maintenance_interval_s: float = 60.0
    shutdown_timeout_s: float = 120.0
    health_ttl_s: float = 5.0
    # Replays recorded on a development machine and shipped with the code. Only ever read.
    recorded_replays_dir: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "WebSettings":
        return cls(
            data_dir=Path(env.get("FF_DATA_DIR") or DEFAULT_DATA_DIR).resolve(),
            agent_enabled=env.get("FF_AGENT_ENABLED") == "1",
            cooldown_s=_number(env, "FF_COOLDOWN_S", cls.cooldown_s),
            runs_per_hour=int(_number(env, "FF_RUNS_PER_HOUR", cls.runs_per_hour)),
            daily_usd_cap=_number(env, "FF_DAILY_USD_CAP", cls.daily_usd_cap),
            total_usd_cap=_number(env, "FF_TOTAL_USD_CAP", cls.total_usd_cap),
            daily_token_cap=int(_number(env, "FF_DAILY_TOKEN_CAP", cls.daily_token_cap)),
            retention_age_s=_number(env, "FF_RETENTION_AGE_S", cls.retention_age_s),
            retention_keep=int(_number(env, "FF_RETENTION_KEEP", cls.retention_keep)),
            recorded_replays_dir=DEMO_ROOT / "sportsdeck" / "replays",
        )


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    text = env.get(name)
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


def take_api_key(env: MutableMapping[str, str] = os.environ) -> ApiKey | None:
    """Removes the key from the environment, so nothing started later can inherit it. An error
    never quotes the value."""
    value = env.pop(KEY_NAME, None)
    if not value:
        return None
    try:
        return ApiKey(value.strip())
    except ValueError:
        raise ValueError(f"{KEY_NAME} is malformed") from None
