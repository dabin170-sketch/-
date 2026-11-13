"""에이전트 실행 엔진 시나리오 A~H 검증."""
import io

import pytest
from fastapi.testclient import TestClient

from app import tools as T
from app.main import create_app
from app.store import LocalBackend, Store
from app.tools import file_text
from app.util import dadd, utcnow, today
from app.workflow import digest
from conftest import TODAY, add, run


def wf_new(eng, clock, goal, **kw):
    r = eng.wf.new(goal, clock.now, "user", **kw)
    assert r["type"] == "workflow", r
    return r["workflow"]


def go(eng, clock, n=600):
    eng.start_session(clock.now) if not eng.active(clock.now) else None
    return run(eng, clock, max_ticks=n)


def pending_ap(store, wf):
    return [a for a in store.s["approvals"].values() if a.get("workflow_id") == wf["id"] and a["status"] == "PENDING"]


def step(wf, sid):
    return next(x for x in wf["plan"] if x["id"] == sid)


# ---------------------------------------------------------------- A. 프로젝트 검색 → 관련 할 일 생성
def test_A_project_search_and_todo_creation(eng, store, clock):
    add(store, "projects", id="p1", title="임용 스터디 발표", deadline="2026-11-23", status="진행")
    add(store, "project_tasks", id="pt0", project_id="p1", title="자료 조사", due="2026-11-10", done=False)
    add(store, "memories", id="m1", project_id="p1", kind="확정 사실", tags="규칙", content="할 일: 리허설, 질의응답 준비")
    add(store, "memories", id="m2", project_id="p1", kind="확정 사실", tags="규칙", content="모든 작업은 마감 2일 전 완료")
    wf = wf_new(eng, clock, "임용 스터디 발표 프로젝트 할 일 정리해줘: 역할 분담")
    assert wf["template"] == "project_todos" and wf["project_id"] == "p1"
    go(eng, clock)
    assert wf["status"] == "WAITING_APPROVAL"                         # 할 일 생성(MEDIUM) 전에 계획 승인
    assert not [t for t in store.s["c"]["project_tasks"].values() if t["id"] != "pt0"]
    ap = pending_ap(store, wf)[0]
    assert any("할 일 추가: 역할 분담" in d for d in ap["detail"])
    assert eng.resolve_approval(ap["id"], True, clock.now)
    go(eng, clock)
    assert wf["status"] == "DONE", wf["trace"][-3:]
    titles = {t["title"]: t for t in store.s["c"]["project_tasks"].values() if t["project_id"] == "p1"}
    assert {"역할 분담", "리허설", "질의응답 준비", "최종 점검", "「자료 조사」 일정 다시 잡기"} <= set(titles)
    assert titles["최종 점검"]["due"] == "2026-11-21"                  # 규칙 '마감 2일 전' 반영
    assert titles["역할 분담"]["by_agent"] == "project"
    assert step(wf, "s2")["inputs_from"] == ["s1"] and step(wf, "s3")["inputs_from"] == ["s1", "s2"]
    assert step(wf, "s3")["verify"]["passed"]
    assert [c["name"] for c in step(wf, "s3")["verify"]["checks"]] == ["DB 저장 확인", "중복 할 일 없음", "요청 항목 모두 처리"]
    phases = [t["phase"] for t in wf["trace"]]
    for ph in ("GOAL", "CONTEXT", "PLAN", "EXECUTE", "OBSERVE", "VERIFY", "COMPLETE"):
        assert ph in phases
    m = eng.perf("project")
    assert m["units_done"] >= 3 and m["produced"] == 5
    # 같은 명령을 다시 해도 중복 생성하지 않는다 (할 일이 모두 있으므로 생성 단계는 건너뜀)
    n = len(store.s["c"]["project_tasks"])
    wf2 = wf_new(eng, clock, "임용 스터디 발표 프로젝트 할 일 정리해줘: 역할 분담")
    go(eng, clock)
    assert wf2["status"] == "DONE" and len(store.s["c"]["project_tasks"]) == n
    assert step(wf2, "s3")["status"] == "SKIPPED" and not pending_ap(store, wf2)


# ---------------------------------------------------------------- B. 실제 파일 생성 → 검증 → 다운로드 링크
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_CODE", "code-1234")
    monkeypatch.setenv("CRON_TOKEN", "cron-secret-token")
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
        if eng.tick(utcnow()) in ("idle", "limit"):
            break


