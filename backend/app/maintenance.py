"""GitHub Actions에서 실행하는 정기 점검 스크립트.

데이터 브랜치를 체크아웃한 폴더를 인자로 받는다:  python -m app.maintenance <데이터 폴더>
- state.json 정합성(파싱·복호화·스키마 버전) 검사
- 스냅샷(백업) 유효성 검사
- 오래 대기 중인 작업 수 집계
결과는 inbox.json에 남기며, 다음 접속 때 대시보드의 '재접속 보고서'에 표시된다.
state.json은 절대 수정하지 않는다 (앱과의 충돌 방지).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone


def decode(raw: bytes, key: str) -> dict:
    obj = json.loads(raw)
    if isinstance(obj, dict) and obj.get("enc") == "fernet":
        if not key:
            return {"__encrypted__": True}
        from cryptography.fernet import Fernet
        f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
        obj = json.loads(f.decrypt(obj["data"].encode()))
    return obj


def check(data_dir: str, key: str, now: datetime) -> list[dict]:
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    day = now.strftime("%Y-%m-%d")
    notes = []

    def note(kind: str, message: str):
        notes.append({"id": f"{kind}:{day}", "ts": ts, "kind": kind, "message": message})

    path = os.path.join(data_dir, "state.json")
    if not os.path.exists(path):
        note("actions_info", "아직 저장된 데이터가 없습니다.")
        return notes
    try:
        s = decode(open(path, "rb").read(), key)
    except Exception as e:
        note("integrity_error", f"state.json을 읽지 못했습니다: {e}")
        return notes
    if s.get("__encrypted__"):
        note("actions_info", "데이터가 암호화되어 있어 형식만 확인했습니다 (Actions에 키가 없음).")
    else:
        missing = [k for k in ("schema_version", "tasks", "agents", "c") if k not in s]
        if missing:
            note("integrity_error", f"state.json에 필수 항목이 없습니다: {', '.join(missing)}")
        else:
            stale = [t for t in s["tasks"].values()
                     if t.get("status") in ("QUEUED", "BLOCKED", "WAITING_APPROVAL") and t.get("updated_at", ts)[:10] < day
                     and (now - datetime.strptime(t["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days >= 7]
            pend = sum(1 for a in s.get("approvals", {}).values() if a.get("status") == "PENDING")
            note("actions_check", f"정기 점검 완료 — 작업 {len(s['tasks'])}건, 7일 넘게 대기 중 {len(stale)}건, 승인 대기 {pend}건")
    snaps = sorted(f for f in os.listdir(os.path.join(data_dir, "snapshots"))) \
        if os.path.isdir(os.path.join(data_dir, "snapshots")) else []
    if not snaps:
        note("actions_backup", "스냅샷(백업)이 아직 없습니다.")
    else:
        try:
            decode(open(os.path.join(data_dir, "snapshots", snaps[-1]), "rb").read(), key)
            note("actions_backup", f"최신 스냅샷 {snaps[-1]} 읽기 확인 (총 {len(snaps)}개)")
        except Exception as e:
            note("integrity_error", f"최신 스냅샷 {snaps[-1]}을 읽지 못했습니다: {e}")
    return notes


def main() -> int:
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    notes = check(data_dir, os.environ.get("DATA_ENCRYPTION_KEY", ""), datetime.now(timezone.utc))
    inbox_path = os.path.join(data_dir, "inbox.json")
    try:
        inbox = json.load(open(inbox_path, encoding="utf-8"))
    except Exception:
        inbox = []
    ids = {n["id"] for n in notes}
    inbox = [i for i in inbox if i.get("id") not in ids] + notes
    with open(inbox_path, "w", encoding="utf-8") as f:
        json.dump(inbox[-60:], f, ensure_ascii=False, indent=1)
    for n in notes:
        print(f"[{n['kind']}] {n['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
