"""PART 20 시나리오 검증 — HTTP API (인증·CSRF·문서·회의·시뮬레이터·복구)."""
import io
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.maintenance import check
from app.store import LocalBackend, Store
from app.util import dadd, today, utcnow


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_CODE", "code-1234")
    store = Store(LocalBackend(str(tmp_path / "d")))
    app = create_app(store, run_loop=False)
    c = TestClient(app)
    c.store, c.engine = store, app.state.engine
    return c


def login(api):
    r = api.post("/api/setup", json={"password": "correct horse", "setup_code": "code-1234"})
    assert r.status_code == 200
    api.headers["X-CSRF-Token"] = r.json()["csrf"]
    return api


def drive(api, n=300):
    eng = api.engine
    for _ in range(n):
        eng.heartbeat(utcnow())
        if eng.tick(utcnow()) in ("idle", "limit"):
            break


def test_setup_and_auth(api):
    assert api.get("/api/me").json()["setup_needed"] is True
    assert api.get("/api/state").status_code == 401
    assert api.post("/api/setup", json={"password": "correct horse", "setup_code": "wrong"}).status_code == 403
    assert api.post("/api/setup", json={"password": "short", "setup_code": "code-1234"}).status_code == 422
    login(api)
    assert api.post("/api/setup", json={"password": "another one", "setup_code": "code-1234"}).status_code == 409
    st = api.get("/api/state").json()
    assert len(st["agents"]) == 12 and "user" not in st
    assert "password_hash" not in json.dumps(st)
    api.cookies.clear()
    assert api.get("/api/state").status_code == 401
    assert api.post("/api/login", json={"password": "nope nope"}).status_code == 401
    assert api.post("/api/login", json={"password": "correct horse"}).status_code == 200


def test_csrf_required(api):
    login(api)
    del api.headers["X-CSRF-Token"]
    assert api.post("/api/c/goals", json={"title": "x"}).status_code == 403


def test_login_rate_limit(api):
    login(api)
    codes = [api.post("/api/login", json={"password": "bad password"}).status_code for _ in range(8)]
    assert 429 in codes


def test_crud_validation(api):
    login(api)
    assert api.post("/api/c/events", json={"title": "x", "date": "2026-13-45"}).status_code == 422
    assert api.post("/api/c/events", json={"title": "x", "date": "2026-11-13", "start": "11:00", "end": "10:00"}).status_code == 422
    assert api.post("/api/c/unknown", json={}).status_code == 404
    assert api.post("/api/c/reports", json={}).status_code == 404
    r = api.post("/api/c/events", json={"id": "client-id-001", "title": "수업", "date": "2026-11-13", "evil": "<script>"})
    assert r.status_code == 200 and "evil" not in r.json()
    again = api.post("/api/c/events", json={"id": "client-id-001", "title": "다른 제목", "date": "2026-11-13"})
    assert again.json()["title"] == "수업"                    # 같은 ID 재전송은 중복 생성하지 않음
    assert api.put("/api/c/events/client-id-001", json={"done": True}).json()["version"] == 2
    assert api.post("/api/c/study_logs", json={"subject_id": "nope", "date": "2026-11-13"}).status_code == 422
    assert api.delete("/api/c/events/client-id-001").status_code == 200
    assert "client-id-001" in api.store.s["deleted"]