def test_B_document_file_verified_and_downloadable(api, tmp_path):
    from docx import Document
    td = today(api.store.s, utcnow())
    api.post("/api/c/events", json={"title": "특수교육학 수업", "date": td, "start": "10:00", "end": "12:00"})
    api.post("/api/session/start", json={})
    r = api.post("/api/workflows", json={"goal": "앞으로 일주일 일정표 문서 만들어줘"}).json()
    assert r["type"] == "workflow" and r["template"] == "document"
    drive(api)
    wf = api.store.s["workflows"][r["id"]]
    assert wf["status"] == "DONE", wf["trace"][-4:]
    s2 = step(wf, "s2")
    names = {c["name"]: c["passed"] for c in s2["verify"]["checks"]}
    assert names == {"실제 파일 존재": True, "파일 형식 검사": True, "문서 열기 가능": True, "필수 내용 포함": True}
    link = wf["result"]["links"][0]["url"]
    assert link == f"/api/files/{s2['output']['file_id']}" and wf["result"]["files"]
    assert (tmp_path / "d" / s2["output"]["file_id"].join(["files/", ".docx"])).exists()   # 저장소에 실제 파일
    res = api.get(link)
    assert res.status_code == 200 and res.content[:2] == b"PK"
    cells = [c.text for t in Document(io.BytesIO(res.content)).tables for row in t.rows for c in row.cells]
    assert "특수교육학 수업" in cells
    assert api.get("/api/files/nope").status_code == 404


# ---------------------------------------------------------------- C. 일정 충돌 발견 → 대안 제시
def test_C_conflict_found_and_alternatives(eng, store, clock):
    tomorrow = dadd(TODAY, 1)
    add(store, "events", id="e1", title="전공 수업", date=tomorrow, start="14:00", end="16:00")
    wf = wf_new(eng, clock, "내일 오후 3시에 팀 회의 1시간 잡아줘")
    assert wf["params"] == {"title": "팀 회의", "date": tomorrow, "start": "15:00", "duration_min": 60, "assumed": []}
    go(eng, clock)
    assert step(wf, "s2")["output"]["conflicts"][0]["title"] == "전공 수업"
    assert wf["status"] == "WAITING_INPUT"
    ap = pending_ap(store, wf)[0]
    assert ap["kind"] == "choice" and "겹침" in ap["detail"][0]
    alts = [o for o in ap["options"] if "set" in o["value"] and "start" in o["value"]["set"]]
    assert alts and all(not ("14:00" <= o["value"]["set"]["start"] < "16:00") for o in alts)
    assert {o["label"] for o in ap["options"]} >= {"겹쳐도 그대로 등록", "이 작업 취소"}
    assert len(store.s["c"]["events"]) == 1                          # 선택 전에는 저장하지 않음
    eng.resolve_approval(ap["id"], True, clock.now, choice=0)
    go(eng, clock)
    assert wf["status"] == "DONE"
    new = [e for e in store.s["c"]["events"].values() if e["title"] == "팀 회의"]
    assert len(new) == 1 and new[0]["start"] == alts[0]["value"]["set"]["start"]
    assert all(c["passed"] for c in step(wf, "s3")["verify"]["checks"])


def test_C2_existing_conflicts_resolved_by_choice(eng, store, clock):
    add(store, "events", id="e1", title="세미나", date=TODAY, start="13:00", end="15:00", importance=5)
    add(store, "events", id="e2", title="멘토링", date=TODAY, start="14:00", end="15:00", importance=2)
    wf = wf_new(eng, clock, "이번 주 일정 충돌 확인해줘")
    assert wf["template"] == "conflict_resolve"
    go(eng, clock)
    c = step(wf, "s2")["output"]["conflicts"][0]
    assert c["move"]["title"] == "멘토링" and c["alternatives"]       # 중요도가 낮은 쪽을 옮기자고 제안
    ap = pending_ap(store, wf)[0]
    eng.resolve_approval(ap["id"], True, clock.now, choice=0)
    go(eng, clock)
    assert wf["status"] == "DONE"
    e2 = store.s["c"]["events"]["e2"]
    alt = c["alternatives"][0]
    assert (e2["date"], e2["start"]) == (alt["date"], alt["start"])


