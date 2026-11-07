"""Calls the target's health, reset and state hooks. Responses are untrusted and size-limited."""

import json
import re
import threading
from dataclasses import dataclass, field

import httpx

from faultforge import safety
from faultforge.expressions import FIELD_PATTERN
from faultforge.models import Build, TargetConfig

MAX_RESPONSE_BYTES = 256_000
MAX_STATE_FIELDS = 128
MAX_BUILD_VALUE_LENGTH = 128
MAX_STATE_VALUE = 2**63
BUILD_VALUE_PATTERN = re.compile(r"[\x20-\x7e]+")


class HookError(Exception):
    """A hook failed, redirected, timed out or returned something unusable."""


@dataclass(frozen=True)
class Snapshot:
    values: dict[str, int]
    in_flight: int | None
    build: Build | None
    raw: dict = field(default_factory=dict)


def _build_value(build: dict, key: str) -> str | None:
    value = build.get(key)
    if isinstance(value, str) and len(value) <= MAX_BUILD_VALUE_LENGTH and BUILD_VALUE_PATTERN.fullmatch(value):
        return value
    return None


def _state_int(value: object) -> bool:
    return type(value) is int and abs(value) < MAX_STATE_VALUE


def parse_snapshot(raw: dict) -> Snapshot:
    values = {
        key: value
        for key, value in list(raw.items())[:MAX_STATE_FIELDS]
        if key != "in_flight" and FIELD_PATTERN.fullmatch(key) and _state_int(value)
    }
    in_flight = raw.get("in_flight")
    build = raw.get("build")
    parsed_build = None
    if isinstance(build, dict):
        stale = build.get("stale_source", raw.get("stale_source"))
        parsed_build = Build(
            build_id=_build_value(build, "build_id"),
            instance_id=_build_value(build, "instance_id"),
            instance_started_at=_build_value(build, "instance_started_at"),
            stale_source=stale if isinstance(stale, bool) else None,
        )
    return Snapshot(
        values=values,
        in_flight=in_flight if _state_int(in_flight) else None,
        build=parsed_build,
        raw=raw,
    )


def _fetch(client: httpx.Client, method: str, url: str, label: str, body: dict | None) -> bytes:
    headers = {"Accept-Encoding": "identity"}
    with client.stream(method, url, json=body, headers=headers) as response:
        if response.status_code != 200:
            raise HookError(f"{label} returned {response.status_code}")
        # Size limits only mean something for bytes that are never decompressed.
        if "content-encoding" in response.headers:
            raise HookError(f"{label} returned an encoded response")
        content = bytearray()
        for chunk in response.iter_raw():
            content += chunk
            if len(content) > MAX_RESPONSE_BYTES:
                raise HookError(f"{label} response is too large")
        return bytes(content)


class TargetHooks:
    def __init__(self, target: TargetConfig, timeout_s: float):
        self.target = target
        self.timeout_s = timeout_s

    def _call(self, method: str, path: str, body: dict | None = None) -> bytes:
        """timeout_s is a hard limit on the whole call. httpx timeouts apply per read, so on
        their own they can't stop a server that drips one byte at a time."""
        url = safety.build_url(self.target.hooks_origin or self.target.origin, path)
        label = f"{method} {path}"
        # trust_env=False: proxy settings in the environment must never redirect these calls.
        client = httpx.Client(timeout=self.timeout_s, follow_redirects=False, trust_env=False)
        outcome: list[bytes | Exception] = []

        def attempt() -> None:
            try:
                outcome.append(_fetch(client, method, url, label, body))
            except Exception as error:
                outcome.append(error)

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(self.timeout_s)
        # This also ends a worker that's still stuck: its next read fails on the closed socket.
        client.close()
        if not outcome:
            raise HookError(f"{label} exceeded its {self.timeout_s:g} s deadline")
        result = outcome[0]
        if isinstance(result, HookError):
            raise result
        if isinstance(result, Exception):
            raise HookError(f"{label} failed: {type(result).__name__}") from result
        return result

    def health(self) -> None:
        self._call("GET", self.target.health)

    def reset(self, body: dict[str, object] | None = None) -> None:
        """body replaces the configured reset body, for a scenario with its own starting state."""
        reset = self.target.reset
        if reset is not None:
            self._call(reset.method, reset.path, reset.json_body if body is None else body)

    def state(self) -> Snapshot:
        content = self._call("GET", self.target.state)
        try:
            raw = json.loads(content)
        except (ValueError, RecursionError) as error:
            raise HookError("state hook didn't return valid JSON") from error
        if not isinstance(raw, dict):
            raise HookError("state hook didn't return a JSON object")
        return parse_snapshot(raw)
