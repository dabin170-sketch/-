"""Session-Based Autonomous Agent Engine.

접속(세션)이 살아 있는 동안 tick()이 반복 호출된다. 한 번의 tick은 '한 작업의 한 단계'만 실행하고
체크포인트를 남긴다. 따라서 어느 시점에 중단되어도 마지막으로 끝난 단계부터 이어서 실행할 수 있다.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

from . import analysis as A
from .agents import init_agents, level_of
from .rules import RULES, Ctx, NeedAI
from .store import Store
from .workflow import ACTIVE as WF_ACTIVE, Workflows
from .util import dadd, ddiff, iso, parse, today, uid, utcnow

HEARTBEAT_TIMEOUT = 75  # 초. 이 시간 동안 heartbeat가 없으면 세션이 끊긴 것으로 본다.
AI_JOB_TIMEOUT = 300
ACTIVE = ("NEW", "ANALYZING", "QUEUED", "RUNNING", "WAITING_APPROVAL", "BLOCKED")


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.last_discovery: Optional[datetime] = None
        self._last_tick: Optional[datetime] = None
        self.wf = Workflows(self)
        from .intake import Intake
        self.intake = Intake(self)

    @property
    def s(self) -> dict:
        return self.store.s

    # ------------------------------------------------------------ 기록
    def log(self, task: Optional[dict], agent: str, etype: str, msg: str, now: datetime) -> None:
        self.s["task_logs"].append({"id": uid("lg_"), "task_id": task["id"] if task else None, "agent_id": agent,
                                    "event_type": etype, "message": msg, "ts": iso(now)})
        if len(self.s["task_logs"]) > 3000:
            del self.s["task_logs"][:500]

    def audit(self, action: str, detail: str, now: Optional[datetime] = None) -> None:
        self.s["audit"].append({"id": uid("au_"), "ts": iso(now or utcnow()), "action": action, "detail": detail[:500]})
        if len(self.s["audit"]) > 1500:
            del self.s["audit"][:300]

    def history(self, coll: str, rid: str, op: str, now: datetime) -> None:
        self.s["history"].append({"id": uid("h_"), "ts": iso(now), "collection": coll, "record": rid, "op": op})
        if len(self.s["history"]) > 1500:
            del self.s["history"][:300]

    def perf(self, aid: str) -> dict:
        """실제 업무 품질 지표 (게임 경험치와 별도로 관리)."""
        p = self.s["agents"][aid].setdefault("perf", {})
        for k in ("units_done", "units_failed", "verified", "first_pass", "with_error", "retries", "time_ms", "time_n",
                  "produced", "corrected", "rating_sum", "rating_n"):
            p.setdefault(k, 0)
        return p

    def usage(self, now: datetime) -> dict:
        u = self.s["usage"].setdefault(now.strftime("%Y-%m-%d"), {})
        for k in ("steps", "tasks", "model_calls", "commits", "active_sec", "server_sec"):
            u.setdefault(k, 0)
        return u

    def month_hours(self, now: datetime) -> float:
        m = now.strftime("%Y-%m")
        return round(sum(u.get("server_sec", 0) for d, u in self.s["usage"].items() if d.startswith(m)) / 3600, 1)

    def set_status(self, aid: str, status: str, now: datetime, task: Optional[str] = "keep") -> None:
        a = self.s["agents"][aid]
        if a["status"] != status:
            a["status"], a["status_since"] = status, iso(now)
        if task != "keep":
            a["current_task"] = task
        a["updated_at"] = iso(now)

    # ------------------------------------------------------------ 작업 생성
    def add_task(self, kind: str, title: str, agent: str, key: str, now: datetime, evidence: list,
                 source: str, params: Optional[dict] = None, due: Optional[str] = None, importance: int = 0,
                 valid_until: Optional[str] = None) -> Optional[dict]:
        """idempotency key가 같은 작업이 이미 있으면 만들지 않는다 (중복 생성 방지)."""
        if any(t["idempotency_key"] == key for t in self.s["tasks"].values()):
            return None
        t = {"id": uid("t_"), "kind": kind, "title": title, "description": " / ".join(evidence)[:400],
             "assigned_agent": agent, "project_id": (params or {}).get("project_id"),
             "priority": 0, "priority_factors": [], "status": "NEW", "source": source, "evidence": evidence,
             "params": params or {}, "due": due, "valid_until": valid_until, "user_importance": importance,
             "steps": [n for n, _, _ in RULES[kind]["steps"]], "progress": 0,
             "checkpoint": {"step": 0, "state": {}}, "approved_steps": [], "waiting_ai": None,
             "blocked_reason": "", "result": None, "error": "", "retry_count": 0, "rating": None,
             "idempotency_key": key, "created_at": iso(now), "updated_at": iso(now),
             "started_at": None, "completed_at": None}
        self.s["tasks"][t["id"]] = t
        self.log(t, agent, "CREATED", f"업무 생성 [{source}] 근거: " + " / ".join(evidence)[:300], now)
        self.store.mark()
        return t

    # ------------------------------------------------------------ 세션
    def active(self, now: datetime) -> Optional[dict]:
        if not self.s["sessions"]:
            return None
        ses = self.s["sessions"][-1]
        if ses["status"] != "ACTIVE":
            return None
        if (now - parse(ses["last_heartbeat_at"])).total_seconds() > HEARTBEAT_TIMEOUT:
            ses.update(status="ENDED", ended_at=ses["last_heartbeat_at"], end_reason="heartbeat 끊김")
            self._offline(now)
            self.store.mark(important=True)
            return None
        return ses

    def start_session(self, now: datetime, ai_ready: bool = False, ai_models: Optional[dict] = None) -> dict:
        s = self.s
        init_agents(s, now)
        cur = self.active(now)
        if cur:  # 다른 탭·기기에서 이미 진행 중이면 같은 세션에 합류
            cur["last_heartbeat_at"] = iso(now)
            cur["ai_ready"] = cur.get("ai_ready") or ai_ready
            if ai_models is not None:
                cur["ai_models"] = ai_models
            return cur
        prev = s["sessions"][-1] if s["sessions"] else None
        last_seen = prev["last_heartbeat_at"] if prev else None
        td = today(s, now)
        gap_days = ddiff(td, today(s, parse(last_seen))) if last_seen else None

        # Actions가 남긴 알림 수거
        known = {i["id"] for i in s["inbox"]}
        new_inbox = [i for i in self.store.read_inbox() if isinstance(i, dict) and i.get("id") not in known]
        s["inbox"] = (s["inbox"] + new_inbox)[-100:]

        # 중단된 작업 복구: 마지막 체크포인트부터 다시 실행
        recovered, skipped = [], []
        for t in s["tasks"].values():
            if t["status"] == "RUNNING":
                t.update(status="QUEUED", waiting_ai=None, updated_at=iso(now))
                recovered.append(t["title"])
                self.log(t, t["assigned_agent"], "RECOVERED",
                         f"중단된 작업 복구 — {t['checkpoint']['step']}/{len(t['steps'])} 단계 완료 지점부터 재개", now)
            # 미접속 기간에 유효기간이 지난 자동 업무는 실행하지 않고 생략
            if t["status"] in ("NEW", "ANALYZING", "QUEUED", "BLOCKED") and t.get("valid_until") and t["valid_until"] < td:
                t.update(status="STOPPED", error="미접속 기간에 유효기간이 지나 실행하지 않고 생략함", updated_at=iso(now))
                skipped.append(t["title"])
                self.log(t, t["assigned_agent"], "SKIPPED", "유효기간 경과로 생략 (실행되지 않았음)", now)
            elif t["status"] == "ANALYZING":
                t["status"] = "NEW"
        for j in s["ai_jobs"].values():
            if j["status"] == "pending":
                j.update(status="expired", updated_at=iso(now))

        sch = A.a_schedule(s, now)
        report = {"first": prev is None, "last_seen": last_seen, "gap_days": gap_days,
                  "recovered": recovered, "skipped": skipped,
                  "overdue": len(sch["overdue"]), "deadlines": len(sch["deadlines"]),
                  "pending_approvals": sum(1 for a in s["approvals"].values() if a["status"] == "PENDING"),
                  "unfinished": sum(1 for t in s["tasks"].values() if t["status"] in ACTIVE),
                  "workflows": [w["title"] for w in s["workflows"].values() if w["status"] in WF_ACTIVE],
                  "proposals": sum(1 for w in s["workflows"].values() if w["status"] == "PROPOSED"),
                  "inbox": new_inbox}
        ses = {"id": uid("ses_"), "started_at": iso(now), "last_heartbeat_at": iso(now), "ended_at": None,
               "status": "ACTIVE", "phase": "checkin", "ai_ready": ai_ready, "ai_models": ai_models or {}, "tasks_run": 0,
               "limit": "", "report": report}
        s["sessions"].append(ses)
        del s["sessions"][:-200]
        for a in s["agents"].values():
            self.set_status(a["id"], "PAUSED" if a["paused"] else "CHECKING_IN", now, None)
        self.last_discovery = None
        self.log(None, "chief", "SESSION", "접속 확인 — 직원 출근 시작"
                 + (f" (마지막 접속 후 {gap_days}일)" if gap_days else ""), now)
        self.audit("session.start", f"gap_days={gap_days} recovered={len(recovered)} skipped={len(skipped)}", now)
        self.store.mark(important=True)
        return ses

    def heartbeat(self, now: datetime, ai_ready: Optional[bool] = None, ai_models: Optional[dict] = None) -> Optional[dict]:
        ses = self.active(now)
        if ses:
            ses["last_heartbeat_at"] = iso(now)
            if ai_ready is not None:
                ses["ai_ready"] = ai_ready
            if ai_models is not None:
                ses["ai_models"] = ai_models
        return ses

    def end_session(self, now: datetime, reason: str = "사용자 종료") -> None:
        ses = self.active(now)
        if not ses:
            return
        ses.update(status="ENDED", ended_at=iso(now), end_reason=reason)
        self._offline(now)
        self.log(None, "chief", "SESSION", "접속 종료 — 진행 상황 저장", now)
        self.store.mark(important=True)

    def _offline(self, now: datetime) -> None:
        for a in self.s["agents"].values():
            self.set_status(a["id"], "OFFLINE", now, None)

    # ------------------------------------------------------------ 우선순위
    def prioritize(self, t: dict, now: datetime, ai_ready: bool = False) -> None:
        s, td = self.s, today(self.s, now)
        f = []
        if t.get("due"):
            d = ddiff(t["due"], td)
            pts = 30 if d <= 0 else 26 if d == 1 else 20 if d <= 3 else 12 if d <= 7 else 6
            f.append(("마감 긴급도", pts, f"기한 {t['due']} (D{-d:+d})" if d else f"기한 {t['due']} (오늘)"))
        else:
            f.append(("마감 긴급도", 8 if t.get("valid_until") == td else 5,
                      "오늘 안에만 유효한 업무" if t.get("valid_until") == td else "기한 없음"))
        area = {"study": "학습", "writing": "집필", "finance": "재정", "invest": "재정", "project": "프로젝트",
                "life": "생활"}.get(t["assigned_agent"])
        imp = max([int(g.get("importance") or 3) for g in A.V(s, "goals")
                   if g.get("status", "진행") == "진행" and g.get("area") == area] or [0])
        f.append(("목표 중요도", imp * 4 if imp else 10,
                  f"관련 목표({area}) 중요도 {imp}" if imp else "연결된 목표 없음 — 기본값"))
        ui = int(t.get("user_importance") or 0)
        f.append(("사용자 지정 중요도", ui * 5, f"사용자 지정 {ui}/3" if ui else "자동 생성 업무"))
        est = RULES[t["kind"]]["est"]
        f.append(("예상 소요", 10 if est <= 2 else 7 if est <= 3 else 4, f"예상 {est}단계 분량"))
        recent = [x for x in s["tasks"].values() if x["assigned_agent"] == t["assigned_agent"]
                  and x["status"] == "DONE" and (x.get("completed_at") or "")[:10] >= dadd(td, -3)]
        f.append(("최근 진행 정도", 3 if recent else 10,
                  f"이 분야에서 최근 3일 완료 {len(recent)}건" if recent else "이 분야에서 최근 3일 완료 업무 없음"))
        blocks = t["kind"] in ("chief.briefing", "schedule.conflicts", "schedule.daily_plan")
        f.append(("의존성", 5 if blocks else 0, "다른 업무의 판단 근거가 되는 업무" if blocks else "의존 관계 없음"))
        needs_ai = RULES[t["kind"]]["needs_ai"]
        f.append(("실행 가능 여부", 0 if needs_ai and not ai_ready else 5,
                  "로컬 AI가 필요하지만 준비되지 않음" if needs_ai and not ai_ready else "지금 바로 실행 가능"))
        f.append(("실패 위험", -3 * t["retry_count"], f"이전 실패 {t['retry_count']}회"))
        f.append(("AI 자원 사용량", 2 if needs_ai else 5, "AI 추론 필요" if needs_ai else "AI 추론 불필요(규칙 기반)"))
        pref = self.preference_bonus(t["assigned_agent"])
        f.append(("선호 반영(디지털 분신)", pref[0], pref[1]))
        t["priority"] = max(0, min(100, sum(p for _, p, _ in f)))
        t["priority_factors"] = [{"name": n, "points": p, "why": w} for n, p, w in f]

    def preference_bonus(self, aid: str) -> tuple[int, str]:
        st = self.s["agents"][aid]["stats"]
        if st["rating_n"] >= 2:
            avg = st["rating_sum"] / st["rating_n"]
            if avg >= 4:
                return 5, f"이 직원 업무에 준 평가 평균 {avg:.1f}점 — 선호하는 업무로 판단"
            if avg <= 2:
                return -3, f"이 직원 업무에 준 평가 평균 {avg:.1f}점"
        return 0, "평가 기록 부족 — 반영 없음"

    # ------------------------------------------------------------ 한도
    def limit_reason(self, now: datetime, ses: dict) -> str:
        st, u = self.s["settings"], self.usage(now)
        if ses["tasks_run"] >= st["max_tasks_per_session"]:
            return f"세션당 작업 한도({st['max_tasks_per_session']}건)에 도달"
        if u["steps"] >= st["max_steps_per_day"]:
            return f"일일 실행 단계 한도({st['max_steps_per_day']})에 도달"
        if self.month_hours(now) >= st["render_hours_limit"]:
            return f"이번 달 서버 가동 추정치가 {st['render_hours_limit']}시간에 도달 (Render 무료 750시간 보호)"
        return ""

    # ------------------------------------------------------------ 메인 루프
    def tick(self, now: Optional[datetime] = None) -> str:
        now = now or utcnow()
        s = self.s
        ses = self.active(now)
        if not ses:
            self._last_tick = None
            return "no-session"
        if self._last_tick:
            self.usage(now)["active_sec"] += min(5, max(0, (now - self._last_tick).total_seconds()))
        self._last_tick = now
        self._expire_jobs(now)

        if s["settings"].get("paused"):
            for a in s["agents"].values():
                self.set_status(a["id"], "PAUSED", now)
            return "paused"

        if ses["phase"] == "checkin":
            for a in s["agents"].values():
                self.set_status(a["id"], "PAUSED" if a["paused"] else "IDLE", now, None)
            ses["phase"] = "run"
            self.log(None, "chief", "SESSION", f"직원 {sum(1 for a in s['agents'].values() if not a['paused'])}명 출근 완료", now)
            self.store.mark()
            return "checkin"

        # 잠깐 표시하는 상태(완료·오류) 정리
        for a in s["agents"].values():
            if a["paused"]:
                self.set_status(a["id"], "PAUSED", now)
            elif a["status"] in ("COMPLETED", "ERROR", "PAUSED", "CHECKING_IN") and \
                    (now - parse(a["status_since"])).total_seconds() >= 3:
                self.set_status(a["id"], "IDLE", now, None)

        tasks = list(s["tasks"].values())
        # 화면에 보이는 직원 상태를 실제 작업 상태와 맞춘다
        self.wf.check_timeouts(now)
        self.wf.unblock(now)
        for a in s["agents"].values():
            cid = a["current_task"]
            cur = s["tasks"].get(cid) if cid else None
            w = s["workflows"].get(cid) if cid else None
            alive = (cur and cur["status"] in ("RUNNING", "WAITING_APPROVAL", "BLOCKED", "ANALYZING")) or \
                (w and w["status"] in WF_ACTIVE and self.wf.step_agent(w) == a["id"])
            if a["status"] in ("WORKING", "BLOCKED", "WAITING_APPROVAL", "PLANNING") and not alive:
                self.set_status(a["id"], "IDLE", now, None)
        for t in tasks:
            if t["status"] in ("WAITING_APPROVAL", "BLOCKED") and s["agents"][t["assigned_agent"]]["status"] == "IDLE":
                self.set_status(t["assigned_agent"], t["status"], now, t["id"])
        for w in s["workflows"].values():
            if w["status"] in ("WAITING_APPROVAL", "WAITING_INPUT", "BLOCKED"):
                aid = self.wf.step_agent(w)
                if s["agents"][aid]["status"] == "IDLE":
                    self.set_status(aid, "BLOCKED" if w["status"] == "BLOCKED" else "WAITING_APPROVAL", now, w["id"])
        new = [t for t in tasks if t["status"] == "NEW"]
        if new:
            for t in new:
                t.update(status="ANALYZING", updated_at=iso(now))
                if s["agents"][t["assigned_agent"]]["status"] == "IDLE":
                    self.set_status(t["assigned_agent"], "PLANNING", now, t["id"])
            self.set_status("chief", "PLANNING", now)
            self.store.mark()
            return "analyzing"
        ana = [t for t in tasks if t["status"] == "ANALYZING"]
        if ana:
            for t in ana:
                self.prioritize(t, now, ses.get("ai_ready", False))
                t.update(status="QUEUED", updated_at=iso(now))
                self.log(t, "chief", "PRIORITIZED", f"우선순위 {t['priority']}점으로 대기열 배정", now)
                if s["agents"][t["assigned_agent"]]["status"] == "PLANNING":
                    self.set_status(t["assigned_agent"], "IDLE", now, None)
            if s["agents"]["chief"]["status"] == "PLANNING":
                self.set_status("chief", "IDLE", now)
            self.store.mark()
            return "queued"

        # 막혔던 작업 재검토
        for t in tasks:
            if t["status"] == "BLOCKED":
                r = RULES.get(t["kind"])
                perms = s["agents"][t["assigned_agent"]]["permissions"]
                if t["blocked_reason"].startswith("AI") and ses.get("ai_ready") and \
                        self.usage(now)["model_calls"] < s["settings"]["ai_daily_limit"]:
                    t.update(status="QUEUED", blocked_reason="", updated_at=iso(now))
                elif t["blocked_reason"].startswith("권한") and r and all(w in perms["write"] for w in r["writes"]):
                    t.update(status="QUEUED", blocked_reason="", updated_at=iso(now))

        lim = self.limit_reason(now, ses)
        t = self._pick()
        if lim and lim.startswith("세션당") and t and t["status"] == "RUNNING":
            lim = ""  # 작업 수 한도는 새 작업을 시작하지 않는 것 — 이미 시작한 작업은 끝까지 마친다
        if lim:
            if ses.get("limit") != lim:
                ses["limit"] = lim
                self.log(None, "system", "LIMIT", f"무료 한도 보호: {lim} — 남은 업무는 대기 상태로 둡니다", now)
                self.audit("limit.hit", lim, now)
                for a in s["agents"].values():
                    if a["status"] in ("WORKING", "PLANNING"):
                        self.set_status(a["id"], "IDLE", now, None)
                self.store.mark(important=True)
            return "limit"
        if not self.limit_reason(now, ses):
            ses["limit"] = ""

        w = self.wf.pick()
        if w and not (t and t["status"] == "RUNNING"):
            return "wf-" + self.wf.advance(w, now, ses)
        if not t:
            interval = s["settings"]["discovery_interval_sec"]
            if self.last_discovery is None or (now - self.last_discovery).total_seconds() >= interval:
                self.last_discovery = now
                if self.discover(now):
                    return "discovered"
            for a in s["agents"].values():
                if a["status"] in ("WORKING", "PLANNING"):
                    self.set_status(a["id"], "IDLE", now, None)
            return "idle"
        return self._step(t, now, ses)

    def _pick(self) -> Optional[dict]:
        cands = [t for t in self.s["tasks"].values()
                 if t["status"] in ("QUEUED", "RUNNING") and not t.get("waiting_ai")
                 and not self.s["agents"][t["assigned_agent"]]["paused"]]
        if not cands:
            return None
        return sorted(cands, key=lambda t: (t["status"] != "RUNNING", -t["priority"], t["created_at"]))[0]

    # ------------------------------------------------------------ 업무 발견
    def discover(self, now: datetime) -> int:
        s, n, td = self.s, 0, today(self.s, now)
        for kind, r in RULES.items():
            if not r["discover"] or n >= 20:
                continue
            try:
                cands = r["discover"](s, now)
            except Exception as e:
                self.log(None, r["agent"], "ERROR", f"업무 발견 규칙 오류({kind}): {e}", now)
                continue
            for c in cands:
                if self.add_task(kind, c.get("title") or r["title"], r["agent"], c["key"], now, c["evidence"],
                                 f"auto:{kind}", params=c.get("params"), due=c.get("due"),
                                 valid_until=c.get("valid_until")):
                    n += 1
        n += self.run_rules(now)
        return self._discover_tail(now, n)

    def run_rules(self, now: datetime, background: bool = False) -> int:
        """주기가 지난 반복 업무 규칙을 업무로 만든다. background=True면 '서버 예약 실행'이 켜진 규칙만."""
        s, n, td = self.s, 0, today(self.s, now)
        for rule in A.V(s, "rules"):  # 사용자가 등록한 반복 업무
            if not rule.get("enabled", True) or not rule.get("action"):
                continue
            if background and not rule.get("run_when_closed"):
                continue
            every = max(1, int(rule.get("every_days") or 7))
            last = rule.get("last_executed_at")
            if last and ddiff(td, last) < every:
                continue
            kind, _, dt = rule["action"].partition(":")
            if kind not in RULES:
                continue
            if self.add_task(kind, f"[반복] {rule['title']}", RULES[kind]["agent"], f"rule:{rule['id']}:{td}", now,
                             [f"반복 규칙 「{rule['title']}」 — 주기 {every}일, 마지막 실행 {last or '없음'}"],
                             f"rule:{rule['id']}", params={**({"doc_type": dt} if dt else {}), "bg": background}):
                rule["last_executed_at"] = td
                rule["updated_at"] = iso(now)
                n += 1
        return n

    def _discover_tail(self, now: datetime, n: int) -> int:
        try:
            p = self.wf.discover_proposals(now)
        except Exception as e:
            p = 0
            self.log(None, "chief", "ERROR", f"제안 발견 오류: {e}", now)
        if p:
            self.log(None, "chief", "PROPOSAL", f"후속 작업 {p}건을 제안함 (승인해야 실행)", now)
        if n:
            self.log(None, "chief", "DISCOVERY", f"저장된 데이터에서 새 업무 {n}건 발견", now)
        return n + p

    # ------------------------------------------------------------ 단계 실행
    def _step(self, t: dict, now: datetime, ses: dict) -> str:
        s = self.s
        r = RULES.get(t["kind"])
        agent = s["agents"][t["assigned_agent"]]
        if not r:
            return self._fail(t, now, "알 수 없는 업무 종류", final=True)
        i = t["checkpoint"]["step"]
        name, fn, risk = r["steps"][i]

        missing = [w for w in r["writes"] if w not in agent["permissions"]["write"]]
        if missing:
            t.update(status="BLOCKED", blocked_reason=f"권한 없음: {', '.join(missing)} 쓰기 권한이 필요", updated_at=iso(now))
            self.set_status(agent["id"], "BLOCKED", now, t["id"])
            self.log(t, agent["id"], "BLOCKED", t["blocked_reason"], now)
            self.store.mark()
            return "blocked"

        if (risk == "HIGH" or (risk == "MEDIUM" and not agent["permissions"].get("auto_medium"))) \
                and i not in t["approved_steps"]:
            ap = {"id": uid("ap_"), "task_id": t["id"], "step": i, "agent_id": agent["id"],
                  "action": f"{t['title']} — {name}", "detail": t["checkpoint"]["state"].get("proposal", []),
                  "risk_level": risk, "status": "PENDING", "requested_at": iso(now), "resolved_at": None,
                  "updated_at": iso(now)}
            s["approvals"][ap["id"]] = ap
            t.update(status="WAITING_APPROVAL", updated_at=iso(now))
            self.set_status(agent["id"], "WAITING_APPROVAL", now, t["id"])
            self.log(t, agent["id"], "APPROVAL_REQUESTED", f"[{risk}] '{name}' 단계는 사용자 승인이 필요합니다", now)
            self.store.mark(important=True)
            return "approval"

        if t["status"] != "RUNNING":
            first = t["started_at"] is None
            t.update(status="RUNNING", started_at=t["started_at"] or iso(now))
            if first:
                ses["tasks_run"] += 1
                self.usage(now)["tasks"] += 1
            self.log(t, agent["id"], "STARTED" if first else "RESUMED",
                     f"{'업무 시작' if first else f'{i + 1}단계부터 재개'}: {t['title']}", now)
        self.set_status(agent["id"], "WORKING", now, t["id"])

        t0 = time.monotonic()
        try:
            fn(Ctx(self, t, now))
        except NeedAI as e:
            return self._need_ai(t, e, now, ses)
        except Exception as e:
            return self._fail(t, now, f"{name}: {e}")
        took = time.monotonic() - t0
        t["checkpoint"]["step"] = i + 1
        t["progress"] = round(100 * (i + 1) / len(r["steps"]))
        t["error"] = ""
        t["updated_at"] = iso(now)
        self.usage(now)["steps"] += 1
        self.log(t, agent["id"], "STEP", f"{i + 1}/{len(r['steps'])} {name} 완료 ({took * 1000:.0f}ms) — 체크포인트 저장", now)
        if i + 1 >= len(r["steps"]):
            self._complete(t, now)
        self.store.mark(important=True)
        return "step"

    def _need_ai(self, t: dict, e: NeedAI, now: datetime, ses: dict) -> str:
        s = self.s
        aid = t["assigned_agent"]
        if not ses.get("ai_ready"):
            reason = "AI 모델 없음 — 이 지시는 규칙으로 처리할 수 없어 로컬 AI가 필요합니다 (설정에서 모델을 켜면 자동 재개)"
        elif self.usage(now)["model_calls"] >= s["settings"]["ai_daily_limit"]:
            reason = f"AI 일일 호출 한도({s['settings']['ai_daily_limit']}회) 도달 — 내일 자동 재개"
        else:
            job = {"id": uid("job_"), "kind": "task", "task_id": t["id"], "agent_id": aid, "system": e.system,
                   "prompt": e.prompt, "status": "pending", "created_at": iso(now), "updated_at": iso(now)}
            s["ai_jobs"][job["id"]] = job
            t.update(waiting_ai=job["id"], updated_at=iso(now))
            self.usage(now)["model_calls"] += 1
            self.log(t, aid, "AI_REQUESTED", "브라우저 로컬 AI에 추론 요청", now)
            self.store.mark()
            return "ai-wait"
        t.update(status="BLOCKED", blocked_reason=reason, updated_at=iso(now))
        self.set_status(aid, "BLOCKED", now, t["id"])
        self.log(t, aid, "BLOCKED", reason, now)
        self.store.mark()
        return "blocked"

    def ai_result(self, job_id: str, text: str, error: str, now: datetime, model: str = "", ms: int = 0) -> bool:
        s = self.s
        job = s["ai_jobs"].get(job_id)
        if not job or job["status"] != "pending":
            return False
        job.update(status="error" if error else "done", updated_at=iso(now))
        if job["kind"] == "workflow":
            ok = self.wf.ai_result(job, text, error, model, ms, now)
            self.store.mark(important=True)
            return ok
        if job["kind"] == "meeting":
            m = s["c"].get("meetings", {}).get(job.get("meeting_id"))
            if m and not error and text.strip():
                m["transcript"].append({"agent": job["agent_id"], "mode": "ai",
                                        "lines": [x for x in text.strip().split("\n") if x.strip()][:20]})
                m["mode_label"] = "규칙 기반 협업 보고서 + 로컬 AI 토론"
                m["updated_at"] = iso(now)
            self.store.mark(important=True)
            return True
        t = s["tasks"].get(job.get("task_id"))
        if not t or t.get("waiting_ai") != job_id:
            return False
        t["waiting_ai"] = None
        if error or not text.strip():
            self._fail(t, now, f"로컬 AI 추론 실패: {error or '빈 응답'}")
        else:
            t["checkpoint"]["state"]["ai_text"] = text[:6000]
            self.log(t, t["assigned_agent"], "AI_DONE", f"로컬 AI 응답 수신 ({len(text)}자)", now)
        self.store.mark(important=True)
        return True

    def _expire_jobs(self, now: datetime) -> None:
        for j in self.s["ai_jobs"].values():
            if j["status"] == "pending" and (now - parse(j["created_at"])).total_seconds() > AI_JOB_TIMEOUT:
                j.update(status="expired", updated_at=iso(now))
                t = self.s["tasks"].get(j.get("task_id"))
                if t and t.get("waiting_ai") == j["id"]:
                    t["waiting_ai"] = None
                    self._fail(t, now, "로컬 AI 응답 시간 초과")
        if len(self.s["ai_jobs"]) > 200:
            for k in sorted(self.s["ai_jobs"], key=lambda k: self.s["ai_jobs"][k]["created_at"])[:100]:
                if self.s["ai_jobs"][k]["status"] != "pending":
                    del self.s["ai_jobs"][k]
                    self.s["deleted"][k] = iso(now)

    def _fail(self, t: dict, now: datetime, msg: str, final: bool = False) -> str:
        aid = t["assigned_agent"]
        t["retry_count"] += 1
        t["error"] = msg[:300]
        t["updated_at"] = iso(now)
        if final or t["retry_count"] > self.s["settings"]["max_retries"]:
            t["status"] = "FAILED"
            a = self.s["agents"][aid]
            a["stats"]["failed"] += 1
            p = self.perf(aid)
            p["units_failed"] += 1
            p["with_error"] += 1
            p["retries"] += t["retry_count"]
            self.log(t, aid, "FAILED", f"실패 확정 (시도 {t['retry_count']}회): {msg}", now)
        else:
            t["status"] = "QUEUED"
            self.log(t, aid, "ERROR", f"오류 — 같은 단계부터 재시도 예정 ({t['retry_count']}회째): {msg}", now)
        self.set_status(aid, "ERROR", now, None)
        self.store.mark(important=True)
        return "error"

    def _complete(self, t: dict, now: datetime) -> None:
        aid = t["assigned_agent"]
        a = self.s["agents"][aid]
        t["result"] = t["checkpoint"]["state"].get("result") or {"summary": "", "lines": []}
        t["checkpoint"]["state"] = {}  # 완료 후에는 중간 자료를 비워 저장 용량을 줄인다
        t.update(status="DONE", completed_at=iso(now), progress=100)
        # 총괄 관리자 검수: 결과 요약과 근거가 모두 있어야 통과
        passed = bool(t["result"].get("summary")) and bool(t["evidence"]) and isinstance(t["result"].get("lines"), list)
        t["review"] = {"passed": passed, "by": "chief",
                       "note": "결과 요약·근거 확인됨" if passed else "결과 요약 또는 근거가 비어 있음"}
        from .tools import C
        from . import verify as VF
        VF.record(self.s, now, "task", t["id"], [C("결과 요약 있음", bool(t["result"].get("summary")), t["result"].get("summary", "")[:80]),
                                                C("근거 기록 있음", bool(t["evidence"]), " / ".join(t["evidence"])[:80])],
                  aid, title=t["title"], source="rule_task")
        a["stats"]["done"] += 1
        p = self.perf(aid)
        p["units_done"] += 1
        p["verified"] += 1
        p["first_pass"] += 1 if passed and t["retry_count"] == 0 else 0
        p["with_error"] += 1 if t["retry_count"] else 0
        p["retries"] += t["retry_count"]
        if t.get("started_at"):
            p["time_ms"] += int((now - parse(t["started_at"])).total_seconds() * 1000)
            p["time_n"] += 1
        xp = 10 + (5 if passed else 0) + (3 if t["retry_count"] == 0 else 0)
        if passed:
            a["stats"]["review_pass"] += 1
        old = a["level"]
        a["experience"] += xp
        a["level"], label = level_of(a["experience"])
        self.set_status(aid, "COMPLETED", now, None)
        self.log(t, aid, "DONE", f"완료: {t['result'].get('summary', '')} (+{xp} XP, 검수 {'통과' if passed else '보류'})", now)
        if a["level"] > old:
            self.log(None, aid, "LEVEL_UP", f"{a['name']} 레벨 {a['level']} ({label}) 달성", now)

    # ------------------------------------------------------------ 승인·사용자 조작
    def resolve_approval(self, ap_id: str, approve: bool, now: datetime, choice: Optional[int] = None) -> bool:
        ap = self.s["approvals"].get(ap_id)
        if not ap or ap["status"] != "PENDING":
            return False
        if ap.get("workflow_id"):
            if not self.wf.resolve_approval(ap, approve, choice, now):
                return False
            ap.update(status="APPROVED" if approve and not (ap["kind"] == "choice" and self.s["workflows"][ap["workflow_id"]]["status"] == "STOPPED")
                      else "REJECTED", resolved_at=iso(now), updated_at=iso(now))
            self.audit("approval." + ap["status"].lower(), ap["action"] + (f" — {ap.get('chosen')}" if ap.get("chosen") else ""), now)
            self.store.mark(important=True)
            return True
        ap.update(status="APPROVED" if approve else "REJECTED", resolved_at=iso(now), updated_at=iso(now))
        t = self.s["tasks"].get(ap["task_id"])
        if t and t["status"] == "WAITING_APPROVAL":
            if approve:
                t["approved_steps"].append(ap["step"])
                t.update(status="QUEUED", updated_at=iso(now))
                self.log(t, t["assigned_agent"], "APPROVED", "사용자가 승인 — 실행 재개", now)
            else:
                t.update(status="STOPPED", error="사용자가 승인하지 않음", updated_at=iso(now))
                mid = "mem_" + ap["id"]  # 과거 결정 패턴을 기억으로 남긴다 (기억 보관소에서 수정·삭제 가능)
                self.s["c"].setdefault("memories", {})[mid] = {
                    "id": mid, "agent_id": t["assigned_agent"], "scope": "장기", "project_id": "",
                    "content": f"사용자가 '{ap['action']}' 제안을 거절함", "tags": "승인기록", "source": "승인 기록",
                    "confidence": 1, "expires_at": "", "sensitivity": "일반", "user_editable": True,
                    "created_at": iso(now), "updated_at": iso(now)}
                self.log(t, t["assigned_agent"], "REJECTED", "사용자가 거절 — 실행하지 않고 중단", now)
            self.set_status(t["assigned_agent"], "IDLE", now, None)
        self.audit("approval." + ("approve" if approve else "reject"), ap["action"], now)
        self.store.mark(important=True)
        return True

    def task_action(self, tid: str, action: str, now: datetime, rating: Optional[int] = None) -> bool:
        t = self.s["tasks"].get(tid)
        if not t:
            return False
        if action == "stop" and t["status"] in ACTIVE:
            t.update(status="STOPPED", error="사용자가 중단", waiting_ai=None, updated_at=iso(now))
            for ap in self.s["approvals"].values():
                if ap["task_id"] == tid and ap["status"] == "PENDING":
                    ap.update(status="REJECTED", resolved_at=iso(now), updated_at=iso(now))
            self.log(t, t["assigned_agent"], "STOPPED", "사용자가 중단", now)
        elif action == "retry" and t["status"] in ("FAILED", "STOPPED", "BLOCKED"):
            t.update(status="QUEUED", retry_count=0, error="", blocked_reason="", updated_at=iso(now))
            self.log(t, t["assigned_agent"], "RETRY", f"사용자 요청으로 {t['checkpoint']['step'] + 1}단계부터 재시도", now)
        elif action == "rate" and t["status"] == "DONE" and rating in (1, 2, 3, 4, 5):
            a = self.s["agents"][t["assigned_agent"]]
            if t.get("rating"):
                a["stats"]["rating_sum"] -= t["rating"]
                a["stats"]["rating_n"] -= 1
            else:
                a["experience"] += rating  # 사용자 평가도 경험치에 반영
                a["level"], _ = level_of(a["experience"])
            a["stats"]["rating_sum"] += rating
            a["stats"]["rating_n"] += 1
            a["updated_at"] = iso(now)
            t.update(rating=rating, updated_at=iso(now))
        else:
            return False
        self.audit(f"task.{action}", t["title"], now)
        self.store.mark(important=True)
        return True

    # ------------------------------------------------------------ 서버 예약 실행 (브라우저가 닫혀 있어도)
    def background_run(self, now: datetime, max_steps: Optional[int] = None, reason: str = "서버 예약 실행") -> dict:
        """'서버 예약 실행'이 켜진 반복 규칙을 서버에서 실행한다. 로컬 AI가 필요한 단계와 승인이 필요한 단계는 실행하지 않고 기다린다."""
        s = self.s
        if self.active(now):
            return {"ran": False, "reason": "접속 세션이 진행 중이라 화면 쪽 엔진이 처리합니다"}
        if s["settings"].get("paused"):
            return {"ran": False, "reason": "자동화가 일시중지되어 있습니다"}
        max_steps = int(max_steps or s["settings"].get("cron_max_steps", 300))
        created = self.run_rules(now, background=True)
        ses = {"id": "background", "tasks_run": 0, "ai_ready": False, "ai_models": {}, "limit": "", "background": True}
        steps, done = 0, []
        while steps < max_steps:
            if self.usage(now)["steps"] >= s["settings"]["max_steps_per_day"]:
                break
            cands = [t for t in s["tasks"].values() if (t.get("params") or {}).get("bg")
                     and t["status"] in ("NEW", "ANALYZING", "QUEUED", "RUNNING") and not t.get("waiting_ai")]
            if not cands:
                w = self.wf.pick(background=True)
                if not w:
                    break
                self.wf.advance(w, now, ses)
                steps += 1
                continue
            for t in cands:
                if t["status"] in ("NEW", "ANALYZING"):
                    self.prioritize(t, now, False)
                    t["status"] = "QUEUED"
            t = sorted(cands, key=lambda t: (t["status"] != "RUNNING", -t["priority"], t["created_at"]))[0]
            self._step(t, now, ses)
            steps += 1
            if t["status"] == "DONE":
                done.append(t["title"])
        waiting = [f"{t['title']} ({'승인 대기' if t['status'] == 'WAITING_APPROVAL' else t['blocked_reason'] or t['status']})"
                   for t in s["tasks"].values() if (t.get("params") or {}).get("bg")
                   and t["status"] in ("WAITING_APPROVAL", "BLOCKED")]
        self._offline(now)
        out = {"ran": True, "created": created, "steps": steps, "done": done, "waiting": waiting}
        if created or steps:
            msg = f"{reason}: 새 업무 {created}건, 실행 단계 {steps}개, 완료 {len(done)}건" + \
                  (f" ({', '.join(done[:3])})" if done else "") + (f", 대기 {len(waiting)}건" if waiting else "")
            s["inbox"].append({"id": uid("srv_"), "ts": iso(now), "kind": "server_run", "message": msg})
            del s["inbox"][:-100]
            self.log(None, "system", "SERVER_RUN", msg, now)
            self.audit("server.run", msg, now)
            self.store.mark(important=True)
        return out

    # ------------------------------------------------------------ 시스템 점검 자료
    def system_snapshot(self, now: datetime) -> dict:
        s = self.s
        u = dict(self.usage(now))
        cut = iso(now - timedelta(hours=24))
        failed = sum(1 for t in s["tasks"].values() if t["status"] == "FAILED" and t["updated_at"] >= cut)
        sync = self.store.status(now)
        mh = self.month_hours(now)
        size_kb = round(len(self.store.encode()) / 1024)
        st = s["settings"]
        warn = []
        if not sync["durable"]:
            warn.append("주의: 영구 저장소(GitHub)가 연결되지 않았습니다. 서버가 재시작되면 데이터가 사라질 수 있습니다.")
        if sync["error"]:
            warn.append("주의: 최근 저장 시도가 실패했습니다 — " + sync["error"])
        if failed >= 3:
            warn.append(f"주의: 24시간 안에 작업 {failed}건이 실패했습니다.")
        if mh >= 600:
            warn.append(f"주의: 이번 달 서버 가동 추정 {mh}시간 — Render 무료 750시간에 가까워지고 있습니다.")
        if size_kb > 800:
            warn.append(f"주의: 상태 파일이 {size_kb}KB입니다. 오래된 기록 정리를 권장합니다.")
        if sync["durable"] and not sync["encrypted"]:
            warn.append("참고: 데이터 암호화(DATA_ENCRYPTION_KEY)가 꺼져 있습니다. 비공개 저장소만으로는 충분한 보호가 아닙니다.")
        return {"sync": sync, "failed_24h": failed, "usage": u, "month_hours": mh, "size_kb": size_kb,
                "pending": sum(1 for a in s["approvals"].values() if a["status"] == "PENDING"),
                "limits": {k: st[k] for k in ("max_steps_per_day", "ai_daily_limit", "max_tasks_per_session",
                                              "commits_per_day", "render_hours_limit")},
                "warnings": warn}