# ---------------------------------------------------------------- D. 여러 직원의 산출물 연결
def test_D_collaboration_outputs_are_chained(eng, store, clock):
    add(store, "events", id="pe", title="특수교육 IEP 발표", date=dadd(TODAY, 4), start="10:00", end="11:00")
    add(store, "documents", id="d1", title="개별화교육계획 정리", type="업로드 자료", status="완료",
        content="개별화교육계획(IEP)은 특수교육대상학생 한 명 한 명을 위해 작성한다. IEP에는 현재 수행 수준과 교육 목표가 들어간다. "
                "교육 목표는 측정 가능해야 한다. 특수교육 지원팀이 IEP를 함께 만든다.")
    add(store, "documents", id="d2", title="통합교육 메모", type="업로드 자료", status="완료",
        content="통합교육은 특수교육대상학생이 일반학급에서 함께 배우는 것이다. 통합교육에서 IEP는 일반교사와 공유된다. "
                "협력교수는 통합교육의 핵심 방법이다.")
    add(store, "memories", id="r1", kind="확정 사실", tags="규칙", content="발표 슬라이드는 최대 6장", key="rule:max_slides")
    wf = wf_new(eng, clock, "이번 주 발표 준비해줘")
    go(eng, clock)
    assert wf["status"] == "DONE", [t["msg"] for t in wf["trace"][-5:]]
    assert [x["agent"] for x in wf["plan"]] == ["chief", "research", "study", "study", "docs", "docs", "system", "chief", "chief"]
    s1, s2, s3, s5, s6, s7 = (step(wf, k) for k in ("s1", "s2", "s3", "s5", "s6", "s7"))
    assert s1["output"]["date"] == dadd(TODAY, 4)
    assert {x["id"] for x in s2["output"]["sources"]} == {"d1", "d2"}
    # 앞 단계 산출물이 다음 단계의 실제 입력으로 쓰였는지: 입력 해시 == 앞 단계 출력 해시
    assert s3["input_digest"]["$s2.sources"] == digest(s2["output"]["sources"])
    assert s5["input_digest"]["$s3.concepts"] == digest(s3["output"]["concepts"])
    assert s6["input_digest"]["$s5.content"] == digest(s5["output"]["content"])
    assert s7["input_digest"]["$s6.file_id"] == digest(s6["output"]["file_id"])
    assert all(set(c["source_ids"]) <= {"d1", "d2"} for c in s3["output"]["concepts"])
    data = store.read_file(store.s["files"][s6["output"]["file_id"]]["path"])
    text = file_text(data, "docx")
    for c in s3["output"]["concepts"][:3]:
        assert c["term"] in text
    assert "개별화교육계획 정리" in text and dadd(TODAY, 4) in text
    assert s5["output"]["slides"] <= 6 and s7["verify"]["passed"]
    assert "발표 슬라이드 최대 6장" in wf["result"]["applied_rules"]
    assert step(wf, "s4")["output"]["mode"] == "rule_fallback"          # AI가 없으면 비AI 대체임을 표시
    items = step(wf, "s8")["output"]["items"]
    assert len(items) == 2 and all(i["date"] < dadd(TODAY, 4) for i in items)
    assert not [e for e in store.s["c"]["events"].values() if "리허설" in e["title"]]   # 제안만, 등록은 안 함


def test_D2_quality_check_triggers_revision(eng, store, clock, monkeypatch):
    add(store, "events", id="pe", title="통합교육 발표", date=dadd(TODAY, 3), start="10:00", end="11:00")
    add(store, "documents", id="d2", title="통합교육 메모", type="업로드 자료", status="완료",
        content="통합교육은 특수교육대상학생이 일반학급에서 함께 배우는 것이다. 협력교수는 통합교육의 핵심 방법이다. "
                "협력교수에는 여러 유형이 있다.")
    real = T.TOOLS["outline.build"]["fn"]
    calls = []

    def flaky(ctx, args):                       # 첫 구성에서 개념 하나를 빠뜨리는 상황을 만든다
        out = real(ctx, args)
        calls.append(args.get("fix"))
        if len(calls) == 1:
            drop = next(c["term"] for c in args["concepts"] if c["term"] not in out["title"])
            out["content"] = "\n".join(ln for ln in out["content"].split("\n") if drop not in ln)
            out["required"] = [r for r in out["required"] if r != drop]
        return out
    monkeypatch.setitem(T.TOOLS["outline.build"], "fn", flaky)
    wf = wf_new(eng, clock, "통합교육 발표 준비해줘")
    go(eng, clock)
    assert wf["status"] == "DONE"
    assert len(calls) == 2 and calls[1]                            # 오류·누락 검사 → s5부터 보충해 다시 만듦
    assert any(t["phase"] == "REVISE" and "s5 단계부터" in t["msg"] for t in wf["trace"])
    assert step(wf, "s7")["verify"]["passed"]


