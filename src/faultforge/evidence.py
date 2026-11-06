"""Engine-independent calculations over per-request evidence."""

from collections.abc import Iterable

from faultforge.models import ClientCounts, RequestRecord


def peak_in_flight(records: Iterable[RequestRecord]) -> int:
    """Highest number of overlapping requests. Touching intervals don't count as overlap."""
    events = []
    for record in records:
        events.append((record.t_start, 1))
        events.append((record.t_end, -1))
    current = peak = 0
    for _, delta in sorted(events):
        current += delta
        peak = max(peak, current)
    return peak


def count_outcomes(records: Iterable[RequestRecord]) -> ClientCounts:
    counts = {"accepted": 0, "rejected": 0, "unexpected": 0, "transport_error": 0}
    for record in records:
        counts[record.outcome] += 1
    return ClientCounts(**counts)
