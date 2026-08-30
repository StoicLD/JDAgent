"""Injectable clocks for lease and backup tests."""

from datetime import UTC, datetime, timedelta


class SystemClock:
    """Wall clock used by production catalog operations."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Deterministic clock that tests can advance without sleeping."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
