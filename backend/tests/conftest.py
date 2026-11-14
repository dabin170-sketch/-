import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

os.environ["LIFEOS_NO_AUTOSTART"] = "1"
os.environ["LIFEOS_INSECURE_COOKIE"] = "1"
os.environ["SECRET_KEY"] = "test-secret"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents import init_agents  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.store import LocalBackend, Store  # noqa: E402
from app.util import iso  # noqa: E402

# 2026-11-13 (금) 09:00 KST
T0 = datetime(2026, 11, 13, 0, 0, 0, tzinfo=timezone.utc)
TODAY = "2026-11-13"


class Clock:
    def __init__(self):
        self.now = T0

    def tick(self, sec=1):
        self.now += timedelta(seconds=sec)
        return self.now


def add(store, coll, **rec):
    c = store.s["c"].setdefault(coll, {})
    rid = rec.pop("id", f"{coll}_{len(c) + 1:03d}")
    c[rid] = {"id": rid, "created_at": iso(T0), "updated_at": iso(T0), **rec}
    return rid


def run(eng, clock, max_ticks=400, until_idle=True):
    """세션이 살아 있는 상태로 엔진을 돌린다. 반환: tick 결과 목록."""
    out = []
    for _ in range(max_ticks):
        eng.heartbeat(clock.now)
        r = eng.tick(clock.tick())
        out.append(r)
        if until_idle and r in ("idle", "limit", "paused"):
            break
    return out


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path):
    st = Store(LocalBackend(str(tmp_path / "data")))
    st.load()
    init_agents(st.s, T0)
    return st


@pytest.fixture
def eng(store):
    return Engine(store)
