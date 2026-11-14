"""영구 저장 계층.

- GitHubBackend: GitHub Contents API (파일 SHA 기반 갱신, 충돌 감지)
- LocalBackend : 로컬 파일 (개발·테스트용. Render에서는 영구 저장소가 아님)
- Store        : 메모리 상태 + 묶음 동기화 + 충돌 병합 + 일일 스냅샷 + 선택적 암호화

원칙: 영구 저장소에 실제로 기록된 뒤에만 '저장 완료'로 표시한다.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from datetime import datetime
from typing import Optional

from .util import iso, parse, uid, utcnow

STATE_FILE = "state.json"
INBOX_FILE = "inbox.json"
SCHEMA_VERSION = 1

DEFAULT_SETTINGS = {
    "tz_offset_min": 540,
    "daily_hour_limit": 10,
    "max_tasks_per_session": 40,
    "max_steps_per_day": 1500,
    "ai_daily_limit": 60,
    "commits_per_day": 300,
    "render_hours_limit": 700,
    "discovery_interval_sec": 60,
    "max_retries": 2,
    "web_research": False,
    "web_daily_limit": 30,
    "cron_max_steps": 300,
    "paused": False,
    "updated_at": "",
}

DICTS = ["agents", "tasks", "approvals", "ai_jobs", "workflows", "files", "ai_stats", "verifications", "intake"]
LISTS = {"task_logs": 3000, "sessions": 200, "audit": 1500, "history": 1500, "inbox": 100}


class Conflict(Exception):
    """원격 파일이 내가 알던 버전과 다름."""


def new_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION, "rev": 0, "updated_at": "",
        "user": None, "settings": dict(DEFAULT_SETTINGS),
        "agents": {}, "tasks": {}, "approvals": {}, "ai_jobs": {}, "c": {},
        "workflows": {}, "files": {}, "ai_stats": {}, "verifications": {}, "intake": {},
        "task_logs": [], "sessions": [], "audit": [], "history": [], "inbox": [],
        "usage": {}, "idem": {}, "deleted": {}, "meta": {},
    }


def migrate(s: dict) -> dict:
    """누락 필드를 채우고 스키마 버전을 맞춘다."""
    base = new_state()
    for k, v in base.items():
        if k not in s or s[k] is None and k != "user":
            s[k] = v
    for k, v in DEFAULT_SETTINGS.items():
        s["settings"].setdefault(k, v)
    s["schema_version"] = SCHEMA_VERSION
    return s


def _newer(a: dict, b: dict) -> dict:
    return a if a.get("updated_at", "") >= b.get("updated_at", "") else b


def merge(a: dict, b: dict) -> dict:
    """두 상태를 레코드 단위로 병합 (updated_at이 더 최신인 쪽 우선, 삭제 기록 존중)."""
    out = copy.deepcopy(a)
    deleted = dict(b.get("deleted", {}))
    for k, v in a.get("deleted", {}).items():
        if v > deleted.get(k, ""):
            deleted[k] = v
    out["deleted"] = deleted

    def md(x: dict, y: dict) -> dict:
        r = {}
        for i in set(x) | set(y):
            rec = _newer(x[i], y[i]) if i in x and i in y else (x.get(i) or y.get(i))
            if i in deleted and deleted[i] >= rec.get("updated_at", ""):
                continue
            r[i] = copy.deepcopy(rec)
        return r

    for k in DICTS:
        out[k] = md(a.get(k, {}), b.get(k, {}))
    out["c"] = {n: md(a.get("c", {}).get(n, {}), b.get("c", {}).get(n, {}))
                for n in set(a.get("c", {})) | set(b.get("c", {}))}
    for k, cap in LISTS.items():
        seen, items = set(), []
        for it in a.get(k, []) + b.get(k, []):
            if it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            items.append(copy.deepcopy(it))
        items.sort(key=lambda it: it.get("ts") or it.get("started_at") or "")
        out[k] = items[-cap:]
    usage = copy.deepcopy(b.get("usage", {}))
    for d, u in a.get("usage", {}).items():
        cur = usage.setdefault(d, {})
        for kk, vv in u.items():
            if isinstance(vv, (int, float)) and isinstance(cur.get(kk, 0), (int, float)):
                cur[kk] = max(cur.get(kk, 0), vv)
            else:
                cur.setdefault(kk, vv)
    out["usage"] = usage
    out["idem"] = {**b.get("idem", {}), **a.get("idem", {})}
    out["meta"] = {**b.get("meta", {}), **a.get("meta", {})}
    out["settings"] = copy.deepcopy(_newer(a.get("settings", {}), b.get("settings", {})))
    ua, ub = a.get("user"), b.get("user")
    out["user"] = copy.deepcopy(_newer(ua, ub) if ua and ub else (ua or ub))
    out["rev"] = max(a.get("rev", 0), b.get("rev", 0)) + 1
    out["updated_at"] = max(a.get("updated_at", ""), b.get("updated_at", ""))
    return migrate(out)


# ---------------------------------------------------------------- backends
class LocalBackend:
    durable = False
    label = "로컬 파일 (영구 저장 아님)"

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def _p(self, path: str) -> str:
        p = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def read(self, path: str):
        p = self._p(path)
        if not os.path.exists(p):
            return None, None
        raw = open(p, "rb").read()
        return raw, hashlib.sha1(raw).hexdigest()

    def write(self, path: str, data: bytes, sha: Optional[str], message: str) -> str:
        cur, cur_sha = self.read(path)
        if cur is not None and sha != cur_sha:
            raise Conflict(path)
        p = self._p(path)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        return hashlib.sha1(data).hexdigest()

    def list(self, directory: str) -> list[str]:
        p = os.path.join(self.dir, directory)
        return sorted(os.listdir(p)) if os.path.isdir(p) else []


class GitHubBackend:
    """GitHub 저장소의 전용 브랜치에 JSON 파일을 저장한다 (코드 브랜치와 분리 → 재배포를 유발하지 않음)."""
    durable = True
    label = "GitHub 저장소"

    def __init__(self, token: str, repo: str, branch: str = "lifeos-data", client=None):
        import httpx
        self.repo, self.branch = repo, branch
        self.http = client or httpx.Client(timeout=20)
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "life-os"}
        self.base = f"https://api.github.com/repos/{repo}"
        self._branch_ok = False

    def _req(self, method: str, url: str, **kw):
        return self.http.request(method, self.base + url, headers=self.headers, **kw)

    def ensure_branch(self):
        if self._branch_ok:
            return
        r = self._req("GET", f"/branches/{self.branch}")
        if r.status_code == 404:
            # 코드와 섞이지 않도록 부모 없는(orphan) 브랜치를 만든다.
            t = self._req("POST", "/git/trees", json={"tree": [
                {"path": "README.md", "mode": "100644", "type": "blob",
                 "content": "LIFE OS 데이터 브랜치입니다. 직접 수정하지 마세요.\n"}]})
            t.raise_for_status()
            c = self._req("POST", "/git/commits", json={
                "message": "LIFE OS 데이터 브랜치 생성", "tree": t.json()["sha"], "parents": []})
            c.raise_for_status()
            ref = self._req("POST", "/git/refs", json={
                "ref": f"refs/heads/{self.branch}", "sha": c.json()["sha"]})
            if ref.status_code not in (201, 422):
                ref.raise_for_status()
        elif r.status_code != 200:
            r.raise_for_status()
        self._branch_ok = True

    def read(self, path: str):
        r = self._req("GET", f"/contents/{path}", params={"ref": self.branch})
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        j = r.json()
        content = j.get("content") or ""
        if not content and j.get("size", 0) > 0:  # 1MB 초과 파일은 blob API로 읽는다
            b = self._req("GET", f"/git/blobs/{j['sha']}")
            b.raise_for_status()
            content = b.json()["content"]
        return base64.b64decode(content), j["sha"]

    def write(self, path: str, data: bytes, sha: Optional[str], message: str) -> str:
        self.ensure_branch()
        body = {"message": message, "content": base64.b64encode(data).decode(), "branch": self.branch}
        if sha:
            body["sha"] = sha
        r = self._req("PUT", f"/contents/{path}", json=body)
        if r.status_code in (409, 422):
            raise Conflict(path)
        r.raise_for_status()
        return r.json()["content"]["sha"]

    def list(self, directory: str) -> list[str]:
        r = self._req("GET", f"/contents/{directory}", params={"ref": self.branch})
        if r.status_code != 200:
            return []
        return sorted(x["name"] for x in r.json() if isinstance(x, dict))


# ---------------------------------------------------------------- store
class Store:
    def __init__(self, backend, data_key: str = ""):
        self.b = backend
        self.fernet = None
        if data_key:
            from cryptography.fernet import Fernet
            self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(data_key.encode()).digest()))
        self.s = new_state()
        self.sha: Optional[str] = None
        self.dirty = False
        self.important = False
        self.last_sync: Optional[datetime] = None
        self.last_try: Optional[datetime] = None
        self.dirty_since: Optional[datetime] = None
        self.error = ""
        self.conflicts = 0
        self.boot_id = uid("boot_")
        self.loaded = False

    # --- 직렬화
    def encode(self, s: Optional[dict] = None) -> bytes:
        raw = json.dumps(s if s is not None else self.s, ensure_ascii=False, separators=(",", ":")).encode()
        if self.fernet:
            return json.dumps({"enc": "fernet", "v": 1, "data": self.fernet.encrypt(raw).decode()}).encode()
        return raw

    def decode(self, raw: bytes) -> dict:
        obj = json.loads(raw)
        if isinstance(obj, dict) and obj.get("enc") == "fernet":
            if not self.fernet:
                raise RuntimeError("저장된 데이터가 암호화되어 있습니다. DATA_ENCRYPTION_KEY를 설정하세요.")
            obj = json.loads(self.fernet.decrypt(obj["data"].encode()))
        return obj

    # --- 불러오기
    def load(self) -> None:
        try:
            raw, sha = self.b.read(STATE_FILE)
            if raw:
                self.s = migrate(self.decode(raw))
                self.sha = sha
            self.loaded = True
            self.error = ""
        except Exception as e:  # 저장소에 접근하지 못하면 빈 상태로 덮어쓰지 않도록 기록만 한다
            self.error = f"불러오기 실패: {e}"
            self.loaded = False

    def read_inbox(self) -> list:
        """GitHub Actions가 남긴 알림(inbox.json)을 읽는다."""
        try:
            raw, _ = self.b.read(INBOX_FILE)
            return json.loads(raw) if raw else []
        except Exception:
            return []

    # --- 변경 표시
    def mark(self, important: bool = False) -> None:
        now = utcnow()
        self.s["rev"] = self.s.get("rev", 0) + 1
        self.s["updated_at"] = iso(now)
        if not self.dirty:
            self.dirty_since = now
        self.dirty = True
        self.important = self.important or important

    def due(self, now: datetime) -> bool:
        """지금 동기화할 시점인가? (GitHub는 실시간 DB가 아니므로 묶어서 커밋)"""
        if not self.dirty or not self.loaded:
            return False
        since = (now - self.last_try).total_seconds() if self.last_try else 1e9
        commits = self.s["usage"].get(now.strftime("%Y-%m-%d"), {}).get("commits", 0)
        if commits >= self.s["settings"].get("commits_per_day", 300):
            return since >= 600
        if self.error:
            return since >= 45  # 실패 후 재시도 간격
        return since >= (30 if self.important else 120)

    # --- 동기화 (동기 버전: 테스트와 종료 시 사용)
    def flush(self, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        if not self.loaded:
            self.error = self.error or "저장소를 불러오지 못해 저장을 보류했습니다."
            return False
        self.last_try = now
        for _ in range(3):
            payload, rev = self.encode(), self.s["rev"]
            try:
                sha = self._persist(payload, self.sha, self._snapshot_name(now))
            except Conflict:
                try:
                    self._merge_remote(*self.b.read(STATE_FILE))
                except Exception as e:
                    return self._fail(e)
                continue
            except Exception as e:
                return self._fail(e)
            return self._ok(sha, rev, now)
        return self._fail(RuntimeError("충돌이 반복되어 저장하지 못했습니다."))

    # --- 동기화 (비동기 버전: 네트워크만 스레드에서 실행)
    async def aflush(self, now: Optional[datetime] = None) -> bool:
        import asyncio
        now = now or utcnow()
        if not self.loaded:
            self.error = self.error or "저장소를 불러오지 못해 저장을 보류했습니다."
            return False
        self.last_try = now
        for _ in range(3):
            payload, rev = self.encode(), self.s["rev"]
            try:
                sha = await asyncio.to_thread(self._persist, payload, self.sha, self._snapshot_name(now))
            except Conflict:
                try:
                    raw, rsha = await asyncio.to_thread(self.b.read, STATE_FILE)
                    self._merge_remote(raw, rsha)
                except Exception as e:
                    return self._fail(e)
                continue
            except Exception as e:
                return self._fail(e)
            return self._ok(sha, rev, now)
        return self._fail(RuntimeError("충돌이 반복되어 저장하지 못했습니다."))

    def _snapshot_name(self, now: datetime) -> Optional[str]:
        d = now.strftime("%Y-%m-%d")
        return None if self.s["meta"].get("last_snapshot") == d else f"snapshots/{d}.json"

    def _persist(self, payload: bytes, sha: Optional[str], snapshot: Optional[str]) -> str:
        new_sha = self.b.write(STATE_FILE, payload, sha, "LIFE OS 상태 동기화")
        if snapshot:
            try:
                _, ssha = self.b.read(snapshot)
                self.b.write(snapshot, payload, ssha, "LIFE OS 일일 스냅샷")
                self._snap_done = snapshot
            except Exception:
                self._snap_done = None
        return new_sha

    def _merge_remote(self, raw, sha) -> None:
        self.conflicts += 1
        if raw:
            merged = merge(self.s, self.decode(raw))
            self.s.clear()
            self.s.update(merged)
            self.s["audit"].append({"id": uid("au_"), "ts": iso(utcnow()), "action": "sync.merge",
                                    "detail": "다른 기기의 변경과 충돌하여 레코드 단위로 병합했습니다."})
        self.sha = sha

    def _ok(self, sha: str, rev: int, now: datetime) -> bool:
        self.sha = sha
        self.last_sync = now
        self.error = ""
        snap = getattr(self, "_snap_done", None)
        if snap:
            self.s["meta"]["last_snapshot"] = snap.split("/")[-1][:-5]
            self._snap_done = None
        u = self.s["usage"].setdefault(now.strftime("%Y-%m-%d"), {})
        u["commits"] = u.get("commits", 0) + 1
        if self.s["rev"] == rev:
            self.dirty = False
            self.important = False
            self.dirty_since = None
        return True

    def _fail(self, e: Exception) -> bool:
        self.error = f"저장 실패: {e}"[:300]
        return False

    # --- 산출물 파일 (문서 등). 상태 파일과 별도로 저장소의 files/ 아래에 실제 파일로 기록한다.
    def write_file(self, path: str, data: bytes) -> str:
        payload = self.fernet.encrypt(data) if self.fernet else data
        _, sha = self.b.read(path)
        self.b.write(path, payload, sha, f"LIFE OS 산출물 {path}")
        return hashlib.sha256(data).hexdigest()

    def read_file(self, path: str) -> Optional[bytes]:
        raw, _ = self.b.read(path)
        if raw is None:
            return None
        return self.fernet.decrypt(raw) if self.fernet else raw

    # --- 스냅샷
    def snapshots(self) -> list[str]:
        return [n[:-5] for n in self.b.list("snapshots") if n.endswith(".json")]

    def restore(self, name: str) -> bool:
        raw, _ = self.b.read(f"snapshots/{name}.json")
        if not raw:
            return False
        snap = migrate(self.decode(raw))
        snap["rev"] = self.s["rev"] + 1
        snap["user"] = self.s["user"]  # 현재 비밀번호는 유지
        self.s.clear()
        self.s.update(snap)
        self.mark(important=True)
        return True

    def status(self, now: Optional[datetime] = None) -> dict:
        now = now or utcnow()
        if not self.b.durable:
            state = "LOCAL_ONLY"
        elif self.error:
            state = "ERROR"
        elif self.dirty:
            state = "PENDING"
        else:
            state = "SAVED"
        return {
            "state": state, "durable": self.b.durable, "backend": self.b.label,
            "encrypted": bool(self.fernet), "error": self.error, "conflicts": self.conflicts,
            "last_sync": iso(self.last_sync) if self.last_sync else None,
            "dirty_since": iso(self.dirty_since) if self.dirty and self.dirty_since else None,
            "last_snapshot": self.s["meta"].get("last_snapshot"),
        }


def state_age_seconds(s: dict, now: datetime) -> float:
    try:
        return (now - parse(s["updated_at"])).total_seconds()
    except Exception:
        return 0.0
