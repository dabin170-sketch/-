"""PART 20 시나리오 검증 — 엔진·저장 계층."""
import base64
import json
from datetime import timedelta

import httpx

from app.agents import init_agents
from app.engine import Engine
from app.store import GitHubBackend, LocalBackend, Store
from app.util import iso
from conftest import T0, TODAY, Clock, add, run


# ---- 테스트 1. 자동 출근
def test_auto_checkin_and_queue(eng, store, clock):
    add(store, "events", title="수업", date=TODAY, start="10:00", end="12:00", type="일정")
    assert all(a["status"] == "OFFLINE" for a in store.s["agents"].values())
    eng.start_session(clock.now)
    assert all(a["status"] == "CHECKING_IN" for a in store.s["agents"].values())
    assert eng.tick(clock.tick()) == "checkin"
    assert all(a["status"] == "IDLE" for a in store.s["agents"].values())
    assert eng.tick(clock.tick()) == "discovered"          # 사용자 명령 없이 업무를 찾음
    assert eng.tick(clock.tick()) == "analyzing"
    assert eng.tick(clock.tick()) == "queued"
    queued = [t for t in store.s["tasks"].values() if t["status"] == "QUEUED"]
    assert queued and all(0 <= t["priority"] <= 100 and t["priority_factors"] for t in queued)
    assert eng.tick(clock.tick()) == "step"
    working = [a for a in store.s["agents"].values() if a["status"] == "WORKING"]
    assert len(working) == 1                                 # 실행 중인 직원만 WORKING


def test_no_session_no_work(eng, store, clock):
    add(store, "events", title="수업", date=TODAY, start="10:00", end="12:00")
    assert eng.tick(clock.tick()) == "no-session"
    assert not store.s["tasks"]


# ---- 테스트 2. 실제 업무 수행 (일정 충돌)
def test_schedule_conflict_found_and_saved(eng, store, clock):
    add(store, "events", id="e1", title="전공 수업", date=TODAY, start="10:00", end="12:00")
    add(store, "events", id="e2", title="멘토링", date=TODAY, start="11:30", end="13:00")
    add(store, "events", id="e3", title="저녁 약속", date=TODAY, start="18:00", end="19:00")
    eng.start_session(clock.now)
    run(eng, clock)
    t = next(t for t in store.s["tasks"].values() if t["kind"] == "schedule.conflicts")
    assert t["status"] == "DONE"
    assert len(t["result"]["conflicts"]) == 1
    assert {t["result"]["conflicts"][0]["a"], t["result"]["conflicts"][0]["b"]} == {"e1", "e2"}
    rep = store.s["c"]["reports"]["rep_" + t["id"]]
    assert "전공 수업" in rep["lines"][0] and "멘토링" in rep["lines"][0]
    assert t["review"]["passed"] and store.s["agents"]["schedule"]["experience"] > 0


def test_no_conflict_no_task(eng, store, clock):
    add(store, "events", title="수업", date=TODAY, start="10:00", end="12:00")
    add(store, "events", title="점심", date=TODAY, start="12:00", end="13:00")
    eng.start_session(clock.now)
    run(eng, clock)
    assert not [t for t in store.s["tasks"].values() if t["kind"] == "schedule.conflicts"]


# ---- 테스트 3. 자율 업무 생성 (근거 기록)
def test_autonomous_discovery_with_evidence(eng, store, clock):
    add(store, "goals", title="임용 합격", area="학습", importance=5, status="진행")
    add(store, "subjects", id="s1", title="특수교육학", total_units=300, done_units=0, review_interval_days=3,
        unit_label="쪽")
    add(store, "study_logs", subject_id="s1", date="2026-11-05", units=40, kind="진도")
    eng.start_session(clock.now)
    run(eng, clock)
    t = next(t for t in store.s["tasks"].values() if t["kind"] == "study.review")
    assert t["source"] == "auto:study.review" and t["status"] == "DONE"
    assert any("2026-11-05" in e for e in t["evidence"])    # 근거 데이터 기록
    assert any("복습" in ln for ln in t["result"]["lines"])
    imp = next(f for f in t["priority_factors"] if f["name"] == "목표 중요도")
    assert imp["points"] == 20                               # 학습 목표 중요도 5 반영


