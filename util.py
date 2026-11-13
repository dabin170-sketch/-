"""공용 유틸리티: 시간, ID, 해시."""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone

FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def uid(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def short_hash(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def local(s: dict, now: datetime) -> datetime:
    """사용자 현지 시각 (설정의 UTC 오프셋 적용, 기본 한국 +9시간)."""
    return now + timedelta(minutes=int(s.get("settings", {}).get("tz_offset_min", 540)))


def today(s: dict, now: datetime) -> str:
    return local(s, now).date().isoformat()


def dadd(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def ddiff(a: str, b: str) -> int:
    """a - b (일 단위)."""
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def valid_date(d) -> bool:
    try:
        date.fromisoformat(str(d))
        return True
    except Exception:
        return False


def week_key(d: str) -> str:
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def num(x, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default