# ---------------------------------------------------------------- E. 실패 원인·복구 상태 기록
def test_E_failure_cause_and_recovery_recorded(eng, store, clock, monkeypatch):
    def boom(ctx, args):
        raise RuntimeError("데이터 파일 손상")
    real = T.TOOLS["doc.compose"]["fn"]
    monkeypatch.setitem(T.TOOLS["doc.compose"], "fn", boom)
    wf = wf_new(eng, clock, "주간 보고서 만들어줘")
    go(eng, clock)
    s1 = step(wf, "s1")
    assert wf["status"] == "FAILED" and s1["status"] == "FAILED"
    assert [e["cause"] for e in s1["errors"]] == ["exception", "exception"]    # 같은 오류 2번 → 무한 반복 없이 중단
    rec = wf["recovery"]
    assert rec["step"] == "s1" and rec["cause"] == "exception" and rec["resumable"] and "같은 오류가 반복됨" in rec["message"]
    assert eng.perf("docs")["units_failed"] == 1
    monkeypatch.setitem(T.TOOLS["doc.compose"], "fn", real)
    assert eng.wf.action(wf["id"], "retry", clock.now)
    go(eng, clock)
    assert wf["status"] == "DONE" and wf["recovery"] is None


def test_E2_transient_error_is_retried(eng, store, clock, monkeypatch):
    real = store.write_file
    n = []

    def once(path, data):
        n.append(path)
        if len(n) == 1:
            raise OSError("네트워크 끊김")
        return real(path, data)
    monkeypatch.setattr(store, "write_file", once)
    wf = wf_new(eng, clock, "이번 주 일정표 만들어줘")
    go(eng, clock)
    s2 = step(wf, "s2")
    assert wf["status"] == "DONE" and s2["errors"][0]["cause"] == "transient" and s2["attempts"] == 1
    assert any(t["phase"] == "REVISE" and "transient" in t["msg"] for t in wf["trace"])
    assert eng.perf("docs")["with_error"] == 1


# ---------------------------------------------------------------- F. 확정된 프로젝트 규칙을 다음 실행에서 정확히 검색
def test_F_project_rules_retrieved_and_latest_wins(eng, store, clock):
    add(store, "projects", id="pa", title="IEP 연구", status="진행")
    add(store, "projects", id="pb", title="통합교육 연구", status="진행")
    add(store, "memories", id="old", project_id="pa", kind="확정 사실", tags="규칙", key="rule:max_slides",
        content="발표 슬라이드는 최대 8장", updated_at="2026-11-01T00:00:00Z")
    add(store, "memories", id="new", project_id="pa", kind="확정 사실", tags="규칙", key="rule:max_slides",
        content="발표 슬라이드는 최대 5장", updated_at="2026-11-10T00:00:00Z")
    add(store, "memories", id="hyp", project_id="pa", kind="AI 가설", key="rule:max_slides",
        content="발표 슬라이드 12장이 좋아 보임", updated_at="2026-11-12T00:00:00Z")
    add(store, "memories", id="other", project_id="pb", kind="확정 사실", tags="규칙", content="발표 슬라이드는 최대 3장")
    # 사용자가 명령으로 새 규칙을 확정 → 다음 실행에서 검색되어야 한다
    rem = wf_new(eng, clock, "IEP 연구 프로젝트 규칙: 리허설 3회 기억해줘")
    go(eng, clock)
    assert rem["template"] == "remember" and rem["status"] == "DONE"
    saved = store.s["c"]["memories"][step(rem, "s1")["output"]["memory_id"]]
    assert saved["kind"] == "확정 사실" and saved["project_id"] == "pa" and saved["key"] == "rule:rehearsals"
    add(store, "documents", id="d1", title="IEP 자료", type="업로드 자료", status="완료",
        content="IEP 연구는 개별화교육계획의 효과를 다룬다. 개별화교육계획은 학생 맞춤 목표를 세운다. 연구 설계는 단일대상 설계를 쓴다.")
    add(store, "events", id="pe", title="IEP 연구 발표", date=dadd(TODAY, 6), start="10:00", end="11:00")
    wf = wf_new(eng, clock, "IEP 연구 발표 준비해줘")
    go(eng, clock)
    assert wf["status"] == "DONE"
    ctx = wf["context"]
    rule_ids = {r["id"] for r in ctx["rules"]}
    assert "new" in rule_ids and saved["id"] in rule_ids
    assert "old" not in rule_ids and "other" not in rule_ids and "hyp" not in {m["id"] for m in ctx["memories"]}
    sup = {x["id"]: x["reason"] for x in ctx["superseded"]}
    assert "더 최근 지시" in sup["old"] and "AI 가설" in sup["hyp"]
    assert ctx["rule_values"]["max_slides"] == 5 and ctx["rule_values"]["rehearsals"] == 3
    assert step(wf, "s5")["output"]["slides"] <= 5 and len(step(wf, "s8")["output"]["items"]) == 3
    # 다른 프로젝트에서는 pa 규칙이 섞이지 않는다
    wf_b = wf_new(eng, clock, "통합교육 연구 프로젝트 할 일 정리해줘: 문헌 정리")
    go(eng, clock)
    assert {r["id"] for r in wf_b["context"]["rules"]} == {"other"}