def test_nothing_invented_when_no_data(eng, store, clock):
    eng.start_session(clock.now)
    run(eng, clock)
    kinds = {t["kind"] for t in store.s["tasks"].values()}
    assert kinds == {"chief.briefing", "system.health"}       # 매일 점검 2건 외에는 지어내지 않음


# ---- 테스트 4. 중복 실행 방지
def test_no_duplicate_tasks(eng, store, clock):
    add(store, "events", id="e1", title="A", date=TODAY, start="10:00", end="12:00")
    add(store, "events", id="e2", title="B", date=TODAY, start="11:00", end="13:00")
    eng.start_session(clock.now)
    run(eng, clock)
    n = len(store.s["tasks"])
    steps = eng.usage(clock.now)["steps"]
    for _ in range(5):                                        # 탐색을 여러 번 더 돌려도
        clock.tick(61)
        assert eng.heartbeat(clock.now)
        run(eng, clock)
    assert len(store.s["tasks"]) == n                         # 같은 업무가 다시 생기지 않고
    assert eng.usage(clock.now)["steps"] == steps             # 다시 실행되지도 않음
    keys = [t["idempotency_key"] for t in store.s["tasks"].values()]
    assert len(keys) == len(set(keys))


# ---- 테스트 5. 작업 복구 (체크포인트)
def test_recover_from_checkpoint(tmp_path, clock):
    backend = LocalBackend(str(tmp_path / "d"))
    store = Store(backend)
    store.load()
    init_agents(store.s, T0)
    add(store, "events", id="e1", title="A", date=TODAY, start="10:00", end="12:00")
    add(store, "events", id="e2", title="B", date=TODAY, start="11:00", end="13:00")
    eng = Engine(store)
    eng.start_session(clock.now)
    while True:                                               # 어떤 작업이 2단계까지 끝난 시점에서 '브라우저 종료'
        eng.heartbeat(clock.now)
        eng.tick(clock.tick())
        mid = [t for t in store.s["tasks"].values() if t["status"] == "RUNNING" and t["checkpoint"]["step"] == 2]
        if mid:
            break
    tid = mid[0]["id"]
    assert store.flush(clock.now)

    store2 = Store(backend)                                   # 서버 재시작 + 한참 뒤 재접속
    store2.load()
    eng2 = Engine(store2)
    clock.tick(3600)
    ses = eng2.start_session(clock.now)
    t = store2.s["tasks"][tid]
    assert t["title"] in ses["report"]["recovered"]
    assert t["status"] == "QUEUED" and t["checkpoint"]["step"] == 2
    run(eng2, clock)
    assert t["status"] == "DONE"
    done_steps = [l for l in store2.s["task_logs"] if l["task_id"] == tid and l["event_type"] == "STEP"]
    assert len(done_steps) == len(t["steps"])                 # 1·2단계는 다시 실행하지 않고 3단계만 실행
    assert any(l["event_type"] == "RESUMED" for l in store2.s["task_logs"] if l["task_id"] == tid)


# ---- 테스트 6. Render 재시작 후 데이터 보존 (GitHub 저장) + 충돌 병합
class FakeGitHub:
    def __init__(self):
        self.files, self.branch, self.commits = {}, False, 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.split("/repos/me/life-os", 1)[1]
        if path.startswith("/branches/"):
            return httpx.Response(200 if self.branch else 404, json={})
        if path in ("/git/trees", "/git/commits"):
            return httpx.Response(201, json={"sha": "x" * 40})
        if path == "/git/refs":
            self.branch = True
            return httpx.Response(201, json={})
        name = path.split("/contents/", 1)[1]
        if req.method == "GET":
            if name in self.files:
                data, sha = self.files[name]
                return httpx.Response(200, json={"content": base64.b64encode(data).decode(), "sha": sha, "size": len(data)})
            kids = [{"name": k.split("/")[-1]} for k in self.files if k.startswith(name + "/")]
            return httpx.Response(200, json=kids) if kids else httpx.Response(404, json={})
        body = json.loads(req.content)
        cur = self.files.get(name)
        if (cur and body.get("sha") != cur[1]) or (not cur and body.get("sha")):
            return httpx.Response(409, json={"message": "conflict"})
        self.commits += 1
        sha = f"sha{self.commits:037d}"
        self.files[name] = (base64.b64decode(body["content"]), sha)
        return httpx.Response(200, json={"content": {"sha": sha}})