# ---- 테스트 10. AI 회의 (규칙 기반 협업 보고서)
def test_meeting_uses_real_data(api):
    login(api)
    td = today(api.store.s, utcnow())
    api.post("/api/c/events", json={"title": "수업", "date": td, "start": "10:00", "end": "12:00"})
    api.post("/api/c/events", json={"title": "멘토링", "date": td, "start": "11:00", "end": "13:00"})
    sub = api.post("/api/c/subjects", json={"title": "특수교육학", "total_units": 100, "done_units": 30}).json()
    api.post("/api/session/start", json={})
    m = api.post("/api/meetings", json={"title": "이번 주 학습 시간 배분", "participants": ["schedule", "study", "life"]}).json()
    assert "규칙 기반" in m["mode_label"] and "AI 회의 아님" in m["mode_label"]
    assert m["participants"][0] == "chief" and "일정" in m["sources"]
    by = {t["agent"]: " ".join(t["lines"]) for t in m["transcript"]}
    assert "1건" in by["schedule"] and "특수교육학" in by["study"] and "30%" in by["study"]
    kinds = {api.store.s["tasks"][a["task_id"]]["kind"] for a in m["action_items"]}
    assert {"schedule.conflicts", "study.review"} <= kinds     # 후속 업무가 실제 작업으로 저장됨
    assert api.store.s["c"]["meetings"][m["id"]]["conclusions"]
    assert not api.store.s["ai_jobs"]                           # AI가 없으면 AI 작업을 만들지 않음
    drive(api)
    assert all(api.store.s["tasks"][a["task_id"]]["status"] == "DONE" for a in m["action_items"])
    t = api.post("/api/documents/generate", json={"doc_type": "minutes", "meeting_id": m["id"]}).json()
    drive(api)
    doc = api.store.s["c"]["documents"]["doc_" + t["id"]]
    assert "결정 사항" in doc["content"] and sub["title"] in doc["content"]


# ---- 테스트 11. 실제 문서 제작 (파일을 내려받아 열 수 있는가)
def test_document_generated_and_opens(api):
    from docx import Document
    login(api)
    td = today(api.store.s, utcnow())
    api.post("/api/c/events", json={"title": "전공 수업", "date": td, "start": "10:00", "end": "12:00"})
    api.post("/api/session/start", json={})
    t = api.post("/api/documents/generate", json={"doc_type": "timetable"}).json()
    drive(api)
    task = api.store.s["tasks"][t["id"]]
    assert task["status"] == "DONE"
    doc_id = task["result"]["doc_id"]
    r = api.get(f"/api/documents/{doc_id}/export?fmt=docx")
    assert r.status_code == 200 and r.content[:2] == b"PK" and "attachment" in r.headers["content-disposition"]
    d = Document(io.BytesIO(r.content))                        # 실제로 열어서 내용 확인
    cells = [c.text for tb in d.tables for row in tb.rows for c in row.cells]
    assert "전공 수업" in cells and "10:00~12:00" in cells
    assert "전공 수업" in api.get(f"/api/documents/{doc_id}/export?fmt=md").text
    html = api.get(f"/api/documents/{doc_id}/export?fmt=html").text
    assert "<table>" in html and "전공 수업" in html
    assert api.get(f"/api/documents/{doc_id}/export?fmt=pdf").status_code == 422   # 구현하지 않은 형식은 거절
    daily = api.post("/api/documents/generate", json={"doc_type": "daily_report"}).json()
    drive(api)
    content = api.store.s["c"]["documents"]["doc_" + daily["id"]]["content"]
    assert "문서 작성 — 일정표" in content                      # 실제로 완료된 업무가 보고서에 들어감


def test_upload_validation_and_search(api):
    login(api)
    assert api.post("/api/upload", files={"file": ("a.exe", b"MZ", "application/octet-stream")}).status_code == 415
    assert api.post("/api/upload", files={"file": ("a.txt", b"\xff\xfe\x00bad", "text/plain")}).status_code == 415
    assert api.post("/api/upload", files={"file": ("big.txt", b"a" * 300_001, "text/plain")}).status_code == 413
    r = api.post("/api/upload", files={"file": ("노트.txt", "개별화교육계획 IEP 정리".encode(), "text/plain")})
    assert r.status_code == 200
    hits = api.get("/api/search", params={"q": "IEP"}).json()["hits"]
    assert hits and hits[0]["id"] == r.json()["id"]


