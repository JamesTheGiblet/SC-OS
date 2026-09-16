"""Monotonic + wall clock. Replaceable for tests."""

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def monotonic() -> float:
    import time
    return time.monotonic()