def gh_store(fake, key=""):
    st = Store(GitHubBackend("tok", "me/life-os", client=httpx.Client(transport=httpx.MockTransport(fake.handler))), key)
    st.load()
    return st


def test_persist_across_restart_github(clock):
    fake = FakeGitHub()
    st = gh_store(fake, key="my-secret-passphrase")
    assert st.loaded and st.status()["state"] == "SAVED"
    init_agents(st.s, T0)
    add(st, "goals", id="g1", title="임용 합격")
    st.mark(important=True)
    assert st.status()["state"] == "PENDING"                 # 저장 전에는 '저장 완료'가 아님
    assert st.flush(clock.now) and st.status()["state"] == "SAVED"
    assert fake.branch and b"\xec\x9e\x84" not in fake.files["state.json"][0]   # 암호화되어 평문이 없음
    assert f"snapshots/{clock.now:%Y-%m-%d}.json" in fake.files

    st2 = gh_store(fake, key="my-secret-passphrase")         # 서버 재시작 (메모리·디스크 초기화)
    assert st2.s["c"]["goals"]["g1"]["title"] == "임용 합격"
    assert st2.snapshots() == [f"{clock.now:%Y-%m-%d}"]

    st3 = gh_store(fake, key="")                              # 키 없이 열면 빈 상태로 덮어쓰지 않고 중단
    assert not st3.loaded and not st3.flush(clock.now)


def test_save_failure_is_not_reported_as_saved(clock):
    def down(req):
        raise httpx.ConnectError("network down")
    fake = FakeGitHub()
    st = gh_store(fake)
    add(st, "goals", id="g1", title="x")
    st.mark()
    st.b.http = httpx.Client(transport=httpx.MockTransport(down))
    assert not st.flush(clock.now)
    assert st.status()["state"] == "ERROR" and st.dirty
    st.b.http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    assert st.flush(clock.tick(60)) and st.status()["state"] == "SAVED"   # 재시도 성공


def test_conflict_merge_between_devices(clock):
    fake = FakeGitHub()
    a = gh_store(fake)
    init_agents(a.s, T0)
    a.mark()
    assert a.flush(clock.now)
    b = gh_store(fake)                                        # 다른 기기
    add(a, "goals", id="ga", title="A기기 목표")
    a.mark()
    add(b, "goals", id="gb", title="B기기 목표")
    b.mark()
    assert a.flush(clock.now)
    assert b.flush(clock.now) and b.conflicts == 1            # SHA 불일치 감지 → 병합 후 재시도
    assert set(b.s["c"]["goals"]) == {"ga", "gb"}
    assert set(gh_store(fake).s["c"]["goals"]) == {"ga", "gb"}


# ---- 테스트 7. AI 모델 미지원
def test_rules_work_without_ai_and_never_fake(eng, store, clock):
    add(store, "expenses", date=TODAY, amount=90000, kind="지출", category="식비")
    add(store, "budgets", category="식비", limit=100000)
    eng.start_session(clock.now, ai_ready=False)
    run(eng, clock)
    fin = next(t for t in store.s["tasks"].values() if t["kind"] == "finance.budget")
    assert fin["status"] == "DONE" and "90,000" in " ".join(fin["result"]["lines"])
    # 규칙으로 해석할 수 없는 지시는 AI가 없으면 '완료'가 아니라 '대기'가 되어야 한다
    t = eng.add_task("user.instruction", "지시", "writing", "user:x", clock.now, ["사용자 지시"], "user",
                     params={"text": "주인공의 심리를 한 문단으로 묘사해줘"})
    clock.tick(1)
    run(eng, clock)
    assert t["status"] == "BLOCKED" and t["blocked_reason"].startswith("AI")
    assert store.s["agents"]["writing"]["status"] == "BLOCKED"
    # 키워드로 해석되는 지시는 AI 없이 실제 업무로 연결된다
    t2 = eng.add_task("user.instruction", "지시", "finance", "user:y", clock.now, ["사용자 지시"], "user",
                      params={"text": "이번 달 예산 점검해줘"})
    clock.tick(1)
    run(eng, clock)
    assert t2["status"] == "DONE" and store.s["tasks"][t2["result"]["child"]]["kind"] == "finance.budget"