def test_F2_user_correction_becomes_next_default(api):
    td = today(api.store.s, utcnow())
    api.post("/api/session/start", json={})
    api.post("/api/agents/schedule", json={"auto_medium": True})
    r = api.post("/api/workflows", json={"goal": "내일 오후 2시 스터디 잡아줘"}).json()
    drive(api)
    wf = api.store.s["workflows"][r["id"]]
    eid = step(wf, "s3")["output"]["created"][0]
    ev = api.store.s["c"]["events"][eid]
    assert (ev["start"], ev["end"]) == ("14:00", "15:00")
    api.put(f"/api/c/events/{eid}", json={"end": "15:30"})            # 사용자가 직원이 만든 일정을 고침
    assert api.engine.perf("schedule")["corrected"] == 1
    st = api.get("/api/state").json()
    sch = next(a for a in st["agents"] if a["id"] == "schedule")
    assert sch["metrics"]["correction_rate"] == 100
    r2 = api.post("/api/workflows", json={"goal": "모레 오전 10시 스터디 잡아줘"}).json()
    wf2 = api.store.s["workflows"][r2["id"]]
    assert wf2["params"]["duration_min"] == 90                          # 수정 피드백이 다음 기본값이 됨


# ---------------------------------------------------------------- G. 브라우저를 닫아도 서버 예약 작업 실행
def test_G_server_scheduled_run_without_browser(api):
    api.post("/api/c/rules", json={"title": "매일 일정표", "action": "docs.generate:timetable", "every_days": 1,
                                   "run_when_closed": True})
    api.post("/api/c/rules", json={"title": "화면 있을 때만", "action": "finance.budget", "every_days": 1})
    assert api.engine.active(utcnow()) is None                          # 접속(브라우저) 없음
    no_auth = TestClient(api.app)
    assert no_auth.post("/api/cron/run").status_code == 403
    assert no_auth.post("/api/cron/run", headers={"X-Cron-Token": "wrong"}).status_code == 403
    r = no_auth.post("/api/cron/run", headers={"X-Cron-Token": "cron-secret-token"}).json()
    assert r["ran"] and r["created"] == 1 and len(r["done"]) == 1 and r["saved"]
    docs = list(api.store.s["c"]["documents"].values())
    assert len(docs) == 1 and docs[0]["title"].startswith("일정표")
    assert not [t for t in api.store.s["tasks"].values() if t["kind"] == "finance.budget"]   # 설정 안 한 규칙은 실행 안 함
    assert api.store.s["inbox"][-1]["kind"] == "server_run"
    again = no_auth.post("/api/cron/run", headers={"X-Cron-Token": "cron-secret-token"}).json()
    assert again["created"] == 0 and again["steps"] == 0                # 같은 날 두 번 실행하지 않음
    api.post("/api/session/start", json={})
    busy = no_auth.post("/api/cron/run", headers={"X-Cron-Token": "cron-secret-token"}).json()
    assert busy["ran"] is False


def test_G2_cron_disabled_without_token(tmp_path):
    import os
    os.environ.pop("CRON_TOKEN", None)
    c = TestClient(create_app(Store(LocalBackend(str(tmp_path / "x"))), run_loop=False))
    assert c.post("/api/cron/run", headers={"X-Cron-Token": ""}).status_code == 404


