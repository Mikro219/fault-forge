"""Client identity and the sliding windows behind the public limits."""

import ipaddress
from collections import deque
from dataclasses import dataclass, field

TRUSTED_PROXY = "127.0.0.1"
UNKNOWN_CLIENT = "unknown"


def client_key(peer: str | None, forwarded_for: list[str]) -> str:
    """X-Forwarded-For counts only from the local reverse proxy, and only its last entry: that's
    the one the proxy added, and anything before it came from the client."""
    if peer == TRUSTED_PROXY and forwarded_for:
        forwarded = _normalize(forwarded_for[-1].split(",")[-1].strip())
        if forwarded is not None:
            return forwarded
    return _normalize(peer or "") or UNKNOWN_CLIENT


def _normalize(text: str) -> str | None:
    """IPv6 is keyed per /64, because one client usually controls a whole /64."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


@dataclass
class SlidingWindow:
    limit: int
    window_s: float
    _hits: deque[float] = field(default_factory=deque)

    def _prune(self, now: float) -> None:
        while self._hits and now - self._hits[0] >= self.window_s:
            self._hits.popleft()

    def retry_after(self, now: float) -> float:
        """0 if a hit is allowed now, otherwise the seconds until one is."""
        self._prune(now)
        if len(self._hits) < self.limit:
            return 0.0
        return self._hits[0] + self.window_s - now

    def hit(self, now: float) -> None:
        self._hits.append(now)


@dataclass
class KeyedWindows:
    """One window per client. Expired ones are dropped on each hit, and hits are limited elsewhere."""

    limit: int
    window_s: float
    _windows: dict[str, SlidingWindow] = field(default_factory=dict)

    def retry_after(self, key: str, now: float) -> float:
        window = self._windows.get(key)
        return window.retry_after(now) if window is not None else 0.0

    def hit(self, key: str, now: float) -> None:
        for window in self._windows.values():
            window.retry_after(now)
        self._windows = {name: window for name, window in self._windows.items() if window._hits}
        self._windows.setdefault(key, SlidingWindow(self.limit, self.window_s)).hit(now)