def test_ai_job_roundtrip(eng, store, clock):
    eng.start_session(clock.now, ai_ready=True)
    run(eng, clock)
    t = eng.add_task("user.instruction", "지시", "writing", "user:x", clock.now, ["사용자 지시"], "user",
                     params={"text": "주인공의 심리를 한 문단으로 묘사해줘"})
    run(eng, clock, max_ticks=10, until_idle=False)
    job = next(j for j in store.s["ai_jobs"].values() if j["status"] == "pending")
    assert t["status"] == "RUNNING" and t["waiting_ai"] == job["id"]
    assert "<자료>" in job["prompt"] and "데이터로만" in job["system"]   # 프롬프트 인젝션 방어 지침
    assert eng.ai_result(job["id"], "자료 없음", "", clock.now)
    run(eng, clock)
    assert t["status"] == "DONE" and t["result"]["ai"] is True
    assert not eng.ai_result(job["id"], "again", "", clock.now)           # 같은 결과를 두 번 받지 않음


# ---- 테스트 8. 승인 시스템
def test_risky_step_needs_approval(eng, store, clock):
    add(store, "events", id="e1", title="과제 제출", date="2026-11-10", type="마감", done=False)
    eng.start_session(clock.now)
    run(eng, clock)
    t = next(t for t in store.s["tasks"].values() if t["kind"] == "schedule.overdue")
    assert t["status"] == "WAITING_APPROVAL"
    assert store.s["c"]["events"]["e1"]["date"] == "2026-11-10"            # 승인 전에는 바뀌지 않음
    assert store.s["agents"]["schedule"]["status"] == "WAITING_APPROVAL"
    ap = next(a for a in store.s["approvals"].values() if a["task_id"] == t["id"])
    assert ap["risk_level"] == "MEDIUM" and ap["detail"]
    for _ in range(5):
        clock.tick(61)
        assert eng.heartbeat(clock.now)
        run(eng, clock)
    assert t["status"] == "WAITING_APPROVAL" and len(store.s["approvals"]) == 1   # 반복 요청·무단 실행 없음
    assert eng.resolve_approval(ap["id"], True, clock.now)
    run(eng, clock)
    assert t["status"] == "DONE" and store.s["c"]["events"]["e1"]["date"] == TODAY


def test_rejected_approval_stops(eng, store, clock):
    add(store, "events", id="e1", title="과제 제출", date="2026-11-10", type="할일", done=False)
    eng.start_session(clock.now)
    run(eng, clock)
    ap = next(iter(store.s["approvals"].values()))
    eng.resolve_approval(ap["id"], False, clock.now)
    run(eng, clock)
    assert store.s["tasks"][ap["task_id"]]["status"] == "STOPPED"
    assert store.s["c"]["events"]["e1"]["date"] == "2026-11-10"


def test_high_risk_always_needs_approval_even_with_auto_medium(eng, store, clock):
    store.s["agents"]["system"]["permissions"]["auto_medium"] = True
    old = iso(T0 - timedelta(days=40))
    for i in range(25):
        store.s["tasks"][f"old{i}"] = {"id": f"old{i}", "status": "DONE", "updated_at": old, "idempotency_key": f"k{i}",
                                       "assigned_agent": "chief", "kind": "chief.briefing", "title": "x",
                                       "completed_at": old, "created_at": old, "source": "auto"}
    eng.start_session(clock.now)
    run(eng, clock)
    t = next(t for t in store.s["tasks"].values() if t["kind"] == "system.cleanup")
    assert t["status"] == "WAITING_APPROVAL" and "old0" in store.s["tasks"]
    ap = next(a for a in store.s["approvals"].values() if a["task_id"] == t["id"])
    assert ap["risk_level"] == "HIGH"
    eng.resolve_approval(ap["id"], True, clock.now)
    run(eng, clock)
    assert t["status"] == "DONE" and "old0" not in store.s["tasks"]