# ---------------------------------------------------------------- H. 고위험 작업은 승인 없이 수행하지 않음
def test_H_high_risk_needs_approval(eng, store, clock):
    store.s["agents"]["system"]["permissions"]["auto_medium"] = True
    add(store, "ideas", id="i1", title="오래된 메모", content="x")
    wf = wf_new(eng, clock, "「오래된 메모」 삭제해줘")
    go(eng, clock)
    assert wf["template"] == "delete_record" and wf["status"] == "WAITING_APPROVAL"
    ap = pending_ap(store, wf)[0]
    assert ap["risk_level"] == "HIGH" and ap["kind"] == "step"
    assert "i1" in store.s["c"]["ideas"]
    for _ in range(3):
        clock.tick(70)
        go(eng, clock)
    assert "i1" in store.s["c"]["ideas"] and len(pending_ap(store, wf)) == 1
    eng.resolve_approval(ap["id"], False, clock.now)
    go(eng, clock)
    assert wf["status"] == "STOPPED" and "i1" in store.s["c"]["ideas"]
    assert any("거절" in m["content"] for m in store.s["c"]["memories"].values())
    wf2 = wf_new(eng, clock, "「오래된 메모」 삭제해줘")
    go(eng, clock)
    eng.resolve_approval(pending_ap(store, wf2)[0]["id"], True, clock.now)
    go(eng, clock)
    assert wf2["status"] == "DONE" and "i1" not in store.s["c"]["ideas"]


# ---------------------------------------------------------------- 능동적 업무 발견 (제안 상태)
def test_proactive_proposals_only_run_after_acceptance(eng, store, clock):
    add(store, "events", id="pe", title="교육과정 발표", date=dadd(TODAY, 5), start="10:00", end="11:00")
    add(store, "events", id="dl", title="특수교육공학 과제 제출", date=dadd(TODAY, 2), type="마감")
    add(store, "writings", id="w1", title="선량한 사람들", target_count=30000, current_count=5000,
        deadline=dadd(TODAY, 4), status="집필 중")
    add(store, "subjects", id="s1", title="특수교육학", total_units=200, done_units=20, minutes_per_unit=20,
        exam_date=dadd(TODAY, 10), unit_label="쪽")
    go(eng, clock)
    props = {w["title"]: w for w in store.s["workflows"].values() if w["status"] == "PROPOSED"}
    assert set(props) == {"「교육과정 발표」 리허설 일정 잡기", "「특수교육공학 과제 제출」 참고 자료 조사",
                          "「선량한 사람들」 연재 원고 집필 시간 확보", "「특수교육학」 시험 대비 복습 시간 확보"}
    assert all(p["evidence"] for p in props.values())
    before = len(store.s["c"]["events"])
    for _ in range(3):
        clock.tick(70)
        go(eng, clock)
    assert len(store.s["c"]["events"]) == before                      # 수락 전에는 아무것도 실행하지 않음
    assert sum(1 for w in store.s["workflows"].values() if w["status"] == "PROPOSED") == 4   # 중복 제안 없음
    p = props["「교육과정 발표」 리허설 일정 잡기"]
    eng.wf.action(p["id"], "accept", clock.now)
    go(eng, clock)
    ap = pending_ap(store, p)[0]                                       # 수락 후에도 일정 등록 전 계획 승인
    eng.resolve_approval(ap["id"], True, clock.now)
    go(eng, clock)
    assert p["status"] == "DONE"
    reh = [e for e in store.s["c"]["events"].values() if "리허설" in e["title"]]
    assert len(reh) == 2 and all(e["date"] < dadd(TODAY, 5) for e in reh)


