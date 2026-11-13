"""다단계 워크플로 실행 엔진 (상태 머신).

GOAL → CONTEXT → PLAN → EXECUTE → OBSERVE → VERIFY → (REVISE) → … → COMPLETE

- 한 번의 advance()는 상태 하나만 전이하고 저장한다 → 어느 지점에서 끊겨도 이어서 실행할 수 있다.
- 결정적으로 처리할 수 있는 단계는 일반 코드(도구)로 실행하고, 문장 생성이 필요한 단계만 로컬 AI 작업을 만든다.
- 모든 전이와 근거는 trace에 남는다. 성공 메시지는 실제 도구 결과와 검증 결과로만 만든다.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from typing import Optional

from . import analysis as A
from . import memory as M
from . import planner as P
from .agents import level_of
from .rules import RULES
from .tools import TOOLS, NeedAIJob, TCtx, ToolError
from .util import dadd, ddiff, iso, parse, short_hash, today, uid, week_key

ACTIVE = ("RUNNING", "WAITING_APPROVAL", "WAITING_INPUT", "WAITING_AI", "BLOCKED")
SKIP_CAUSES = ("disabled", "not_found", "no_ai", "limit", "ai_interrupted", "timeout")
TIER_DEFAULT = {"classify": "light", "summary": "light", "tags": "light",
                "plan": "strong", "analysis": "strong", "verify": "strong", "coordinate": "strong", "answer": "strong"}


def digest(v) -> str:
    return hashlib.sha256(json.dumps(v, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:12]


class Workflows:
    def __init__(self, eng):
        self.eng = eng

    @property
    def s(self):
        return self.eng.s

    @property
    def all(self) -> dict:
        return self.s["workflows"]

    # ------------------------------------------------------------ 기록
    def trace(self, wf: dict, phase: str, msg: str, now, step: str | None = None, data=None) -> None:
        wf["trace"].append({"ts": iso(now), "phase": phase, "step": step, "msg": msg[:400],
                            **({"data": data} if data is not None else {})})
        if len(wf["trace"]) > 300:
            del wf["trace"][:50]
        wf["updated_at"] = iso(now)
        self.eng.log(None, self.step_agent(wf), "WORKFLOW", f"[{wf['title'][:24]}] {phase}: {msg[:160]}", now)

    def step_agent(self, wf: dict) -> str:
        st = self.get_step(wf, wf.get("cursor"))
        return st["agent"] if st and wf["phase"] in ("EXECUTE", "OBSERVE", "VERIFY", "REVISE") else wf["lead"]

    @staticmethod
    def get_step(wf: dict, sid: Optional[str]) -> Optional[dict]:
        return next((x for x in wf["plan"] if x["id"] == sid), None) if sid else None

    # ------------------------------------------------------------ 생성
    def new(self, goal: str, now, source: str = "user", agent: str | None = None, project_id: str | None = None,
            intent: dict | None = None, status: str = "RUNNING", key: str | None = None, evidence: list | None = None):
        it = intent or P.interpret(self.s, now, goal, agent, project_id)
        if it["template"] == "rule_task":
            kind = it["params"]["kind"]
            t = self.eng.add_task(kind, RULES[kind]["title"], RULES[kind]["agent"], "user:" + uid(), now,
                                  [f"사용자 지시: {goal[:100]}"], "user", importance=2)
            return {"type": "task", "task": t, "intent": it}
        key = key or f"{source}:{uid()}"
        if any(w["idempotency_key"] == key for w in self.all.values()):
            return {"type": "duplicate"}
        wf = {"id": uid("wf_"), "title": it["title"], "goal": goal, "template": it["template"], "params": it["params"],
              "lead": it["lead"], "project_id": it.get("project_id"), "project_title": it.get("project_title"),
              "source": source, "status": status, "phase": "GOAL", "plan": [], "cursor": None,
              "context": {}, "plan_approved": False, "trace": [], "result": None, "error": "", "recovery": None,
              "evidence": evidence or [f"사용자 요청: {goal[:200]}"], "rating": None, "idempotency_key": key,
              "created_at": iso(now), "updated_at": iso(now), "started_at": None, "completed_at": None}
        self.all[wf["id"]] = wf
        self.trace(wf, "GOAL", ("제안됨: " if status == "PROPOSED" else "목표 접수: ") + goal[:200], now)
        self.eng.store.mark(important=True)
        return {"type": "workflow", "workflow": wf, "intent": it}

    def propose(self, template: str, title: str, params: dict, evidence: list, key: str, now, lead: str,
                project_id: str | None = None) -> Optional[dict]:
        if any(w["idempotency_key"] == key for w in self.all.values()):
            return None
        if sum(1 for w in self.all.values() if w["status"] == "PROPOSED") >= 10:
            return None
        it = {"template": template, "title": title, "params": params, "lead": lead, "project_id": project_id,
              "project_title": None}
        r = self.new(title, now, "proposal", intent=it, status="PROPOSED", key=key, evidence=evidence)
        return r.get("workflow")

    # ------------------------------------------------------------ 선택
    def pick(self, background: bool = False) -> Optional[dict]:
        c = [w for w in self.all.values() if w["status"] == "RUNNING"
             and not self.s["agents"].get(self.step_agent(w), {}).get("paused")
             and (not background or w["source"].startswith("schedule"))]
        if not c:
            return None
        return sorted(c, key=lambda w: (w["source"] != "user", w["created_at"]))[0]

    def resolve(self, wf: dict, val, refs: dict):
        if isinstance(val, str) and val.startswith("$"):
            parts = val[1:].split(".")
            if parts[0] == "ctx":
                cur = wf["context"]
                parts = ["rule_values"] + parts[2:] if len(parts) > 1 and parts[1] == "rules" else parts[1:]
            else:
                st = self.get_step(wf, parts[0])
                cur = st and st.get("output")
                parts = parts[1:]
            for p in parts:
                if isinstance(cur, list) and p.isdigit():
                    cur = cur[int(p)] if int(p) < len(cur) else None
                elif isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    cur = None
            refs[val] = digest(cur)
            return cur
        if isinstance(val, dict):
            return {k: self.resolve(wf, v, refs) for k, v in val.items()}
        if isinstance(val, list):
            return [self.resolve(wf, v, refs) for v in val]
        return val

    # ------------------------------------------------------------ 상태 전이
    def advance(self, wf: dict, now, ses: dict) -> str:
        ph = wf["phase"]
        try:
            return getattr(self, "_" + ph.lower())(wf, now, ses)
        except Exception as e:  # 엔진 자체 오류도 기록하고 멈춘다 (무한 반복 방지)
            return self._fail_wf(wf, now, wf.get("cursor"), "engine", f"실행 엔진 오류: {e}")
        finally:
            self.eng.store.mark(important=True)

    def _goal(self, wf, now, ses):
        wf["started_at"] = wf["started_at"] or iso(now)
        self.eng.set_status(wf["lead"], "PLANNING", now, wf["id"])
        info = f"템플릿 {wf['template']}" + (f", 프로젝트 「{wf['project_title']}」" if wf.get("project_title") else "")
        assumed = wf["params"].get("assumed") if isinstance(wf["params"], dict) else None
        self.trace(wf, "GOAL", f"최종 목표 파악 — {info}" + (f" · 가정: {', '.join(assumed)}" if assumed else ""), now)
        wf["phase"] = "CONTEXT"
        return "goal"

    def _context(self, wf, now, ses):
        res = M.search(self.s, now, wf["goal"], wf.get("project_id"), None)
        rules = P.context_rules(res["rules"])
        wf["context"] = {"memories": res["active"][:12], "superseded": res["superseded"], "scope": res["scope"],
                         "rules": res["rules"], "rule_values": rules["values"], "applied_rules": rules["applied"]}
        self.trace(wf, "CONTEXT", f"기억 검색({res['scope']}): {len(res['active'])}건, 확정 규칙 {len(res['rules'])}건"
                   + (f", 최신 지시로 대체된 기억 {len(res['superseded'])}건" if res["superseded"] else "")
                   + (" · 적용 규칙: " + ", ".join(a["label"] for a in rules["applied"]) if rules["applied"] else ""), now)
        wf["phase"] = "PLAN"
        return "context"

    def _plan(self, wf, now, ses):
        plan = P.build(wf, self.s, now)
        errs = P.validate(plan)
        if errs:
            return self._fail_wf(wf, now, None, "bad_plan", "계획 검증 실패: " + "; ".join(errs))
        wf["plan"] = plan
        self.trace(wf, "PLAN", f"{len(plan)}단계 계획: " + " → ".join(
            f"{x['id']}:{self.s['agents'][x['agent']]['name']}·{TOOLS[x['tool']]['label']}" for x in plan), now)
        self.eng.set_status(wf["lead"], "IDLE", now, None)
        wf["phase"] = "EXECUTE"
        return "plan"

    def _execute(self, wf, now, ses):
        plan = wf["plan"]
        done = {x["id"] for x in plan if x["status"] in ("DONE", "SKIPPED")}
        nxt = next((x for x in plan if x["status"] == "PENDING" and all(d in done for d in x["depends_on"])), None)
        if not nxt:
            if all(x["status"] in ("DONE", "SKIPPED") for x in plan):
                wf["phase"] = "COMPLETE"
                return "to-complete"
            return self._fail_wf(wf, now, None, "stuck", "선행 단계가 끝나지 않아 더 진행할 수 없음")
        st, tl = nxt, TOOLS[nxt["tool"]]
        agent = self.s["agents"][st["agent"]]
        wf["cursor"] = st["id"]
        missing = [w for w in tl["writes"] if w not in agent["permissions"]["write"]]
        if missing:
            wf.update(status="BLOCKED", blocked={"kind": "permission", "agent": agent["id"], "collections": missing,
                                                 "step": st["id"]})
            self.eng.set_status(agent["id"], "BLOCKED", now, wf["id"])
            self.trace(wf, "EXECUTE", f"{st['id']} 막힘 — {agent['name']}에게 {', '.join(missing)} 쓰기 권한이 없음 "
                       "(직원 패널에서 허용하면 자동 재개)", now, st["id"])
            return "blocked"
        refs: dict = {}
        args = {**self.resolve(wf, st["args"], refs), **st.get("override", {})}
        if st.get("skip_if_empty") and not args.get(st["skip_if_empty"]):
            return self._skip(wf, st, now, f"앞 단계에서 넘겨받은 {st['skip_if_empty']}이(가) 비어 있어 실행할 것이 없음")
        needs = (tl["risk"] == "HIGH" and not st["approved"]) or (tl["risk"] == "MEDIUM" and not (
            st["approved"] or wf["plan_approved"] or agent["permissions"].get("auto_medium")))
        if needs and tl.get("dry"):  # 승인을 묻기 전에, 저장 없이 미리 검사해 문제(충돌 등)가 있으면 먼저 알린다
            try:
                tl["fn"](TCtx(self.eng, wf, st, now), {**args, "_dry": True})
            except ToolError as e:
                st["resolved_args"] = args
                return self._error(wf, st, now, e.cause, e.msg, e.data)
        if tl["risk"] == "HIGH" and not st["approved"]:
            return self._ask_approval(wf, st, now, "step", args)
        if tl["risk"] == "MEDIUM" and not (st["approved"] or wf["plan_approved"] or agent["permissions"].get("auto_medium")):
            return self._ask_approval(wf, st, now, "plan", args)
        st["inputs_from"] = sorted({r[1:].split(".")[0] for r in refs if not r.startswith("$ctx")})
        st["input_digest"] = refs
        st["resolved_args"] = args
        st["status"] = "RUNNING"
        st["started_at"] = st["started_at"] or iso(now)
        self.eng.set_status(agent["id"], "WORKING", now, wf["id"])
        ctx = TCtx(self.eng, wf, st, now)
        import time
        t0 = time.monotonic()
        try:
            out = tl["fn"](ctx, args)
        except NeedAIJob as e:
            return self._need_ai(wf, st, e, now, ses)
        except ToolError as e:
            return self._error(wf, st, now, e.cause, e.msg, e.data)
        except Exception as e:
            return self._error(wf, st, now, "exception", f"{type(e).__name__}: {e}")
        finally:
            st["ms"] += round((time.monotonic() - t0) * 1000)
            self.eng.usage(now)["steps"] += 1
        st["output"] = out
        if st["ms"] > st["timeout_sec"] * 1000:
            self.trace(wf, "EXECUTE", f"{st['id']} 제한 시간 {st['timeout_sec']}초 초과 ({st['ms']}ms) — 결과는 검증 단계에서 확인",
                       now, st["id"])
        self.trace(wf, "EXECUTE", f"{st['id']} {agent['name']} · 도구 {tl['name']} 실행 ({st['ms']}ms)"
                   + (f" · 입력: {', '.join(st['inputs_from'])}의 산출물" if st["inputs_from"] else ""), now, st["id"])
        wf["phase"] = "OBSERVE"
        return "execute"

    def _observe(self, wf, now, ses):
        st = self.get_step(wf, wf["cursor"])
        out = st["output"] or {}
        parts = []
        for k, v in out.items():
            if isinstance(v, list):
                parts.append(f"{k} {len(v)}건")
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                parts.append(f"{k}={v}")
            elif isinstance(v, str) and v and k in ("title", "url", "mode", "topic", "summary", "date"):
                parts.append(f"{k}={v[:40]}")
        st["observation"] = ", ".join(parts)[:300]
        self.trace(wf, "OBSERVE", f"{st['id']} 결과: {st['observation'] or '(빈 결과)'}", now, st["id"])
        wf["phase"] = "VERIFY"
        return "observe"

    def _verify(self, wf, now, ses):
        st = self.get_step(wf, wf["cursor"])
        tl = TOOLS[st["tool"]]
        ctx = TCtx(self.eng, wf, st, now)
        try:
            checks = tl["verify"](ctx, st["resolved_args"], st["output"]) if tl["verify"] else []
        except Exception as e:
            checks = [{"name": "검증 실행", "passed": False, "detail": str(e)}]
        st["verify"] = {"passed": all(c["passed"] for c in checks), "checks": checks, "at": iso(now)}
        if st["tool"] == "ai.generate" and (st["output"] or {}).get("mode") == "ai":
            self._ai_stat(st["scratch"].get("ai_model", "?"), st["resolved_args"]["task_type"], st["verify"]["passed"],
                          st["scratch"].get("ai_ms", 0), now)
        failed = [c for c in checks if not c["passed"]]
        self.trace(wf, "VERIFY", f"{st['id']} 검증 " + ("통과" if not failed else "실패") + " — "
                   + "; ".join(f"{'✔' if c['passed'] else '✘'} {c['name']}" + (f"({c['detail']})" if c["detail"] else "")
                               for c in checks), now, st["id"])
        if failed:
            return self._error(wf, st, now, "verify:" + failed[0]["name"], failed[0]["detail"] or failed[0]["name"])
        self._step_done(wf, st, now)
        wf["phase"] = "EXECUTE"
        return "verify"

    def _revise(self, wf, now, ses):
        st = self.get_step(wf, wf["cursor"])
        errs = st["errors"][st.get("err_base", 0):]
        e = errs[-1]
        cause = e["cause"]
        repeated = len(errs) >= 2 and errs[-2]["cause"] == cause and cause != "conflict"
        over = st["attempts"] > st["max_retries"]
        if cause == "conflict" and st["on_fail"] == "ask":
            if st.get("choices_asked", 0) >= 10:
                return self._fail_wf(wf, now, st["id"], cause, "대안 선택이 10번을 넘어 중단")
            return self._ask_choice(wf, st, e, now)
        if cause == "needs_approval":
            return self._ask_approval(wf, st, now, "step", st.get("resolved_args") or {})
        if st["on_fail"] == "skip" and (cause in SKIP_CAUSES or repeated or over or cause.startswith("verify")):
            return self._skip(wf, st, now, f"{e['msg']} — 이 단계는 건너뛰고 계속 진행")
        if st["on_fail"] == "revise" and st.get("revisions", 0) < 1 and cause.startswith("verify") and st.get("revise_target"):
            out = st["output"] or {}
            fix = (out.get("missing_concepts") or []) + (out.get("missing_sources") or [])
            tgt = st["revise_target"]
            ids = [x["id"] for x in wf["plan"]]
            for x in wf["plan"][ids.index(tgt): ids.index(st["id"]) + 1]:
                x.update(status="PENDING", output=None, verify=None)
            self.get_step(wf, tgt).setdefault("override", {})["fix"] = fix
            st["revisions"] = st.get("revisions", 0) + 1
            st["err_base"] = len(st["errors"])
            self.trace(wf, "REVISE", f"{st['id']} 검사에서 누락 발견({', '.join(fix) or e['msg']}) → {tgt} 단계부터 보충해 다시 만듦",
                       now, st["id"])
            wf["phase"] = "EXECUTE"
            return "revise"
        if st["on_fail"] in ("abort",) or repeated or over or cause in ("bad_input", "engine"):
            why = "같은 오류가 반복됨" if repeated else f"재시도 한도({st['max_retries']}회) 초과" if over else \
                "복구할 수 없는 오류" if cause in ("bad_input", "engine") else "이 단계는 실패 시 중단하도록 계획됨"
            return self._fail_wf(wf, now, st["id"], cause, f"{e['msg']} — {why}")
        if st["tool"] == "doc.create" and cause == "verify:필수 내용 포함":
            st.setdefault("override", {})["fix"] = [x.strip() for x in e["msg"].replace("빠짐:", "").split(",") if x.strip()]
            note = "빠진 필수 내용을 본문에 보충해 다시 생성"
        elif st["tool"] == "ai.generate" and cause in ("timeout", "ai_interrupted"):
            st["scratch"]["force_fallback"] = True
            note = "AI 응답이 없어 허용된 비AI 대체 방식으로 다시 실행"
        else:
            note = "같은 단계를 다시 실행"
        st["status"] = "PENDING"
        self.trace(wf, "REVISE", f"{st['id']} 원인 '{cause}' → {note} ({st['attempts']}/{st['max_retries']}회)", now, st["id"])
        wf["phase"] = "EXECUTE"
        return "revise"

    def _complete(self, wf, now, ses):
        plan = wf["plan"]
        last = next((x for x in reversed(plan) if x["tool"] == "report.compose" and x["status"] == "DONE"), None)
        out = (last or {}).get("output") or {"summary": "완료", "lines": [], "links": []}
        files = [f for f in self.s["files"].values() if f.get("by_wf") == wf["id"]]
        links = [l for l in out.get("links", []) if isinstance(l, dict) and l.get("url")]
        wf["result"] = {"summary": out["summary"], "lines": out["lines"][:40], "links": links,
                        "files": [{"id": f["id"], "name": f["name"], "url": f"/api/files/{f['id']}", "size": f["size"]}
                                  for f in files],
                        "skipped": [f"{x['id']} {x['goal']}: {x.get('skip_reason', '')}" for x in plan if x["status"] == "SKIPPED"],
                        "applied_rules": [a["label"] for a in wf["context"].get("applied_rules", [])]}
        if wf["template"] != "remember":
            m = M.save(self.s, now, f"[{wf['title']}] {out['summary']} — " + " / ".join(out["lines"][:3]),
                       "실행 결과", wf["lead"], wf.get("project_id") or "", f"wf:{wf['template']}:{wf.get('project_id') or ''}",
                       "실행결과", f"워크플로 {wf['id']}")
            wf["result"]["memory_id"] = m["id"]
        for aid in {x["agent"] for x in plan if x["status"] == "DONE"}:
            a = self.s["agents"][aid]
            n = sum(1 for x in plan if x["agent"] == aid and x["status"] == "DONE")
            a["experience"] += 5 * n
            a["level"], _ = level_of(a["experience"])
            a["stats"]["done"] += 1 if aid == wf["lead"] else 0
            a["updated_at"] = iso(now)
        wf.update(status="DONE", phase="COMPLETE", completed_at=iso(now), cursor=None)
        self.eng.set_status(wf["lead"], "COMPLETED", now, None)
        self.trace(wf, "COMPLETE", f"완료: {out['summary']} · 파일 {len(files)}개 · 건너뛴 단계 {len(wf['result']['skipped'])}개", now)
        return "complete"

    # ------------------------------------------------------------ 보조
    def _step_done(self, wf, st, now):
        st.update(status="DONE", finished_at=iso(now))
        p = self.eng.perf(st["agent"])
        errs = st["errors"]
        p["units_done"] += 1
        p["verified"] += 1
        p["first_pass"] += 1 if not errs else 0
        p["with_error"] += 1 if errs else 0
        p["retries"] += st["attempts"]
        p["time_ms"] += max(0, int((parse(st["finished_at"]) - parse(st["started_at"])).total_seconds() * 1000)) + st["ms"] \
            if st["started_at"] else st["ms"]
        p["time_n"] += 1
        self.eng.set_status(st["agent"], "COMPLETED", now, None)

    def _skip(self, wf, st, now, why: str) -> str:
        st.update(status="SKIPPED", skip_reason=why[:200], finished_at=iso(now))
        self.eng.set_status(st["agent"], "IDLE", now, None)
        self.trace(wf, "REVISE", f"{st['id']} 건너뜀: {why}", now, st["id"])
        wf["phase"] = "EXECUTE"
        return "skip"

    def _error(self, wf, st, now, cause: str, msg: str, data: dict | None = None) -> str:
        st["attempts"] += 1
        st["errors"].append({"ts": iso(now), "cause": cause, "msg": str(msg)[:300], "data": data or {},
                             "attempt": st["attempts"]})
        st["status"] = "FAILED_ATTEMPT"
        self.eng.set_status(st["agent"], "ERROR", now, None)
        self.trace(wf, "EXECUTE" if not cause.startswith("verify") else "VERIFY",
                   f"{st['id']} 오류 [{cause}] {msg}", now, st["id"])
        wf["phase"] = "REVISE"
        return "error"

    def _fail_wf(self, wf, now, sid, cause, msg) -> str:
        st = self.get_step(wf, sid)
        if st:
            st["status"] = "FAILED"
            p = self.eng.perf(st["agent"])
            p["units_failed"] += 1
            p["with_error"] += 1
            p["retries"] += st["attempts"]
            self.eng.set_status(st["agent"], "ERROR", now, None)
        done = [x["id"] for x in wf["plan"] if x["status"] == "DONE"]
        wf.update(status="FAILED", error=msg[:300], recovery={
            "step": sid, "cause": cause, "message": msg[:300], "attempts": st["attempts"] if st else 0,
            "completed_steps": done, "resumable": bool(st),
            "resume_hint": (f"{sid} 단계부터 다시 실행할 수 있습니다. 완료된 {len(done)}단계의 산출물은 그대로 씁니다."
                            if st else "계획을 다시 세워야 합니다.")})
        self.trace(wf, "REVISE", f"중단 — 원인 [{cause}] {msg} · 복구 상태: " + wf["recovery"]["resume_hint"], now, sid)
        return "failed"

    def _ask_approval(self, wf, st, now, kind: str, args: dict) -> str:
        if kind == "plan":
            meds = [x for x in wf["plan"] if x["status"] == "PENDING" and TOOLS[x["tool"]]["risk"] == "MEDIUM"]
            detail = [f"{x['id']} {self.s['agents'][x['agent']]['name']}: {x['goal']} ({TOOLS[x['tool']]['label']})" for x in meds]
            detail += preview(st, args)
            action, risk = f"{wf['title']} — 계획 실행 승인 (중간 위험 단계 {len(meds)}개)", "MEDIUM"
        else:
            detail = [f"{st['id']} {st['goal']} ({TOOLS[st['tool']]['label']})"] + preview(st, args)
            action, risk = f"{wf['title']} — {st['goal']}", TOOLS[st["tool"]]["risk"]
        ap = {"id": uid("ap_"), "workflow_id": wf["id"], "task_id": None, "step": st["id"], "kind": kind,
              "agent_id": st["agent"], "action": action, "detail": detail, "risk_level": risk, "options": [],
              "status": "PENDING", "requested_at": iso(now), "resolved_at": None, "updated_at": iso(now)}
        self.s["approvals"][ap["id"]] = ap
        wf["status"] = "WAITING_APPROVAL"
        self.eng.set_status(st["agent"], "WAITING_APPROVAL", now, wf["id"])
        self.trace(wf, "EXECUTE", f"{st['id']} 실행 전 사용자 승인 요청 [{risk}]", now, st["id"])
        return "approval"

    def _ask_choice(self, wf, st, e, now) -> str:
        d = e["data"]
        if st["tool"] == "event.update":
            opts = [{"label": o["label"], "value": {"update_choice": o["value"]}} for o in d.get("options", [])]
        else:
            i = d.get("item", 0)
            opts = [{"label": f"{a['date']} {a['start']}~{a['end']}로 등록", "value": {"item": i, "set": a}}
                    for a in d.get("alternatives", [])[:5]]
            opts.append({"label": "겹쳐도 그대로 등록", "value": {"item": i, "set": {"allow_conflict": True}}})
        opts.append({"label": "이 작업 취소", "value": {"cancel": True}})
        ap = {"id": uid("ap_"), "workflow_id": wf["id"], "task_id": None, "step": st["id"], "kind": "choice",
              "agent_id": st["agent"], "action": f"{wf['title']} — 충돌 발견, 대안을 골라 주세요",
              "detail": [e["msg"]], "risk_level": "MEDIUM", "options": opts, "status": "PENDING",
              "requested_at": iso(now), "resolved_at": None, "updated_at": iso(now)}
        self.s["approvals"][ap["id"]] = ap
        st["status"] = "WAITING_INPUT"
        st["choices_asked"] = st.get("choices_asked", 0) + 1
        wf["status"] = "WAITING_INPUT"
        self.eng.set_status(st["agent"], "WAITING_APPROVAL", now, wf["id"])
        self.trace(wf, "REVISE", f"{st['id']} 충돌 → 대안 {len(opts) - 2}개를 제시하고 사용자 선택 대기", now, st["id"])
        return "choice"

    def resolve_approval(self, ap: dict, approve: bool, choice: Optional[int], now) -> bool:
        wf = self.all.get(ap["workflow_id"])
        if not wf or wf["status"] not in ("WAITING_APPROVAL", "WAITING_INPUT"):
            return False
        st = self.get_step(wf, ap["step"])
        if ap["kind"] == "choice":
            if choice is None or not (0 <= choice < len(ap["options"])):
                return False
            val = ap["options"][choice]["value"]
            ap["chosen"] = ap["options"][choice]["label"]
            if val.get("cancel"):
                approve = False
            else:
                ov = st.setdefault("override", {})
                if "update_choice" in val:
                    ch = val["update_choice"]
                    ov.setdefault("choices", {})[ch["id"]] = "keep" if ch.get("keep") else \
                        {k: ch[k] for k in ("date", "start", "end")}
                else:
                    args = st.get("resolved_args") or {}
                    items = [dict(x) for x in (ov.get("items") or args.get("items") or [args])]
                    items[val["item"]].update(val["set"])
                    if "end" in val["set"] or "allow_conflict" in val["set"]:
                        pass
                    if args.get("items") or ov.get("items"):
                        ov["items"] = items
                    else:
                        ov.update({k: v for k, v in items[0].items() if k in ("date", "start", "end", "allow_conflict")})
                st.update(status="PENDING", approved=True)
                wf["status"] = "RUNNING"
                wf["phase"] = "EXECUTE"
                self.trace(wf, "REVISE", f"{st['id']} 사용자가 선택: {ap['chosen']} → 다시 실행", now, st["id"])
        if not approve:
            wf.update(status="STOPPED", error="사용자가 승인하지 않음")
            if st:
                st["status"] = "SKIPPED" if st["status"] != "DONE" else st["status"]
            M.save(self.s, now, f"사용자가 '{ap['action']}' 실행을 거절함", "확정 사실", ap["agent_id"],
                   wf.get("project_id") or "", "", "승인기록", "승인 기록")
            self.trace(wf, "EXECUTE", "사용자가 거절 — 실행하지 않고 중단", now, ap["step"])
        elif ap["kind"] == "plan":
            wf.update(plan_approved=True, status="RUNNING")
            self.trace(wf, "EXECUTE", "사용자가 계획을 승인 — 중간 위험 단계 실행 허용", now, ap["step"])
        elif ap["kind"] == "step":
            st["approved"] = True
            st["status"] = "PENDING"
            wf.update(status="RUNNING", phase="EXECUTE")
            self.trace(wf, "EXECUTE", f"{st['id']} 사용자가 승인 — 실행 재개", now, st["id"])
        self.eng.set_status(ap["agent_id"], "IDLE", now, None)
        return True

    # ------------------------------------------------------------ 로컬 AI
    def route(self, task_type: str, ses: dict) -> tuple:
        models = (ses or {}).get("ai_models") or {}
        tier = TIER_DEFAULT.get(task_type, "strong")
        reason = f"작업 유형 '{task_type}' 기본 → {'경량' if tier == 'light' else '상위'} 모델"
        if tier == "light" and models.get("light") and models.get("strong"):
            st = self.s["ai_stats"].get(f"{models['light']}|{task_type}")
            if st and st["n"] >= 5 and st["ok"] / st["n"] < 0.7:
                tier = "strong"
                reason = f"경량 모델의 '{task_type}' 검증 통과율 {round(100 * st['ok'] / st['n'])}% (<70%) → 상위 모델로 변경"
        if not models.get(tier):
            other = "strong" if tier == "light" else "light"
            if models.get(other):
                reason += f" · {('경량' if tier == 'light' else '상위')} 모델이 없어 {('상위' if other == 'strong' else '경량')} 모델 사용"
                tier = other
        return tier, models.get(tier) or (ses or {}).get("ai_model") or "", reason

    def _ai_stat(self, model: str, task_type: str, ok: bool, ms: int, now) -> None:
        k = f"{model}|{task_type}"
        st = self.s["ai_stats"].setdefault(k, {"id": k, "model": model, "task_type": task_type, "n": 0, "ok": 0, "ms": 0})
        st["n"] += 1
        st["ok"] += 1 if ok else 0
        st["ms"] += int(ms or 0)
        st["updated_at"] = iso(now)

    def _need_ai(self, wf, st, e: NeedAIJob, now, ses) -> str:
        tier, model, reason = self.route(e.task_type, ses)
        cache = self.s["meta"].setdefault("ai_cache", {})
        ck = digest([e.system, e.prompt, model])
        if ck in cache:
            st["scratch"].update(ai_text=cache[ck]["text"], ai_model=cache[ck]["model"], ai_ms=0, cached=True)
            st["status"] = "PENDING"
            self.trace(wf, "EXECUTE", f"{st['id']} 같은 입력의 이전 AI 결과를 캐시에서 재사용 (추론 생략)", now, st["id"])
            return "ai-cache"
        job = {"id": uid("job_"), "kind": "workflow", "workflow_id": wf["id"], "step_id": st["id"], "agent_id": st["agent"],
               "task_type": e.task_type, "tier": tier, "model": model, "route_reason": reason,
               "system": e.system, "prompt": e.prompt, "status": "pending", "created_at": iso(now), "updated_at": iso(now),
               "cache_key": ck}
        self.s["ai_jobs"][job["id"]] = job
        self.eng.usage(now)["model_calls"] += 1
        st.update(status="WAITING_AI", waiting_job=job["id"], ai_wait_since=iso(now))
        wf["status"] = "WAITING_AI"
        self.trace(wf, "EXECUTE", f"{st['id']} 로컬 AI 작업 요청 — {reason}", now, st["id"])
        return "ai-wait"

    def ai_result(self, job: dict, text: str, error: str, model: str, ms: int, now) -> bool:
        wf = self.all.get(job.get("workflow_id"))
        st = wf and self.get_step(wf, job["step_id"])
        if not st or st.get("waiting_job") != job["id"] or st["status"] != "WAITING_AI":
            return False
        st["waiting_job"] = None
        wf["status"] = "RUNNING"
        used = model or job.get("model") or "?"
        if error or not text.strip():
            self._ai_stat(used, job["task_type"], False, ms, now)
            st["status"] = "RUNNING"
            wf["cursor"] = st["id"]
            return self._error(wf, st, now, "ai_interrupted", f"로컬 AI 실패: {error or '빈 응답'}") is not None
        st["scratch"].update(ai_text=text[:6000], ai_model=used, ai_ms=ms)
        cache = self.s["meta"].setdefault("ai_cache", {})
        cache[job["cache_key"]] = {"text": text[:6000], "model": used, "ts": iso(now)}
        while len(cache) > 60:
            cache.pop(next(iter(cache)))
        st["status"] = "PENDING"
        self.trace(wf, "OBSERVE", f"{st['id']} 로컬 AI 응답 수신 ({used}, {len(text)}자, {ms}ms)", now, st["id"])
        return True

    def check_timeouts(self, now) -> None:
        for wf in self.all.values():
            if wf["status"] != "WAITING_AI":
                continue
            for st in wf["plan"]:
                if st["status"] != "WAITING_AI":
                    continue
                job = self.s["ai_jobs"].get(st.get("waiting_job") or "")
                waited = (now - parse(st.get("ai_wait_since") or iso(now))).total_seconds()
                if not job or job["status"] != "pending":
                    cause, msg = "ai_interrupted", "AI 작업이 중단됨 (브라우저 종료·재접속)"
                elif waited > st["timeout_sec"]:
                    job.update(status="expired", updated_at=iso(now))
                    cause, msg = "timeout", f"로컬 AI 응답 제한 시간 {st['timeout_sec']}초 초과"
                else:
                    continue
                st["waiting_job"] = None
                wf.update(status="RUNNING", cursor=st["id"])
                st["status"] = "RUNNING"
                self._error(wf, st, now, cause, msg)
                self.eng.store.mark(important=True)

    # ------------------------------------------------------------ 사용자 조작
    def action(self, wid: str, act: str, now, rating: Optional[int] = None) -> bool:
        wf = self.all.get(wid)
        if not wf:
            return False
        if act == "accept" and wf["status"] == "PROPOSED":
            wf.update(status="RUNNING", source="proposal")
            self.trace(wf, "GOAL", "사용자가 제안을 수락 — 실행 시작 (중간 위험 단계는 계획을 보여 준 뒤 다시 승인 요청)", now)
        elif act == "dismiss" and wf["status"] == "PROPOSED":
            wf.update(status="STOPPED", error="사용자가 제안을 거절")
            self.trace(wf, "GOAL", "사용자가 제안을 거절", now)
        elif act == "stop" and wf["status"] in ACTIVE:
            wf.update(status="STOPPED", error="사용자가 중단")
            for ap in self.s["approvals"].values():
                if ap.get("workflow_id") == wid and ap["status"] == "PENDING":
                    ap.update(status="REJECTED", resolved_at=iso(now), updated_at=iso(now))
            self.trace(wf, wf["phase"], "사용자가 중단", now)
        elif act == "retry" and wf["status"] in ("FAILED", "STOPPED", "BLOCKED"):
            for st in wf["plan"]:
                if st["status"] in ("FAILED", "FAILED_ATTEMPT", "RUNNING", "WAITING_AI", "WAITING_INPUT") or \
                        (wf["status"] == "STOPPED" and st["status"] == "SKIPPED" and not st.get("skip_reason")):
                    st.update(status="PENDING", attempts=0, err_base=len(st["errors"]))
            wf.update(status="RUNNING", error="", recovery=None, blocked=None,
                      phase="EXECUTE" if wf["plan"] else "GOAL")
            self.trace(wf, "REVISE", "사용자 요청으로 마지막 체크포인트부터 다시 실행 (완료된 단계는 건너뜀)", now)
        elif act == "rate" and wf["status"] == "DONE" and rating in (1, 2, 3, 4, 5):
            p = self.eng.perf(wf["lead"])
            if wf.get("rating"):
                p["rating_sum"] -= wf["rating"]
                p["rating_n"] -= 1
            p["rating_sum"] += rating
            p["rating_n"] += 1
            wf["rating"] = rating
        else:
            return False
        wf["updated_at"] = iso(now)
        self.eng.store.mark(important=True)
        return True

    def unblock(self, now) -> None:
        """권한 막힘이 풀렸으면 자동 재개."""
        for wf in self.all.values():
            b = wf.get("blocked")
            if wf["status"] == "BLOCKED" and b and b.get("kind") == "permission":
                perms = self.s["agents"][b["agent"]]["permissions"]["write"]
                if all(c in perms for c in b["collections"]):
                    wf.update(status="RUNNING", blocked=None)
                    self.trace(wf, "EXECUTE", "권한이 허용되어 자동 재개", now)

    # ------------------------------------------------------------ 능동적 업무 발견 (제안 상태로만 저장)
    def discover_proposals(self, now) -> int:
        s, td, n = self.s, today(self.s, now), 0
        wk = week_key(td)
        rules = P.context_rules(M.search(s, now, "")["rules"])["values"]
        evs = A.V(s, "events")
        for e in evs:
            if "발표" in str(e.get("title")) and td < e.get("date", "") <= dadd(td, 14) and not e.get("done"):
                practice = [x for x in evs if any(k in str(x.get("title")) for k in ("리허설", "연습"))
                            and td <= x.get("date", "") <= e["date"]]
                if not practice:
                    days = ddiff(e["date"], td)
                    n += bool(self.propose("time_blocks", f"「{e['title']}」 리허설 일정 잡기",
                                           {"label": f"{e['title']} 리허설", "before": e["date"],
                                            "count": min(rules["rehearsals"], max(1, days - 1)), "duration_min": 60},
                                           [f"발표 「{e['title']}」 {e['date']} (D-{days})", "그 전까지 리허설·연습 일정이 없음"],
                                           f"prop:rehearsal:{e['id']}:{wk}", now, "schedule"))
        for x in A.a_study(s, now)["subjects"]:
            if x["dday"] is not None and 0 < x["dday"] <= 14 and x["remaining"] > 0 and x["need_per_day"]:
                need_min = x["need_per_day"] * (x["minutes_per_unit"] or 0)
                recent = x["week_minutes"] / 7
                if need_min and need_min > recent * 1.2:
                    dur = int(min(120, max(30, -(-need_min // 30) * 30)))
                    n += bool(self.propose("time_blocks", f"「{x['title']}」 시험 대비 복습 시간 확보",
                                           {"label": f"{x['title']} 복습", "before": dadd(td, x["dday"]),
                                            "count": min(5, max(1, x["dday"] - 1)), "duration_min": dur, "agent": "study"},
                                           [f"시험 D-{x['dday']}, 남은 분량 {x['remaining']:g}{x['unit']}",
                                            f"하루 필요 약 {round(need_min)}분 > 최근 하루 평균 학습 {round(recent)}분"],
                                           f"prop:study:{x['id']}:{wk}", now, "study"))
        for w in A.a_writing(s, now)["items"]:
            if w["days_left"] is not None and 0 < w["days_left"] <= 7 and w["remaining"] > 0:
                n += bool(self.propose("time_blocks", f"「{w['title']}」 연재 원고 집필 시간 확보",
                                       {"label": f"{w['title']} 집필", "before": w["deadline"],
                                        "count": min(3, max(1, w["days_left"] - 1)), "duration_min": 90, "agent": "writing"},
                                       [f"마감 {w['deadline']} (D-{w['days_left']})", f"남은 분량 {int(w['remaining']):,}자"],
                                       f"prop:writing:{w['id']}:{wk}", now, "writing"))
        need = int(rules.get("min_sources") or 2)
        need = max(need, 2)
        for e in evs:
            if e.get("type") == "마감" and not e.get("done") and td <= e.get("date", "") <= dadd(td, 5):
                kws = [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", e["title"])
                       if w not in ("과제", "제출", "마감", "레포트", "보고서")]
                found = {h["id"] for w in kws for h in A.search(s, w, limit=10)}
                if len(found) < need:
                    n += bool(self.propose("research", f"「{e['title']}」 참고 자료 조사",
                                           {"query": " ".join(kws) or e["title"]},
                                           [f"마감 {e['date']} (D-{ddiff(e['date'], td)})",
                                            f"관련 자료 {len(found)}건 (필요 {need}건 이상)"],
                                           f"prop:research:{e['id']}:{e['date']}", now, "research"))
        return n


def preview(st: dict, args: dict) -> list:
    """승인 화면에 보여 줄 실제 실행 내용."""
    t = st["tool"]
    if t == "event.create":
        return [f"일정 추가: {it.get('title')} {it.get('date')} {it.get('start', '')}~{it.get('end', '')}"
                for it in (args.get("items") or [args])]
    if t == "task.create":
        return [f"할 일 추가: {it['title']}" + (f" (기한 {it['due']})" if it.get("due") else "") for it in args.get("items") or []]
    if t == "doc.create":
        return [f"문서 생성: {args.get('title')}"]
    if t == "record.delete":
        return [f"삭제: {args.get('collection')}/{args.get('id')}"]
    if t == "file.modify":
        return [f"문서 수정: {args.get('doc_id')} ({args.get('mode', 'append')})"]
    return []
