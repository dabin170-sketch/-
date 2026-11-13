"""LIFE OS 백엔드 (FastAPI). API + 빌드된 프론트엔드를 한 서비스에서 제공한다."""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import analysis as A
from . import docs as D
from . import features as F
from .agents import DEFS, LEVELS, init_agents, level_of, trust_of
from .engine import Engine
from .rules import RULES, rule_actions
from .tools import catalog as tool_catalog
from . import memory as MEM
from .schema import AGENT_IDS, COLLECTIONS, ID_RE, Invalid, validate
from .security import RateLimiter, csrf_for, hash_password, make_token, read_token, verify_password
from .store import GitHubBackend, LocalBackend, Store, merge, migrate
from .util import iso, today, uid, utcnow

COOKIE = "lifeos_session"
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
TICK_SECONDS = 0.7


def build_store() -> Store:
    token, repo = os.environ.get("LIFEOS_GITHUB_TOKEN", ""), os.environ.get("LIFEOS_GITHUB_REPO", "")
    if token and repo:
        backend = GitHubBackend(token, repo, os.environ.get("LIFEOS_DATA_BRANCH", "lifeos-data"))
    else:
        backend = LocalBackend(os.environ.get("LIFEOS_LOCAL_DIR", "./.lifeos-data"))
    return Store(backend, os.environ.get("DATA_ENCRYPTION_KEY", ""))


# ---------------------------------------------------------------- 요청 모델
class PasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=200)
    setup_code: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    password: str = Field(max_length=200)


class SessionIn(BaseModel):
    ai_ready: bool = False
    ai_models: Optional[dict[str, str]] = None


class WorkflowIn(BaseModel):
    goal: str = Field(min_length=1, max_length=1000)
    agent_id: Optional[str] = None
    project_id: Optional[str] = None


class WorkflowActionIn(BaseModel):
    action: str
    rating: Optional[int] = None


class InstructionIn(BaseModel):
    agent_id: str
    text: str = Field(min_length=1, max_length=1000)
    importance: int = Field(default=2, ge=0, le=3)


class TaskActionIn(BaseModel):
    action: str
    rating: Optional[int] = None


class ApprovalIn(BaseModel):
    approve: bool = True
    choice: Optional[int] = None


class AgentIn(BaseModel):
    paused: Optional[bool] = None
    write: Optional[list[str]] = None
    auto_medium: Optional[bool] = None


class MeetingIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    type: str = Field(default="사용자 요청 회의", max_length=40)
    participants: list[str] = Field(max_length=12)
    use_ai: bool = False


class GenerateIn(BaseModel):
    doc_type: str
    meeting_id: Optional[str] = None


class AIResultIn(BaseModel):
    text: str = Field(default="", max_length=8000)
    error: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    ms: int = Field(default=0, ge=0, le=10_000_000)


class EmergencyIn(BaseModel):
    action: str


class RestoreIn(BaseModel):
    name: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    password: str = Field(max_length=200)


class RecoverIn(BaseModel):
    state: dict[str, Any]