# ---------------------------------------------------------------- 로컬 AI 작업 라우팅·검증·캐시
def test_ai_routing_and_honest_labels(eng, store, clock):
    eng.start_session(clock.now, ai_ready=True, ai_models={"light": "small-1b", "strong": "big-3b"})
    wf = wf_new(eng, clock, "요즘 내 상황에 맞는 공부 조언 한 줄")
    go(eng, clock)
    job = next(j for j in store.s["ai_jobs"].values() if j["status"] == "pending")
    assert job["task_type"] == "answer" and job["tier"] == "strong" and job["model"] == "big-3b"
    assert eng.ai_result(job["id"], "자료가 부족해 일반적인 조언만 가능합니다. 복습 주기를 정해 보세요.", "", clock.now, "big-3b", 900)
    go(eng, clock)
    assert wf["status"] == "DONE" and step(wf, "s3")["output"]["mode"] == "ai"
    assert store.s["ai_stats"]["big-3b|answer"]["ok"] == 1
    # 경량 모델 성공률이 낮으면 요약 작업을 상위 모델로 보낸다
    store.s["ai_stats"]["small-1b|summary"] = {"id": "small-1b|summary", "model": "small-1b", "task_type": "summary",
                                               "n": 6, "ok": 2, "ms": 0, "updated_at": ""}
    tier, model, why = eng.wf.route("summary", eng.active(clock.now))
    assert tier == "strong" and "33%" in why
    # 모델이 없으면 답변 단계는 건너뛰고, 찾은 자료만 보고한다 (AI 답변인 척하지 않음)
    eng.heartbeat(clock.now, ai_ready=False)
    wf2 = wf_new(eng, clock, "요즘 내 상황에 맞는 공부 조언 두 줄")
    go(eng, clock)
    assert wf2["status"] == "DONE" and step(wf2, "s3")["status"] == "SKIPPED"
    assert "로컬 AI" in step(wf2, "s3")["skip_reason"]


def test_ai_interrupted_by_browser_close_recovers(eng, store, clock):
    add(store, "events", id="pe", title="IEP 발표", date=dadd(TODAY, 4), start="10:00", end="11:00")
    add(store, "documents", id="d1", title="IEP 자료", type="업로드 자료", status="완료",
        content="IEP는 개별화교육계획이다. IEP에는 교육 목표가 들어간다. 교육 목표는 측정 가능해야 한다.")
    eng.start_session(clock.now, ai_ready=True, ai_models={"light": "small-1b"})
    wf = wf_new(eng, clock, "IEP 발표 준비해줘")
    go(eng, clock)
    assert wf["status"] == "WAITING_AI" and step(wf, "s4")["status"] == "WAITING_AI"
    eng.end_session(clock.tick())                                       # 브라우저 종료 → AI 작업 중단
    clock.tick(3600)
    eng.start_session(clock.now)                                        # 재접속 (AI 꺼짐)
    go(eng, clock)
    s4 = step(wf, "s4")
    assert wf["status"] == "DONE" and s4["errors"][0]["cause"] == "ai_interrupted"
    assert s4["output"]["mode"] == "rule_fallback" and "AI 아님" in s4["output"]["model"]


def test_tool_catalog_all_real_functions():
    names = {t["name"] for t in T.catalog()}
    need = {"project.search", "event.query", "event.create", "task.create", "doc.search", "doc.create", "file.read",
            "file.modify", "web.research", "memory.search", "memory.save", "stats.compute", "db.query", "approval.request"}
    assert need <= names
    assert all(T.TOOLS[n]["verify"] for n in names)


def test_web_research_with_mock_api(eng, store, clock):
    import httpx

    def handler(req):
        p = dict(req.url.params)
        if p.get("list") == "search":
            return httpx.Response(200, json={"query": {"search": [{"title": "개별화교육계획"}, {"title": "개별화교육계획"}]}})
        return httpx.Response(200, json={"query": {"pages": {"1": {"title": "개별화교육계획", "extract": "개별화교육계획은 ...",
                                                                     "fullurl": "https://ko.wikipedia.org/wiki/x",
                                                                     "revisions": [{"timestamp": "2026-10-01T00:00:00Z"}]}}}})
    T.HTTP_CLIENT = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        wf = {"id": "wfx", "plan": []}
        st = {"id": "s1", "agent": "research", "scratch": {}}
        ctx = T.TCtx(eng, wf, st, clock.now)
        with pytest.raises(T.ToolError) as e:
            T.t_web_research(ctx, {"keywords": ["IEP"]})
        assert e.value.cause == "disabled"                              # 기본은 꺼져 있음
        store.s["settings"]["web_research"] = True
        out = T.t_web_research(ctx, {"keywords": ["IEP"]})
        checks = {c["name"]: c["passed"] for c in T.TOOLS["web.research"]["verify"](ctx, {}, out)}
        assert checks == {"실제 출처 존재": True, "정보 기준 시각 기록": True, "중복 자료 제거": True}
        assert out["sources"][0]["last_modified"] == "2026-10-01T00:00:00Z"
    finally:
        T.HTTP_CLIENT = None
