"""提供可替换的时钟，便于验证租约到期和备份时间。"""

from datetime import UTC, datetime, timedelta


class SystemClock:
    """为目录的租约和备份时间提供当前系统时间。"""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """提供可手动推进的时间，让测试能够复现租约到期等情况。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