def test_simulator(api):
    login(api)
    r = api.post("/api/simulate", json={"title": "임용", "total_units": 1000, "done_units": 400, "minutes_per_unit": 6,
                                        "review_ratio": 0.5, "weekly_available_hours": 10, "rest_days_per_week": 1,
                                        "deadline": dadd(today(api.store.s, utcnow()), 70)}).json()
    assert r["total_hours_needed"] == 90.0 and [p["name"] for p in r["plans"]] == ["안정형", "균형형", "집중형"]
    assert r["plans"][0]["weekly_hours"] < r["plans"][1]["weekly_hours"] < r["plans"][2]["weekly_hours"]
    assert "보장" in r["disclaimer"]
    tight = api.post("/api/simulate", json={"total_units": 1000, "minutes_per_unit": 60, "weekly_available_hours": 5,
                                            "deadline": today(api.store.s, utcnow())}).json()
    assert tight["plans"][0]["burden"].startswith("불가능")


def test_emergency_and_pause(api):
    login(api)
    api.post("/api/c/events", json={"title": "과제", "date": "2020-01-01", "type": "마감", "importance": 5})
    inc = {i["id"] for i in api.get("/api/emergency").json()["incidents"]}
    assert {"missed", "nostore"} <= inc
    api.post("/api/session/start", json={})
    assert "등록" in api.post("/api/emergency/action", json={"action": "reschedule"}).json()["message"]
    api.post("/api/emergency/action", json={"action": "pause"})
    assert api.engine.tick(utcnow()) == "paused"
    assert all(a["status"] == "PAUSED" for a in api.store.s["agents"].values())
    api.put("/api/settings", json={"paused": False})
    assert api.put("/api/settings", json={"max_steps_per_day": 999999}).status_code == 422


def test_permissions_only_by_user_and_bounded(api):
    login(api)
    api.post("/api/agents/study", json={"write": ["events", "user", "nonsense"], "auto_medium": True})
    p = api.store.s["agents"]["study"]["permissions"]
    assert p["write"] == ["events"] and p["auto_medium"] is True
    assert any(a["action"] == "agent.permission" for a in api.store.s["audit"])


def test_recover_from_browser_copy(api):
    login(api)
    api.post("/api/c/goals", json={"id": "goal-000001", "title": "서버에 있던 목표"})
    snap = api.get("/api/state").json()
    snap["c"].setdefault("goals", {})["goal-000002"] = {"id": "goal-000002", "title": "브라우저에만 남은 목표",
                                                        "updated_at": "2026-11-13T00:00:00Z"}
    snap["c"]["goals"]["goal-000001"] = {**snap["c"]["goals"]["goal-000001"], "title": "옛 제목",
                                         "updated_at": "2000-01-01T00:00:00Z"}
    snap["user"] = {"password_hash": "evil"}
    assert api.post("/api/recover", json={"state": snap}).status_code == 200
    g = api.store.s["c"]["goals"]
    assert g["goal-000002"]["title"] == "브라우저에만 남은 목표"
    assert g["goal-000001"]["title"] == "서버에 있던 목표"      # 더 최신인 서버 레코드 유지
    assert api.store.s["user"]["password_hash"].startswith("scrypt$")


def test_restore_needs_password(api):
    login(api)
    api.store.flush()
    name = api.get("/api/snapshots").json()["snapshots"][0]
    api.post("/api/c/goals", json={"id": "goal-000009", "title": "스냅샷 이후"})
    assert api.post("/api/snapshots/restore", json={"name": name, "password": "wrong pass"}).status_code == 403
    assert api.post("/api/snapshots/restore", json={"name": name, "password": "correct horse"}).status_code == 200
    assert "goal-000009" not in api.store.s["c"].get("goals", {})
    assert api.get("/api/state").status_code == 200            # 복구 후에도 로그인 유지


def test_actions_maintenance_script(tmp_path):
    st = Store(LocalBackend(str(tmp_path)))
    st.load()
    st.mark()
    st.flush()
    now = datetime.now(timezone.utc)
    kinds = {n["kind"] for n in check(str(tmp_path), "", now)}
    assert kinds == {"actions_check", "actions_backup"}
    (tmp_path / "state.json").write_text("{broken")
    assert any(n["kind"] == "integrity_error" for n in check(str(tmp_path), "", now))


def test_static_path_traversal(api):
    r = api.get("/../../etc/passwd")
    assert "root:" not in r.text