def create_app(store: Optional[Store] = None, run_loop: bool = True) -> FastAPI:
    store = store or build_store()
    store.load()
    engine = Engine(store)
    secret = os.environ.get("SECRET_KEY") or "dev-only-secret-change-me"
    setup_code = os.environ.get("SETUP_CODE", "")
    cron_token = os.environ.get("CRON_TOKEN", "")
    secure_cookie = os.environ.get("LIFEOS_INSECURE_COOKIE", "") != "1"
    limiter = RateLimiter()

    async def loop():
        last = utcnow()
        bg_last = [last]
        while True:
            await asyncio.sleep(TICK_SECONDS)
            now = utcnow()
            try:
                if store.loaded:
                    engine.usage(now)["server_sec"] += min(5, (now - last).total_seconds())
                    if engine.tick(now) == "no-session" and (now - bg_last[0]).total_seconds() >= 60:
                        bg_last[0] = now
                        engine.background_run(now, reason="서버 예약 실행(화면 닫힘)")
                    if store.due(now):
                        await store.aflush(now)
                elif (now - (store.last_try or last)).total_seconds() > 30:
                    store.last_try = now
                    await asyncio.to_thread(store.load)  # 저장소 접속 재시도
            except Exception as e:  # 루프가 죽지 않도록
                store.error = f"엔진 오류: {e}"[:300]
            last = now

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(loop()) if run_loop else None
        yield
        if task:
            task.cancel()
        if store.dirty and store.loaded:
            store.flush()

    app = FastAPI(title="LIFE OS", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.engine = store, engine

    # ------------------------------------------------------------ 미들웨어
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
            if not limiter.allow("all:" + ip, 300):
                return JSONResponse({"detail": "요청이 너무 많습니다. 잠시 후 다시 시도하세요."}, status_code=429)
            if path in ("/api/login", "/api/setup") and not limiter.allow("login:" + ip, 6):
                return JSONResponse({"detail": "로그인 시도가 너무 많습니다. 1분 후 다시 시도하세요."}, status_code=429)
            if int(request.headers.get("content-length") or 0) > 3_000_000:
                return JSONResponse({"detail": "요청이 너무 큽니다."}, status_code=413)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def pw_marker() -> str:
        return (store.s.get("user") or {}).get("password_hash", "")

    def session_of(request: Request):
        tok = request.cookies.get(COOKIE)
        return read_token(secret, tok, pw_marker()) if tok and store.s.get("user") else None

    def auth(request: Request):
        ses = session_of(request)
        if not ses:
            raise HTTPException(401, "로그인이 필요합니다.")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-csrf-token") != csrf_for(secret, ses["sid"]):
                raise HTTPException(403, "CSRF 토큰이 올바르지 않습니다.")
        if not store.loaded:
            raise HTTPException(503, "저장소를 불러오지 못했습니다: " + store.error)
        return ses

    def set_cookie(resp: Response):
        tok = make_token(secret, pw_marker())
        resp.set_cookie(COOKIE, tok, max_age=60 * 60 * 24 * 14, httponly=True, secure=secure_cookie,
                        samesite="strict", path="/")
        return read_token(secret, tok, pw_marker())

    # ------------------------------------------------------------ 인증
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        ses = session_of(request)
        return {"setup_needed": store.s.get("user") is None, "authed": bool(ses),
                "csrf": csrf_for(secret, ses["sid"]) if ses else None,
                "setup_code_required": bool(setup_code), "storage": store.status(),
                "store_loaded": store.loaded, "weak_secret": secret.startswith("dev-only")}

    @app.post("/api/setup")
    def setup(body: PasswordIn, response: Response):
        if not store.loaded:
            raise HTTPException(503, "저장소를 불러오지 못했습니다: " + store.error)
        if store.s.get("user"):
            raise HTTPException(409, "이미 설정이 끝났습니다.")
        if setup_code and body.setup_code != setup_code:
            raise HTTPException(403, "설정 코드가 올바르지 않습니다.")
        now = utcnow()
        store.s["user"] = {"id": "owner", "password_hash": hash_password(body.password),
                           "created_at": iso(now), "updated_at": iso(now)}
        init_agents(store.s, now)
        engine.audit("auth.setup", "비밀번호 설정 및 직원 12명 초기화", now)
        store.mark(important=True)
        ses = set_cookie(response)
        return {"ok": True, "csrf": csrf_for(secret, ses["sid"])}

    @app.post("/api/login")
    def login(body: LoginIn, response: Response):
        user = store.s.get("user")
        if not user or not verify_password(body.password, user["password_hash"]):
            engine.audit("auth.fail", "로그인 실패")
            raise HTTPException(401, "비밀번호가 올바르지 않습니다.")
        engine.audit("auth.login", "로그인 성공")
        ses = set_cookie(response)
        return {"ok": True, "csrf": csrf_for(secret, ses["sid"])}

    @app.post("/api/logout")
    def logout(response: Response, _=Depends(auth)):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/password")
    def change_password(body: PasswordIn, response: Response, _=Depends(auth)):
        now = utcnow()
        store.s["user"].update(password_hash=hash_password(body.password), updated_at=iso(now))
        engine.audit("auth.password", "비밀번호 변경 — 기존 로그인 모두 무효화", now)
        store.mark(important=True)
        ses = set_cookie(response)
        return {"ok": True, "csrf": csrf_for(secret, ses["sid"])}

    # ------------------------------------------------------------ 상태
    @app.get("/api/schema")
    def schema(_=Depends(auth)):
        cols = json.loads(json.dumps({k: v for k, v in COLLECTIONS.items() if not v.get("internal")}))
        acts = rule_actions()
        for f in cols["rules"]["fields"]:
            if f["k"] == "action":
                f["options"] = [a["value"] for a in acts]
                f["option_labels"] = {a["value"]: a["label"] for a in acts}
        return {"collections": cols, "tools": tool_catalog(), "doc_types": D.DOC_TYPES, "export_formats": D.EXPORT_FORMATS,
                "levels": [{"xp": x, "label": l} for x, l in LEVELS],
                "rules": [{"kind": k, "agent": r["agent"], "title": r["title"], "steps": [n for n, _, _ in r["steps"]],
                           "risk": max((x for _, _, x in r["steps"]), key=["LOW", "MEDIUM", "HIGH"].index),
                           "auto": bool(r["discover"])} for k, r in RULES.items()],
                "writable": sorted({w for r in RULES.values() for w in r["writes"]})}

    def snapshot(now) -> dict:
        s = store.s
        ses = engine.active(now)
        agents = []
        for a in s["agents"].values():
            lv, label = level_of(a["experience"])
            nxt = next((x for x, _ in LEVELS if x > a["experience"]), None)
            agents.append({**a, "level": lv, "level_label": label, "next_xp": nxt, "trust": trust_of(a),
                           "metrics": F.metrics(engine.perf(a["id"]))})
        u = engine.usage(now)
        return {
            "rev": s["rev"], "boot_id": store.boot_id, "now": iso(now), "today": today(s, now),
            "session": ses, "agents": agents, "tasks": list(s["tasks"].values()),
            "logs": s["task_logs"][-400:], "approvals": list(s["approvals"].values()),
            "c": s["c"], "inbox": s["inbox"][-30:], "settings": s["settings"],
            "ai_jobs": [j for j in s["ai_jobs"].values() if j["status"] == "pending"],
            "usage": {"today": u, "month_hours": engine.month_hours(now),
                      "actions_runs_month": sum(1 for i in s["inbox"] if i.get("ts", "")[:7] == iso(now)[:7]
                                                and i.get("kind", "").startswith("actions"))},
            "sync": store.status(now), "dashboard": F.dashboard(engine, now), "audit": s["audit"][-200:],
            "history": s["history"][-200:],
            "workflows": sorted(s["workflows"].values(), key=lambda w: w["created_at"], reverse=True)[:60],
            "files": list(s["files"].values())[-100:],
            "ai_stats": list(s["ai_stats"].values()),
            "cron_enabled": bool(cron_token),
        }

    @app.get("/api/state")
    def state(_=Depends(auth)):
        return snapshot(utcnow())

    @app.get("/api/stream")
    async def stream(request: Request, _=Depends(auth)):
        async def gen():
            last, idle = None, 0
            while not await request.is_disconnected():
                s = store.s
                cur = json.dumps({"rev": s["rev"], "sync": store.status()["state"],
                                  "agents": {a["id"]: a["status"] for a in s["agents"].values()}})
                if cur != last:
                    last, idle = cur, 0
                    yield f"data: {cur}\n\n"
                else:
                    idle += 1
                    if idle % 15 == 0:
                        yield ": keep-alive\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ 세션
    @app.post("/api/session/start")
    def session_start(body: SessionIn, _=Depends(auth)):
        return engine.start_session(utcnow(), body.ai_ready, body.ai_models)

    @app.post("/api/session/heartbeat")
    def session_heartbeat(body: SessionIn, _=Depends(auth)):
        now = utcnow()
        ses = engine.heartbeat(now, body.ai_ready, body.ai_models) or engine.start_session(now, body.ai_ready, body.ai_models)
        return {"ok": True, "session_id": ses["id"], "rev": store.s["rev"]}

    @app.post("/api/session/end")
    async def session_end(request: Request):
        # 브라우저 종료 시 sendBeacon으로 호출됨(헤더를 붙일 수 없어 쿠키만 확인). 실패해도 heartbeat 만료로 처리된다.
        if session_of(request) and store.loaded:
            engine.end_session(utcnow(), "브라우저 종료")
            await store.aflush()
        return {"ok": True}

    # ------------------------------------------------------------ 데이터 CRUD
    def coll(name: str) -> dict:
        if name not in COLLECTIONS or COLLECTIONS[name].get("internal"):
            raise HTTPException(404, "알 수 없는 데이터 종류입니다.")
        return store.s["c"].setdefault(name, {})

    def idem_seen(request: Request) -> bool:
        key = request.headers.get("idempotency-key")
        if not key or not ID_RE.match(key):
            return False
        if key in store.s["idem"]:
            return True
        store.s["idem"][key] = iso(utcnow())
        if len(store.s["idem"]) > 2000:
            for k in sorted(store.s["idem"], key=store.s["idem"].get)[:1000]:
                del store.s["idem"][k]
        return False

    def track_correction(name: str, old: dict, new: dict, now) -> None:
        """직원이 만든 기록을 사용자가 고치면 수정률에 반영하고, 피드백을 확정 사실로 기억한다."""
        fields = [f["k"] for f in COLLECTIONS[name]["fields"]]
        changes = [f"{k}: {old.get(k)} → {new.get(k)}" for k in fields if old.get(k) != new.get(k)]
        if not changes:
            return
        aid = old["by_agent"]
        if not old.get("corrected"):
            engine.perf(aid)["corrected"] += 1
        new["corrected"] = True
        who = store.s["agents"][aid]["name"]
        MEM.save(store.s, now, f"사용자가 {who}이(가) 만든 {COLLECTIONS[name]['label']} 「{old.get('title', '')}」을 수정: "
                 + "; ".join(changes)[:400], "확정 사실", aid, old.get("project_id") or "", f"feedback:{name}:{old['id']}",
                 "피드백", "사용자 수정")
        if name == "events" and new.get("start") and new.get("end") and \
                (old.get("start"), old.get("end")) != (new.get("start"), new.get("end")):
            dur = A.tmin(new["end"]) - A.tmin(new["start"])
            if dur > 0:
                MEM.save(store.s, now, f"일정 기본 길이 {dur}분 (사용자가 직원이 만든 일정을 고친 데서 반영)", "확정 사실",
                         "all", "", "rule:duration_min", "규칙,피드백", "사용자 수정")

    # ------------------------------------------------------------ 워크플로
    @app.post("/api/workflows")
    def workflow_new(body: WorkflowIn, request: Request, _=Depends(auth)):
        if body.agent_id and body.agent_id not in store.s["agents"]:
            raise HTTPException(404, "직원을 찾을 수 없습니다.")
        if body.project_id and body.project_id not in store.s["c"].get("projects", {}):
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        if idem_seen(request):
            return {"type": "duplicate"}
        now = utcnow()
        r = engine.wf.new(body.goal, now, "user", body.agent_id, body.project_id)
        engine.audit("workflow.new", f"{r['type']}: {body.goal[:100]}", now)
        store.mark(important=True)
        return {"type": r["type"], "id": (r.get("workflow") or r.get("task") or {}).get("id"),
                "template": (r.get("intent") or {}).get("template"), "title": (r.get("intent") or {}).get("title")}

    @app.post("/api/workflows/{wid}/action")
    def workflow_action(wid: str, body: WorkflowActionIn, _=Depends(auth)):
        if not engine.wf.action(wid, body.action, utcnow(), body.rating):
            raise HTTPException(400, "이 워크플로에는 해당 동작을 할 수 없습니다.")
        engine.audit(f"workflow.{body.action}", wid)
        return {"ok": True}

    @app.get("/api/tools")
    def tools_list(_=Depends(auth)):
        return {"tools": tool_catalog()}

    @app.get("/api/files/{fid}")
    async def file_get(fid: str, _=Depends(auth)):
        f = store.s["files"].get(fid)
        if not f:
            raise HTTPException(404, "파일을 찾을 수 없습니다.")
        data = await asyncio.to_thread(store.read_file, f["path"])
        if data is None:
            raise HTTPException(404, "저장소에서 파일을 찾을 수 없습니다 (영구 저장소가 없으면 서버 재시작 때 사라집니다).")
        return Response(data, media_type=f["mime"], headers={
            "Content-Disposition": f"attachment; filename=\"document.{f['fmt']}\"; filename*=UTF-8''{quote(f['name'])}"})

    @app.post("/api/cron/run")
    async def cron_run(request: Request):
        """GitHub Actions(또는 외부 무료 cron)가 호출 → 잠든 Render 서버를 깨워 예약 업무를 실행하고 저장한다."""
        if not cron_token:
            raise HTTPException(404, "서버 예약 실행이 설정되지 않았습니다 (CRON_TOKEN).")
        import hmac as _h
        if not _h.compare_digest(request.headers.get("x-cron-token", ""), cron_token):
            raise HTTPException(403, "토큰이 올바르지 않습니다.")
        if not store.loaded:
            raise HTTPException(503, "저장소를 불러오지 못했습니다: " + store.error)
        out = engine.background_run(utcnow(), reason="서버 예약 실행(외부 호출)")
        saved = await store.aflush() if store.dirty else True
        return {**out, "saved": saved, "storage": store.status()["state"]}

    @app.post("/api/c/{name}")
    def create(name: str, body: dict[str, Any], request: Request, _=Depends(auth)):
        c = coll(name)
        rid = body.get("id") if isinstance(body.get("id"), str) and ID_RE.match(body["id"]) else uid(name[:3] + "_")
        if rid in c:  # 오프라인 대기열 재전송 등 — 같은 ID면 다시 만들지 않는다
            return c[rid]
        try:
            rec = validate(name, body, refs=store.s["c"])
        except Invalid as e:
            raise HTTPException(422, str(e))
        now = utcnow()
        rec.update(id=rid, created_at=iso(now), updated_at=iso(now), version=1)
        c[rid] = rec
        engine.history(name, rid, "create", now)
        store.mark(important=True)
        return rec

    @app.put("/api/c/{name}/{rid}")
    def update(name: str, rid: str, body: dict[str, Any], _=Depends(auth)):
        c = coll(name)
        if rid not in c:
            raise HTTPException(404, "대상을 찾을 수 없습니다.")
        try:
            rec = validate(name, body, existing=c[rid], refs=store.s["c"])
        except Invalid as e:
            raise HTTPException(422, str(e))
        now = utcnow()
        old = c[rid]
        rec.update(updated_at=iso(now), version=int(old.get("version", 1)) + 1)
        if old.get("by_agent") in store.s["agents"]:
            track_correction(name, old, rec, now)
        c[rid] = rec
        engine.history(name, rid, "update", now)
        store.mark(important=True)
        return rec

    @app.delete("/api/c/{name}/{rid}")
    def delete(name: str, rid: str, _=Depends(auth)):
        c = coll(name)
        if rid in c:
            now = utcnow()
            del c[rid]
            store.s["deleted"][rid] = iso(now)
            engine.history(name, rid, "delete", now)
            engine.audit("data.delete", f"{name}/{rid} (사용자 직접 삭제)", now)
            store.mark(important=True)
        return {"ok": True}

    # ------------------------------------------------------------ 업무·승인·직원
    @app.post("/api/tasks")
    def instruct(body: InstructionIn, request: Request, _=Depends(auth)):
        if body.agent_id not in store.s["agents"]:
            raise HTTPException(404, "직원을 찾을 수 없습니다.")
        if idem_seen(request):
            return {"ok": True, "duplicate": True}
        now = utcnow()
        t = engine.add_task("user.instruction", f"지시: {body.text[:40]}", body.agent_id, "user:" + uid(), now,
                            [f"사용자가 직접 지시: {body.text[:120]}"], "user", params={"text": body.text},
                            importance=body.importance)
        engine.audit("task.instruct", f"{body.agent_id}: {body.text[:100]}", now)
        store.mark(important=True)
        return t

    @app.post("/api/tasks/{tid}/action")
    def task_action(tid: str, body: TaskActionIn, _=Depends(auth)):
        if not engine.task_action(tid, body.action, utcnow(), body.rating):
            raise HTTPException(400, "이 작업에는 해당 동작을 할 수 없습니다.")
        return {"ok": True}

    @app.post("/api/approvals/{aid}")
    def approval(aid: str, body: ApprovalIn, _=Depends(auth)):
        if not engine.resolve_approval(aid, body.approve, utcnow(), body.choice):
            raise HTTPException(400, "이미 처리되었거나 없는 승인 요청입니다.")
        return {"ok": True}

    @app.post("/api/agents/{aid}")
    def agent_update(aid: str, body: AgentIn, _=Depends(auth)):
        a = store.s["agents"].get(aid)
        if not a:
            raise HTTPException(404, "직원을 찾을 수 없습니다.")
        now = utcnow()
        if body.paused is not None:
            a["paused"] = body.paused
            engine.audit("agent.pause" if body.paused else "agent.resume", a["name"], now)
        if body.write is not None:
            allowed = {w for r in RULES.values() for w in r["writes"]}
            a["permissions"]["write"] = sorted(set(body.write) & allowed)
            engine.audit("agent.permission", f"{a['name']} 쓰기 권한: {a['permissions']['write']}", now)
        if body.auto_medium is not None:
            a["permissions"]["auto_medium"] = body.auto_medium
            engine.audit("agent.permission", f"{a['name']} 중간 위험 자동 실행: {body.auto_medium}", now)
        a["updated_at"] = iso(now)
        store.mark(important=True)
        return {"ok": True}

    # ------------------------------------------------------------ 회의·문서
    @app.post("/api/meetings")
    def meeting(body: MeetingIn, _=Depends(auth)):
        parts = [p for p in dict.fromkeys(body.participants) if p in store.s["agents"]]
        if not parts:
            raise HTTPException(422, "참석 직원을 한 명 이상 선택하세요.")
        return F.hold_meeting(engine, body.title, body.type, parts, body.use_ai, utcnow())

    @app.post("/api/documents/generate")
    def generate(body: GenerateIn, _=Depends(auth)):
        if body.doc_type not in D.DOC_TYPES:
            raise HTTPException(422, "지원하지 않는 문서 종류입니다.")
        now = utcnow()
        params = {"doc_type": body.doc_type}
        if body.doc_type == "minutes":
            if body.meeting_id not in store.s["c"].get("meetings", {}):
                raise HTTPException(404, "회의를 찾을 수 없습니다.")
            params["meeting_id"] = body.meeting_id
        t = engine.add_task("docs.generate", f"문서 작성 — {D.DOC_TYPES[body.doc_type]}", "docs", "user:" + uid(), now,
                            [f"사용자가 문서 공장에서 요청: {D.DOC_TYPES[body.doc_type]}"], "user", params=params,
                            importance=3)
        return t

    @app.get("/api/documents/{doc_id}/export")
    def export(doc_id: str, fmt: str = "docx", _=Depends(auth)):
        d = store.s["c"].get("documents", {}).get(doc_id)
        if not d:
            raise HTTPException(404, "문서를 찾을 수 없습니다.")
        try:
            data, mime, ext = D.export(d["title"], d.get("content") or "", fmt)
        except ValueError as e:
            raise HTTPException(422, str(e))
        ex = store.s["meta"].setdefault("exports", {})
        ex[ext] = ex.get(ext, 0) + 1
        name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", d["title"])[:80] + "." + ext
        return Response(data, media_type=mime, headers={
            "Content-Disposition": f"attachment; filename=\"document.{ext}\"; filename*=UTF-8''{quote(name)}"})

    @app.post("/api/upload")
    async def upload(file: UploadFile, _=Depends(auth)):
        name = (file.filename or "upload.txt")[:120]
        if not name.lower().endswith((".txt", ".md", ".csv")):
            raise HTTPException(415, "텍스트 파일(.txt, .md, .csv)만 올릴 수 있습니다.")
        raw = await file.read(300_001)
        if len(raw) > 300_000:
            raise HTTPException(413, "파일이 너무 큽니다 (최대 300KB).")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(415, "UTF-8 텍스트 파일만 올릴 수 있습니다.")
        if "\x00" in text:
            raise HTTPException(415, "텍스트 파일이 아닙니다.")
        now = utcnow()
        rid = uid("doc_")
        store.s["c"].setdefault("documents", {})[rid] = {
            "id": rid, "title": name, "type": "업로드 자료", "status": "완료", "due_date": "",
            "content": text[:200000], "version": 1, "created_at": iso(now), "updated_at": iso(now)}
        engine.history("documents", rid, "upload", now)
        store.mark(important=True)
        return {"ok": True, "id": rid}

    @app.get("/api/search")
    def search(q: str = "", _=Depends(auth)):
        return {"hits": A.search(store.s, q[:100])}

    # ------------------------------------------------------------ 시뮬레이터·비상·분신
    @app.post("/api/simulate")
    def simulate(body: dict[str, Any], _=Depends(auth)):
        if body.get("deadline") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(body["deadline"])):
            raise HTTPException(422, "마감일 형식이 올바르지 않습니다.")
        return F.simulate(body, today(store.s, utcnow()))

    @app.get("/api/emergency")
    def emergency(_=Depends(auth)):
        return {"incidents": F.incidents(engine, utcnow())}

    @app.post("/api/emergency/action")
    async def emergency_act(body: EmergencyIn, _=Depends(auth)):
        if body.action == "sync":
            ok = await store.aflush()
            return {"message": "영구 저장소에 저장했습니다." if ok else store.error}
        msg = F.emergency_action(engine, body.action, utcnow())
        if not msg:
            raise HTTPException(400, "알 수 없는 조치입니다.")
        return {"message": msg}

    @app.get("/api/twin")
    def twin(_=Depends(auth)):
        return F.twin(store.s)

    # ------------------------------------------------------------ 로컬 AI 결과
    @app.post("/api/ai/jobs/{jid}")
    def ai_job(jid: str, body: AIResultIn, _=Depends(auth)):
        return {"ok": engine.ai_result(jid, body.text, body.error, utcnow(), body.model, body.ms)}

    # ------------------------------------------------------------ 설정·저장·백업
    @app.put("/api/settings")
    def settings(body: dict[str, Any], _=Depends(auth)):
        st = store.s["settings"]
        bounds = {"tz_offset_min": (-720, 840), "daily_hour_limit": (1, 24), "max_tasks_per_session": (1, 200),
                  "max_steps_per_day": (10, 5000), "ai_daily_limit": (0, 500), "commits_per_day": (10, 1000),
                  "render_hours_limit": (1, 750), "discovery_interval_sec": (30, 3600), "max_retries": (0, 5),
                  "web_daily_limit": (0, 200), "cron_max_steps": (10, 2000)}
        for k, (lo, hi) in bounds.items():
            if k in body:
                try:
                    v = float(body[k])
                except Exception:
                    raise HTTPException(422, f"{k} 값이 올바르지 않습니다.")
                if not lo <= v <= hi:
                    raise HTTPException(422, f"{k} 값은 {lo}~{hi} 사이여야 합니다.")
                st[k] = int(v) if v == int(v) else v
        if "paused" in body:
            st["paused"] = bool(body["paused"])
        if "web_research" in body:
            st["web_research"] = bool(body["web_research"])
        now = utcnow()
        st["updated_at"] = iso(now)
        engine.audit("settings.update", json.dumps({k: body[k] for k in body if k in bounds or k in ("paused", "web_research")})[:300], now)
        store.mark(important=True)
        return st

    @app.post("/api/sync")
    async def sync(_=Depends(auth)):
        ok = await store.aflush()
        return {"ok": ok, "sync": store.status()}

    @app.get("/api/export")
    def export_all(_=Depends(auth)):
        s = {k: v for k, v in store.s.items() if k != "user"}
        return Response(json.dumps(s, ensure_ascii=False, indent=1), media_type="application/json", headers={
            "Content-Disposition": f"attachment; filename=\"lifeos-export-{today(store.s, utcnow())}.json\""})

    @app.get("/api/snapshots")
    async def snapshots(_=Depends(auth)):
        return {"snapshots": await asyncio.to_thread(store.snapshots)}

    @app.post("/api/snapshots/restore")
    async def restore(body: RestoreIn, _=Depends(auth)):
        # 기존 데이터를 통째로 바꾸는 HIGH 위험 작업 — 비밀번호를 다시 확인한다.
        if not verify_password(body.password, store.s["user"]["password_hash"]):
            raise HTTPException(403, "비밀번호가 올바르지 않습니다.")
        engine.audit("backup.restore", body.name)
        if not await asyncio.to_thread(store.restore, body.name):
            raise HTTPException(404, "스냅샷을 찾을 수 없습니다.")
        engine.audit("backup.restored", body.name)
        return {"ok": True}

    @app.post("/api/recover")
    def recover(body: RecoverIn, _=Depends(auth)):
        """서버가 저장 전에 재시작되어 브라우저(IndexedDB)가 더 최신 상태를 갖고 있을 때, 레코드 단위로 병합한다."""
        cs = body.state
        if not isinstance(cs.get("c"), dict) or not isinstance(cs.get("tasks"), list):
            raise HTTPException(422, "형식이 올바르지 않습니다.")
        incoming = migrate({
            "rev": int(cs.get("rev", 0)),
            "tasks": {t["id"]: t for t in cs["tasks"] if isinstance(t, dict) and "id" in t},
            "approvals": {a["id"]: a for a in cs.get("approvals", []) if isinstance(a, dict) and "id" in a},
            "c": {n: v for n, v in cs["c"].items() if n in COLLECTIONS and isinstance(v, dict)},
        })
        incoming["settings"] = {"updated_at": ""}
        before = store.s["rev"]
        merged = merge(store.s, incoming)
        merged["agents"] = store.s["agents"]  # 직원 상태·권한은 서버 쪽만 신뢰
        merged["user"] = store.s["user"]
        store.s.clear()
        store.s.update(merged)
        for t in store.s["tasks"].values():
            if t["status"] == "RUNNING":
                t["status"] = "QUEUED"
        engine.audit("recover.merge", f"브라우저 보관본(rev {cs.get('rev')})을 서버(rev {before})에 병합")
        store.mark(important=True)
        return {"ok": True}

    # ------------------------------------------------------------ 정적 파일 (프론트엔드)
    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        full = os.path.realpath(os.path.join(STATIC, path))
        if path and full.startswith(os.path.realpath(STATIC)) and os.path.isfile(full):
            return FileResponse(full)
        index = os.path.join(STATIC, "index.html")
        if os.path.isfile(index):
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "프론트엔드가 빌드되지 않았습니다. frontend에서 npm run build를 실행하세요."}, status_code=503)

    return app


app = create_app() if os.environ.get("LIFEOS_NO_AUTOSTART") != "1" else None
