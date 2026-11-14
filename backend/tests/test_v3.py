"""LIFE OS 3.0 — PHASE 0~3 검증 (기존 데이터 보존, 정책, 검수, 수집함, 업무 발견, 통합 검색, 지휘실, 진행 중 수정)."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from app import policy as POL
from app import tools as T
from app import verify as VF
from app.engine import Engine
from app.intake import extract_deadlines
from app.main import create_app
from app.search import search
from app.store import LocalBackend, Store
from app.util import dadd, today, utcnow
from conftest import T0, TODAY, add, run


def go(eng, clock, n=600):
    if not eng.active(clock.now):
        eng.start_session(clock.now)
    return run(eng, clock, max_ticks=n)


def step(wf, sid):
    return next(x for x in wf["plan"] if x["id"] == sid)


def pending(store, wf):
    return [a for a in store.s["approvals"].values() if a.get("workflow_id") == wf["id"] and a["status"] == "PENDING"]


def make_pdf(lines):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("HYGothic-Medium", 12)
    for i, ln in enumerate(lines):
        c.drawString(50, 800 - 20 * i, ln)
    c.save()
    return buf.getvalue()


# ================================================================ PHASE 0 — 기존 데이터 보존
def test_v2_state_is_preserved(tmp_path, clock):
    """v2 형식(3.0의 새 컬렉션이 없는) 상태 파일을 3.0 코드로 열어도 기존 데이터가 그대로여야 한다."""
    v2 = {"schema_version": 1, "rev": 41, "updated_at": "2026-11-12T00:00:00Z",
          "user": {"id": "owner", "password_hash": "scrypt$14$8$1$x$y", "created_at": "", "updated_at": ""},
          "settings": {"tz_offset_min": 540, "daily_hour_limit": 9, "updated_at": ""},
          "agents": {}, "tasks": {"t_old": {"id": "t_old", "kind": "chief.briefing", "status": "DONE", "title": "옛 브리핑",
                                            "assigned_agent": "chief", "idempotency_key": "k", "updated_at": "2026-11-10T00:00:00Z",
                                            "created_at": "2026-11-10T00:00:00Z", "completed_at": "2026-11-10T00:00:00Z",
                                            "source": "auto", "evidence": ["x"], "result": {"summary": "s", "lines": []}}},
          "approvals": {"ap_old": {"id": "ap_old", "task_id": "t_old", "status": "APPROVED", "action": "옛 승인",
                                   "updated_at": "2026-11-10T00:00:00Z"}},
          "ai_jobs": {}, "workflows": {"wf_old": {"id": "wf_old", "title": "옛 워크플로", "status": "DONE", "plan": [],
                                                  "goal": "x", "created_at": "2026-11-11T00:00:00Z", "updated_at": "2026-11-11T00:00:00Z",
                                                  "trace": [], "idempotency_key": "w", "source": "user", "phase": "COMPLETE",
                                                  "lead": "chief", "template": "answer"}},
          "files": {}, "ai_stats": {},
          "c": {"events": {"e_old": {"id": "e_old", "title": "기존 수업", "date": TODAY, "start": "10:00", "end": "12:00",
                                     "updated_at": "2026-11-01T00:00:00Z"}},
                "memories": {"m_old": {"id": "m_old", "content": "기존 기억", "agent_id": "all", "updated_at": "2026-11-01T00:00:00Z"}},
                "meetings": {"mt_old": {"id": "mt_old", "title": "기존 회의", "transcript": [], "updated_at": "2026-11-01T00:00:00Z"}}},
          "task_logs": [], "sessions": [], "audit": [], "history": [], "inbox": [{"id": "a1", "kind": "actions_check", "message": "점검"}],
          "usage": {}, "idem": {}, "deleted": {}, "meta": {}}
    d = tmp_path / "d"
    d.mkdir()
    (d / "state.json").write_text(json.dumps(v2, ensure_ascii=False))
    st = Store(LocalBackend(str(d)))
    st.load()
    from app.agents import init_agents
    init_agents(st.s, T0)
    st.s["agents"]["chief"]["experience"] = 77
    eng = Engine(st)
    go(eng, clock)
    s = st.s
    assert s["user"]["password_hash"] == "scrypt$14$8$1$x$y" and s["settings"]["daily_hour_limit"] == 9
    assert s["c"]["events"]["e_old"]["title"] == "기존 수업" and s["c"]["memories"]["m_old"]["content"] == "기존 기억"
    assert s["c"]["meetings"]["mt_old"]["title"] == "기존 회의" and s["approvals"]["ap_old"]["status"] == "APPROVED"
    assert s["tasks"]["t_old"]["title"] == "옛 브리핑" and s["workflows"]["wf_old"]["title"] == "옛 워크플로"
    assert s["agents"]["chief"]["experience"] >= 77 and s["inbox"][0]["id"] == "a1"
    assert s["verifications"] == {} or all(isinstance(v, dict) for v in s["verifications"].values())
    assert "intake" in s and isinstance(s["intake"], dict)
    assert st.flush(clock.now)
    st2 = Store(LocalBackend(str(d)))
    st2.load()
    assert st2.s["c"]["events"]["e_old"]["title"] == "기존 수업"


# ================================================================ PHASE 1 — 정책 엔진·검수 기록
def test_policy_engine_decisions(store):
    s = store.s
    tl = T.TOOLS
    assert POL.decide(s, "nobody", tl["event.create"])["decision"] == "deny"
    assert POL.decide(s, "study", tl["event.create"])["decision"] == "blocked"          # 학습 코치는 일정 쓰기 권한 없음
    assert POL.decide(s, "schedule", tl["event.create"])["decision"] == "scoped_approval"
    assert POL.decide(s, "schedule", tl["event.create"], scope_approved=True)["decision"] == "allow"
    assert POL.decide(s, "docs", tl["doc.create"])["decision"] == "allow"               # 사용자가 켜 둔 자동 실행 권한
    s["agents"]["system"]["permissions"]["auto_medium"] = True
    assert POL.decide(s, "system", tl["record.delete"], scope_approved=True)["decision"] == "individual_approval"
    assert POL.decide(s, "system", tl["record.delete"], approved_step=True)["decision"] == "allow"
    assert POL.decide(s, "chief", tl["memory.search"])["decision"] == "allow"


def test_verification_runs_recorded_and_recheck_detects_damage(eng, store, clock):
    add(store, "events", title="수업", date=TODAY, start="10:00", end="12:00")
    wf = eng.wf.new("이번 주 일정표 만들어줘", clock.now)["workflow"]
    go(eng, clock)
    assert wf["status"] == "DONE"
    runs = [v for v in store.s["verifications"].values() if v["workflow_id"] == wf["id"]]
    assert runs and runs[0]["artifact_type"] == "document" and runs[0]["status"] == "passed"
    doc_id = step(wf, "s2")["output"]["doc_id"]
    assert VF.verify_artifact(eng, "document", doc_id, clock.now)["status"] == "passed"
    f = store.s["files"][step(wf, "s2")["output"]["file_id"]]
    store.b.write(f["path"], b"broken bytes", store.b.read(f["path"])[1], "손상 시험")   # 저장된 파일이 손상된 상황
    bad = VF.verify_artifact(eng, "document", doc_id, clock.now)
    assert bad["status"] == "failed"
    errs = " ".join(c["error_message"] for c in bad["checks"] if not c["passed"])
    assert "손상" in errs
    assert len([v for v in store.s["verifications"].values() if v["artifact_id"] == doc_id]) >= 3   # 검수 기록 누적 보존


def test_recheck_event_detects_new_conflict(eng, store, clock):
    add(store, "events", id="e1", title="세미나", date=TODAY, start="10:00", end="11:00")
    assert VF.verify_artifact(eng, "event", "e1", clock.now)["status"] == "passed"
    add(store, "events", id="e2", title="상담", date=TODAY, start="10:30", end="11:30")
    run_ = VF.verify_artifact(eng, "event", "e1", clock.now)
    assert run_["status"] == "failed" and any(c["check_type"] == "시간 충돌 없음" and not c["passed"] for c in run_["checks"])
    store.s["c"]["events"]["e2"]["date"] = "2026-13-40"
    assert any(c["check_type"] == "날짜 유효성" and not c["passed"] for c in VF.verify_artifact(eng, "event", "e2", clock.now)["checks"])


# ================================================================ PHASE 2 — AI 수집함
def test_intake_pdf_to_approved_event(eng, store, clock):
    add(store, "projects", id="p1", title="특수교육공학", status="진행")
    pdf = make_pdf(["특수교육공학 과제 안내", "제출 마감: 2026년 11월 20일 오후 6시", "분량: A4 3쪽, 참고문헌 포함"])
    it = eng.intake.add(clock.now, name="과제공지.pdf", data=pdf)
    assert it["processing_status"] == "queued" and it["storage_reference"]
    assert eng.intake.process_next(clock.now)
    assert it["processing_status"] == "classified", it["error"]
    assert "제출 마감" in it["extracted_text"] and it["classification"]["category"] == "과제"
    assert it["project_id"] == "p1"
    dl = [x for x in it["extractions"] if x["extraction_type"] == "deadline"][0]
    assert (dl["value"], dl["time"], dl["evidence_location"]) == ("2026-11-20", "18:00", "2번째 줄") and dl["confidence"] >= 0.9
    kinds = {c["action_type"]: c for c in it["candidates"]}
    assert set(kinds) == {"create_event", "create_project_task"}
    assert not [e for e in store.s["c"].get("events", {}).values() if "마감" in e["title"]]     # 승인 전에는 반영 안 함
    ev = eng.intake.decide(it["id"], kinds["create_event"]["id"], True, None, clock.now)
    assert ev["approval_status"] == "applied" and ev["verification"] == "passed"
    coll, rid = ev["applied_record"].split("/")
    assert store.s["c"]["events"][rid]["date"] == "2026-11-20" and store.s["c"]["events"][rid]["start"] == "18:00"
    pt = eng.intake.decide(it["id"], kinds["create_project_task"]["id"], True, None, clock.now)
    assert store.s["c"]["project_tasks"][pt["applied_record"].split("/")[1]]["project_id"] == "p1"
    with pytest.raises(Exception):
        eng.intake.decide(it["id"], kinds["create_event"]["id"], True, None, clock.now)          # 같은 후보를 두 번 적용하지 않음
    assert store.s["c"]["documents"][it["document_id"]]["type"] == "업로드 자료"                   # 검색용 내부 문서로 보관


def test_intake_low_confidence_date_corrected_by_user(eng, store, clock):
    it = eng.intake.add(clock.now, text="동아리 회의 메모\n다음 모임은 12/3 쯤 할 예정")
    eng.intake.process_next(clock.now)
    c = it["candidates"][0]
    assert it["processing_status"] == "needs_review" and c["approval_status"] == "needs_review" and c["confidence"] < 0.7
    done = eng.intake.decide(it["id"], c["id"], True, {"date": "2026-12-04", "start": "19:00"}, clock.now)
    rid = done["applied_record"].split("/")[1]
    assert store.s["c"]["events"][rid]["date"] == "2026-12-04" and done["user_edited"]["date"] == ["2026-12-03", "2026-12-04"]
    bad = eng.intake.add(clock.now, text="세미나 2026-12-10 마감")
    eng.intake.process_next(clock.now)
    with pytest.raises(Exception) as e:
        eng.intake.decide(bad["id"], bad["candidates"][0]["id"], True, {"date": "2026-02-30"}, clock.now)
    assert "날짜" in str(e.value)


def test_intake_duplicates_and_unsupported(eng, store, clock):
    a = eng.intake.add(clock.now, name="공지.txt", data="교육공학 과제 제출 마감 2026-11-25".encode())
    eng.intake.process_next(clock.now)
    again = eng.intake.add(clock.now, name="공지 사본.txt", data="교육공학 과제 제출 마감 2026-11-25".encode())
    assert again["duplicate"] and again["id"] == a["id"] and len(store.s["intake"]) == 1      # 같은 파일 → 같은 항목
    other = eng.intake.add(clock.now, text="교육공학 과제 제출 마감 2026-11-25\n(조교 공지 다시 보냄)")
    eng.intake.process_next(clock.now)
    assert other["candidates"][0]["approval_status"] == "duplicate"                              # 같은 후속 작업은 중복 생성 안 함
    img = eng.intake.add(clock.now, name="사진.png", data=b"\x89PNG\r\n\x1a\n" + b"0" * 50)
    eng.intake.process_next(clock.now)
    assert img["processing_status"] == "unsupported" and "OCR" in img["error"]
    for name, data, why in [("a.exe", b"MZ....", "실행"), ("가짜.png", b"hello", "위장"), ("x.zip", b"PK..", "지원하지 않는 형식"),
                            ("js.pdf", b"%PDF-1.4 /JavaScript (app.alert)", "스크립트")]:
        with pytest.raises(Exception) as e:
            eng.intake.add(clock.now, name=name, data=data)
        assert why in str(e.value)
    broken = eng.intake.add(clock.now, name="깨진.pdf", data=b"%PDF-1.4\n garbage without objects")
    eng.intake.process_next(clock.now)
    assert broken["processing_status"] == "error" and "PDF" in broken["error"]


def test_intake_survives_restart(tmp_path, clock):
    st = Store(LocalBackend(str(tmp_path / "d")))
    st.load()
    eng = Engine(st)
    it = eng.intake.add(clock.now, name="공지.txt", data="시험 범위 공지 기말고사 2026-12-15".encode())
    assert st.flush(clock.now)
    st2 = Store(LocalBackend(str(tmp_path / "d")))                                               # 분석 전에 서버 재시작
    st2.load()
    eng2 = Engine(st2)
    assert eng2.intake.process_next(clock.now)
    it2 = st2.s["intake"][it["id"]]
    assert it2["processing_status"] in ("classified", "needs_review") and it2["classification"]["category"] == "시험"


def test_deadline_extractor_edges():
    xs = extract_deadlines("2월 30일 마감\n제출: 11.20(금)\n과제 제출 기한 12/1 23:59까지", "2026-11-14")
    vals = {x["value"]: x for x in xs}
    assert "2026-02-30" not in vals and vals["2026-12-01"]["time"] == "23:59" and vals["2026-12-01"]["extraction_type"] == "deadline"


# ================================================================ PHASE 2 — AI가 발견한 일
def test_discoveries_actions_and_suppression(eng, store, clock):
    add(store, "projects", id="p1", title="졸업 논문", deadline=dadd(TODAY, 20), status="진행")
    add(store, "events", id="pe", title="교육과정 발표", date=dadd(TODAY, 5), start="10:00", end="11:00")
    go(eng, clock)
    props = {w["title"]: w for w in store.s["workflows"].values() if w["status"] == "PROPOSED"}
    p = props["「졸업 논문」 다음 할 일 정하기"]
    d = p["discovery"]
    assert d["detection_rule"] == "프로젝트 진행 중 + 실행할 하위 작업 없음" and d["agent"] == "project" and d["est_minutes"] == 10
    r = props["「교육과정 발표」 리허설 일정 잡기"]
    assert r["discovery"]["urgency"] == "보통" and r["discovery"]["importance"] == "높음"
    assert eng.wf.action(r["id"], "add_todo", clock.now)
    todo = store.s["c"]["events"][r["added_todo"]]
    assert todo["type"] == "할일" and todo["title"] == r["title"]
    assert eng.wf.action(p["id"], "never", clock.now)
    assert "project:p1" in store.s["meta"]["suppressed"]
    clock.tick(8 * 86400)                                                                         # 다음 주가 되어도
    go(eng, clock)
    assert not [w for w in store.s["workflows"].values() if w["status"] == "PROPOSED" and "졸업 논문" in w["title"]]


def test_discovery_snooze(api):
    td = today(api.store.s, utcnow())
    api.post("/api/c/projects", json={"title": "포트폴리오", "deadline": dadd(td, 30)})
    api.post("/api/session/start", json={})
    drive(api)
    ds = api.get("/api/discoveries").json()["discoveries"]
    wid = next(w["id"] for w in ds if "포트폴리오" in w["title"])
    assert api.post(f"/api/discoveries/{wid}/snooze").status_code == 200
    after = api.get("/api/discoveries").json()
    assert wid not in [w["id"] for w in after["discoveries"]] and after["snoozed"] == 1
    assert api.post(f"/api/discoveries/{wid}/explode").status_code == 400


# ================================================================ PHASE 2 — 내 인생 검색
def test_personal_search_sources_filter_and_deletion(eng, store, clock):
    add(store, "projects", id="p1", title="IEP 연구", status="진행")
    add(store, "documents", id="d1", title="IEP 정리", content="개별화교육계획 IEP는 학생 맞춤 목표를 담는다.", project_id="p1")
    add(store, "documents", id="d2", title="통합교육 메모", content="IEP 공유 방법", project_id="")
    add(store, "events", id="e1", title="IEP 회의", date=TODAY)
    add(store, "writings", id="w1", title="선량한 사람들", characters="주인공 민준은 IEP 회의에 참석한다")
    add(store, "memories", id="m1", content="IEP 관련 비밀 메모", sensitivity="민감")
    r = search(store.s, "IEP")
    types = {x["type"] for x in r["results"]}
    assert {"projects", "documents", "events", "writings"} <= types and "memories" not in types   # 민감한 기억은 기본 제외
    top = r["results"][0]
    assert top["location"]["id"] and top["source"] and "IEP" in (top["title"] + top["snippet"])
    only = search(store.s, "IEP", project_id="p1")
    assert {x["id"] for x in only["results"]} == {"p1", "d1"}
    del store.s["c"]["documents"]["d1"]
    assert "d1" not in {x["id"] for x in search(store.s, "IEP")["results"]}                      # 삭제 즉시 반영
    assert search(store.s, "존재하지않는낱말")["results"] == []


# ================================================================ PHASE 3 — 업무 지휘실·진행 중 수정
def presentation_setup(store):
    add(store, "events", id="pe", title="IEP 발표", date=dadd(TODAY, 4), start="10:00", end="11:00")
    add(store, "documents", id="d1", title="IEP 자료", type="업로드 자료", status="완료",
        content="IEP는 개별화교육계획이다. IEP에는 현재 수행 수준과 교육 목표가 들어간다. 교육 목표는 측정 가능해야 한다. "
                "특수교육 지원팀이 IEP를 함께 만든다. 협력교수는 통합교육 방법이다. 전환교육은 졸업 이후를 준비한다.")


def test_mission_dedupe_pause_resume(eng, store, clock):
    presentation_setup(store)
    eng.start_session(clock.now)
    a = eng.wf.new("IEP 발표 준비해줘", clock.now)
    b = eng.wf.new("IEP  발표 준비해줘", clock.now)
    assert a["type"] == "workflow" and b["type"] == "existing" and b["workflow"]["id"] == a["workflow"]["id"]
    wf = a["workflow"]
    run(eng, clock, max_ticks=12, until_idle=False)
    assert eng.wf.action(wf["id"], "pause", clock.now) and wf["status"] == "PAUSED"
    before = [x["status"] for x in wf["plan"]]
    run(eng, clock, max_ticks=30)
    assert [x["status"] for x in wf["plan"]] == before                                          # 정지 중에는 진행 안 함
    assert eng.wf.action(wf["id"], "resume", clock.now)
    go(eng, clock)
    assert wf["status"] == "DONE"


def test_revise_running_mission_reruns_only_affected_steps(eng, store, clock):
    presentation_setup(store)
    eng.start_session(clock.now)
    wf = eng.wf.new("IEP 발표 준비해줘", clock.now)["workflow"]
    for _ in range(200):                                                                         # 발표자료 파일까지 만들어진 시점
        eng.heartbeat(clock.now)
        eng.tick(clock.tick())
        if wf["plan"] and step(wf, "s6")["status"] == "DONE":
            break
    old_files = [f["file_id"] for f in step(wf, "s6")["output"]["files"]]
    s2_digest = step(wf, "s2")["input_digest"]
    rev = eng.wf.revise(wf["id"], "슬라이드는 최대 4장으로 줄여줘. 워드 파일만 있으면 돼", clock.now)
    assert rev["new_version"] == 2 and rev["status"] == "applied"
    assert set(rev["affected_steps"]) == {"s3", "s4", "s5", "s6", "s7", "s8", "s9"}               # s1·s2 산출물은 그대로 사용
    assert set(rev["previous_outputs"]) >= {"s3", "s5", "s6"} and rev["previous_outputs"]["s6"]["files"] == old_files
    assert step(wf, "s1")["status"] == "DONE" and step(wf, "s2")["input_digest"] == s2_digest
    go(eng, clock)
    assert wf["status"] == "DONE" and wf["plan_version"] == 2
    assert step(wf, "s5")["output"]["slides"] <= 4
    new_files = step(wf, "s6")["output"]["files"]
    assert [f["fmt"] for f in new_files] == ["docx"]
    assert all(fid in store.s["files"] for fid in old_files)                                    # 이전 결과물 보존
    assert any(t["phase"] == "REVISE" and "v1→v2" in t["msg"] for t in wf["trace"])


def test_revise_schedule_resets_approval_and_unknown_is_recorded(eng, store, clock):
    eng.start_session(clock.now)
    wf = eng.wf.new("내일 오후 2시 상담 잡아줘", clock.now)["workflow"]
    go(eng, clock)
    assert wf["status"] == "WAITING_APPROVAL"
    old_ap = pending(store, wf)[0]
    rev = eng.wf.revise(wf["id"], "오후 4시로 바꿔줘", clock.now)
    assert old_ap["status"] == "REJECTED" and "s2" in rev["affected_steps"]                      # 바뀐 계획은 다시 승인
    go(eng, clock)
    ap = pending(store, wf)[0]
    assert any("16:00" in d for d in ap["detail"])
    eng.resolve_approval(ap["id"], True, clock.now)
    go(eng, clock)
    ev = store.s["c"]["events"][step(wf, "s3")["output"]["created"][0]]
    assert ev["start"] == "16:00" and wf["status"] == "DONE"
    with pytest.raises(ValueError):
        eng.wf.revise(wf["id"], "아무거나", clock.now)                                             # 끝난 작업은 수정 불가
    wf2 = eng.wf.new("모레 오전 9시 스터디 잡아줘", clock.now)["workflow"]
    go(eng, clock)
    rec = eng.wf.revise(wf2["id"], "상냥한 말투로 해줘", clock.now)
    assert rec["status"] == "recorded_only" and rec["memory_id"] in store.s["c"]["memories"] and not rec["affected_steps"]


# ================================================================ API 통합
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_CODE", "code-1234")
    store = Store(LocalBackend(str(tmp_path / "d")))
    app = create_app(store, run_loop=False)
    c = TestClient(app)
    c.store, c.engine = store, app.state.engine
    r = c.post("/api/setup", json={"password": "correct horse", "setup_code": "code-1234"})
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return c


def drive(api, n=600):
    eng = api.engine
    for _ in range(n):
        eng.heartbeat(utcnow())
        eng.intake.process_next(utcnow())
        if eng.tick(utcnow()) in ("idle", "limit"):
            break


def test_api_intake_search_verify_revise(api):
    pdf = make_pdf(["교육평가 과제 공지", "제출 마감 2026년 12월 2일", "주제: 형성평가 사례"])
    r = api.post("/api/intake/items", files={"file": ("평가과제.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    assert api.post("/api/intake/items", files={"file": ("a.exe", b"MZ", "application/octet-stream")}).status_code == 422
    assert api.post("/api/intake/items", data={"text": "  "}).status_code == 422
    drive(api)
    it = api.get(f"/api/intake/items/{iid}").json()
    assert it["processing_status"] == "classified" and it["candidates"]
    c = it["candidates"][0]
    assert api.post(f"/api/intake/items/{iid}/candidates/{c['id']}", json={"approve": True}).json()["approval_status"] == "applied"
    hits = api.get("/api/personal-search", params={"q": "형성평가"}).json()
    assert {h["type"] for h in hits["results"]} >= {"documents", "intake"}
    vr = api.get("/api/verifications").json()["runs"]
    assert any(v["artifact_type"] == "event" and v["status"] == "passed" for v in vr)
    ev_id = c and api.store.s["intake"][iid]["candidates"][0]["applied_record"].split("/")[1]
    assert api.post("/api/verifications", json={"artifact_type": "event", "artifact_id": ev_id}).json()["status"] == "passed"
    assert api.post("/api/verifications", json={"artifact_type": "nope", "artifact_id": "x"}).status_code == 422
    api.post("/api/session/start", json={})
    w = api.post("/api/workflows", json={"goal": "내일 오후 3시 면담 잡아줘"}).json()
    again = api.post("/api/workflows", json={"goal": "내일 오후 3시 면담 잡아줘"}).json()
    assert again["type"] == "existing" and again["id"] == w["id"]
    drive(api)
    rv = api.post(f"/api/workflows/{w['id']}/revise", json={"request": "오후 5시로"}).json()
    assert rv["new_version"] == 2
    st = api.get("/api/state").json()
    assert st["verify_summary"]["total"] >= 2 and "intake_summary" in st


def test_inbox_tools_registered_and_verified(eng, store, clock):
    it = eng.intake.add(clock.now, text="공모전 접수 마감 2026-12-20\n주제: 장애 인식 개선 영상")
    eng.intake.process_next(clock.now)
    wf, st = {"id": "wfx"}, {"id": "s1", "agent": "research", "scratch": {}}
    ctx = T.TCtx(eng, wf, st, clock.now)
    for name in ("inbox.read", "inbox.classify", "inbox.extract_deadlines", "inbox.find_related_projects", "inbox.propose_tasks"):
        out = T.TOOLS[name]["fn"](ctx, {"item_id": it["id"]})
        assert all(c["passed"] for c in T.TOOLS[name]["verify"](ctx, {}, out)), name
    assert T.TOOLS["inbox.classify"]["fn"](ctx, {"item_id": it["id"]})["category"] == "공모전"