def test_permission_removed_blocks(eng, store, clock):
    store.s["agents"]["docs"]["permissions"]["write"] = []
    t = eng.add_task("docs.generate", "문서", "docs", "user:d", clock.now, ["요청"], "user", params={"doc_type": "timetable"})
    eng.start_session(clock.now)
    run(eng, clock)
    assert t["status"] == "BLOCKED" and t["blocked_reason"].startswith("권한")
    assert "documents" not in store.s["c"]
    store.s["agents"]["docs"]["permissions"]["write"] = ["documents"]
    clock.tick(1)
    run(eng, clock)
    assert t["status"] == "DONE"


# ---- 테스트 9. 무료 한도
def test_limit_stops_safely(eng, store, clock):
    store.s["settings"]["max_tasks_per_session"] = 1
    add(store, "events", id="e1", title="A", date=TODAY, start="10:00", end="12:00")
    add(store, "events", id="e2", title="B", date=TODAY, start="11:00", end="13:00")
    eng.start_session(clock.now)
    out = run(eng, clock)
    assert out[-1] == "limit"
    st = [t["status"] for t in store.s["tasks"].values()]
    assert st.count("DONE") == 1 and st.count("QUEUED") == len(st) - 1     # 나머지는 실패가 아니라 대기
    assert "한도" in eng.active(clock.now)["limit"]
    assert not [a for a in store.s["agents"].values() if a["status"] == "WORKING"]
    store.s["settings"]["max_tasks_per_session"] = 40                       # 사용자가 한도를 올리면 이어서 실행
    run(eng, clock)
    assert all(t["status"] == "DONE" for t in store.s["tasks"].values())


def test_ai_daily_limit_blocks(eng, store, clock):
    store.s["settings"]["ai_daily_limit"] = 0
    eng.start_session(clock.now, ai_ready=True)
    t = eng.add_task("user.instruction", "지시", "creative", "user:x", clock.now, ["지시"], "user", params={"text": "시 써줘"})
    run(eng, clock)
    assert t["status"] == "BLOCKED" and "한도" in t["blocked_reason"] and not store.s["ai_jobs"]


# ---- 미접속 기간 처리 / 오류 재시도
def test_absence_skips_expired_and_reports(eng, store, clock):
    eng.start_session(clock.now)
    eng.tick(clock.tick())
    eng.tick(clock.tick())                                    # 오늘 한정 업무 생성까지만 하고 접속 종료
    eng.end_session(clock.tick())
    assert all(a["status"] == "OFFLINE" for a in store.s["agents"].values())
    clock.tick(3 * 86400)
    ses = eng.start_session(clock.now)
    assert ses["report"]["gap_days"] == 3 and len(ses["report"]["skipped"]) == 2
    old = [t for t in store.s["tasks"].values() if t["idempotency_key"].endswith(TODAY)]
    assert all(t["status"] == "STOPPED" for t in old)        # 실행하지 않은 업무를 완료로 표시하지 않음
    run(eng, clock)
    assert sum(1 for t in store.s["tasks"].values() if t["status"] == "DONE") == 2


def test_heartbeat_timeout_ends_session(eng, store, clock):
    eng.start_session(clock.now)
    eng.tick(clock.tick())
    clock.tick(200)                                           # heartbeat 없이 시간이 흐름 (탭이 닫힘)
    assert eng.tick(clock.now) == "no-session"
    assert store.s["sessions"][-1]["status"] == "ENDED"


def test_failure_retries_then_fails(eng, store, clock, monkeypatch):
    from app import rules

    def boom(c):
        raise RuntimeError("테스트 오류")
    steps = rules.RULES["system.health"]["steps"]
    monkeypatch.setitem(rules.RULES["system.health"], "steps", [steps[0], ("분석", boom, "LOW"), steps[2]])
    eng.start_session(clock.now)
    run(eng, clock)
    t = next(t for t in store.s["tasks"].values() if t["kind"] == "system.health")
    assert t["status"] == "FAILED" and t["retry_count"] == 3 and t["checkpoint"]["step"] == 1
    assert store.s["agents"]["system"]["stats"]["failed"] == 1
    assert store.s["agents"]["system"]["experience"] == 0     # 실패한 업무에는 경험치 없음
