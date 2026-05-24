from datetime import datetime, timezone, timedelta


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_ts() -> float:
    return utcnow().timestamp()


def next_daily_reset(hour_utc: int, now: datetime | None = None) -> datetime:
    now = now or utcnow()
    candidate = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
