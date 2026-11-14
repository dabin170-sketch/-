"""회의실 · 인생 시뮬레이터 · 비상대책실 · 디지털 분신 · 대시보드."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from . import analysis as A
from .rules import RULES, a_chief, chief_lines, memory_lines
from .util import dadd, ddiff, iso, local, num, parse, today, uid

# 회의에서 각 직원이 내는 의견: (분석 결과 → 의견 문장들, 권고 업무 종류)
def _opinion(aid: str, s: dict, now) -> tuple[list, list, list]:
    """(의견 문장, 사용한 자료, 권고 업무[(kind, 제목, 근거)])"""
    if aid == "schedule":
        a = A.a_schedule(s, now)
        free_min = sum(A.tmin(x.split("~")[1]) - A.tmin(x.split("~")[0]) for x in a["free"])
        L = [f"오늘 일정은 {len(a['events'])}건이고 08~23시 사이 빈 시간은 약 {free_min // 60}시간 {free_min % 60}분입니다.",
             f"앞으로 7일 안에 시간이 겹치는 일정이 {len(a['conflicts'])}건 있습니다."]
        rec = []
        if a["conflicts"]:
            rec.append(("schedule.conflicts", "일정 시간 충돌 점검", [c["text"] for c in a["conflicts"][:3]]))
        if a["overdue"]:
            L.append(f"기한이 지난 미완료 항목이 {len(a['overdue'])}건 있어 재배치가 필요합니다.")
            rec.append(("schedule.overdue", "밀린 일정 재배치 제안", [f"밀린 항목 {len(a['overdue'])}건"]))
        return L, ["일정"], rec
    if aid == "study":
        a = A.a_study(s, now)
        L = [f"{x['title']}: 진도 {x['pct']}%, 마지막 기록 {x['last'] or '없음'}" if x["pct"] is not None
             else f"{x['title']}: 전체 분량 미입력, 마지막 기록 {x['last'] or '없음'}" for x in a["subjects"]]
        L = L or ["등록된 학습 과목이 없습니다."]
        rec = []
        if a["due"]:
            L.append("복습 주기가 지난 과목에 시간을 먼저 배정하는 것이 좋겠습니다: " + ", ".join(x["title"] for x in a["due"]))
            rec.append(("study.review", "복습 계획 수립", [f"복습 필요 {len(a['due'])}과목"]))
        return L, ["학습 과목", "학습 기록"], rec
    if aid == "life":
        a = A.a_life(s, now)
        L = list(a["lines"])
        if a["overload"]:
            L.append("일정 밀도가 높은 날에는 휴식 시간을 먼저 확보해야 합니다.")
        return L, ["일정", "루틴", "수면·활동 기록"], ([("life.check", "생활 리듬·일정 과부하 점검",
                                                 [f"과부하 예상 {len(a['overload'])}일"])] if a["overload"] else [])
    if aid == "system":
        return ["시스템 상태는 설정 화면의 무료 한도·저장 상태에서 확인할 수 있습니다."], ["시스템 사용량"], []
    if aid == "docs":
        n = len(A.V(s, "documents"))
        drafts = [d for d in A.V(s, "documents") if d.get("status") == "초안"]
        return [f"보관 중인 문서 {n}건, 그중 초안 {len(drafts)}건입니다.",
                "결정 사항이 나오면 회의록을 문서로 만들 수 있습니다."], ["문서"], []
    if aid == "chief":
        return [], ["전체 현황"], []
    fn = A.OPINIONS.get(aid)
    kind = {"writing": "writing.progress", "finance": "finance.budget", "invest": "invest.review",
            "project": "project.status", "research": "research.duplicates", "creative": "creative.links"}.get(aid)
    a = fn(s, now)
    flagged = {"writing": a.get("behind"), "finance": a.get("warn"), "invest": a.get("concentrated"),
               "project": a.get("delayed_count"), "research": a.get("groups"), "creative": a.get("pairs")}.get(aid)
    rec = [(kind, RULES[kind]["title"], a["lines"][:2])] if flagged and kind else []
    src = {"writing": ["집필 프로젝트", "집필 기록"], "finance": ["가계부", "월 예산"], "invest": ["보유 종목"],
           "project": ["프로젝트", "하위 작업"], "research": ["문서", "아이디어", "기억"], "creative": ["아이디어"]}[aid]
    return a["lines"][:6], src, rec


def hold_meeting(eng, title: str, mtype: str, participants: list, use_ai: bool, now) -> dict:
    s = eng.s
    if "chief" not in participants:
        participants = ["chief"] + participants
    transcript, sources, recs = [], [], []
    for aid in participants:
        if aid == "chief":
            continue
        lines, src, rec = _opinion(aid, s, now)
        transcript.append({"agent": aid, "mode": "rule", "lines": lines})
        sources += [x for x in src if x not in sources]
        recs += rec
    actions, conclusions = [], []
    mid = uid("m_")
    for kind, t_title, ev in recs:
        task = eng.add_task(kind, t_title, RULES[kind]["agent"], f"meeting:{mid}:{kind}", now,
                            [f"회의 「{title}」 결정"] + ev, f"meeting:{mid}")
        if task:
            actions.append({"task_id": task["id"], "title": t_title, "agent": RULES[kind]["agent"]})
            conclusions.append(f"{s['agents'][RULES[kind]['agent']]['name']}에게 「{t_title}」 업무를 맡긴다.")
    if not conclusions:
        conclusions.append("참석 직원의 자료에서 즉시 조치가 필요한 항목은 발견되지 않았다.")
    needs_approval = [a["title"] for a in actions
                      if any(r in ("MEDIUM", "HIGH") for _, _, r in RULES[s["tasks"][a["task_id"]]["kind"]]["steps"])
                      and not s["agents"][a["agent"]]["permissions"].get("auto_medium")]
    transcript.append({"agent": "chief", "mode": "rule",
                       "lines": [f"참석 직원 {len(participants) - 1}명의 자료를 종합했습니다."] + conclusions})
    m = {"id": mid, "title": title[:200], "type": mtype, "participants": participants, "sources": sources,
         "transcript": transcript, "conclusions": conclusions, "action_items": actions,
         "needs_approval": needs_approval, "mode_label": "규칙 기반 협업 보고서 (생성형 AI 회의 아님)",
         "created_at": iso(now), "updated_at": iso(now)}
    s["c"].setdefault("meetings", {})[mid] = m
    ses = eng.active(now)
    if use_ai and ses and ses.get("ai_ready"):
        for aid in participants:
            if eng.usage(now)["model_calls"] >= s["settings"]["ai_daily_limit"]:
                break
            mine = next((t["lines"] for t in transcript if t["agent"] == aid), [])
            data = "\n".join(mine + memory_lines(s, aid, now))[:2000]
            s["ai_jobs"][jid := uid("job_")] = {
                "id": jid, "kind": "meeting", "meeting_id": mid, "agent_id": aid,
                "system": s["agents"][aid]["system_prompt"],
                "prompt": f"<자료>\n{data}\n</자료>\n\n회의 주제: {title[:200]}\n\n"
                          "위 자료만 근거로 이 주제에 대한 너의 의견을 3문장 이내로 말하라.",
                "status": "pending", "created_at": iso(now), "updated_at": iso(now)}
            eng.usage(now)["model_calls"] += 1
    eng.log(None, "chief", "MEETING", f"회의 「{title}」 진행 — 후속 업무 {len(actions)}건", now)
    eng.store.mark(important=True)
    return m


# ------------------------------------------------------------------ 인생 시뮬레이터
def simulate(p: dict, td: str) -> dict:
    total, done = num(p.get("total_units")), num(p.get("done_units"))
    remaining = max(total - done, 0)
    mpu = num(p.get("minutes_per_unit"), 30)
    review = num(p.get("review_ratio"), 0.3)
    avail = num(p.get("weekly_available_hours"), 20)
    rest_days = int(num(p.get("rest_days_per_week"), 1))
    deadline = p.get("deadline") or dadd(td, 90)
    days = max(ddiff(deadline, td), 1)
    need_hours = remaining * mpu / 60 * (1 + review)
    plans = []
    for name, buffer, desc in (("안정형", 0.0, "마감일까지 고르게 나누어 진행"),
                               ("균형형", 0.15, "기간의 15%를 여유로 남기고 진행"),
                               ("집중형", 0.35, "기간의 35%를 여유로 남기고 빠르게 진행")):
        use_days = max(1, round(days * (1 - buffer)))
        weeks = use_days / 7
        weekly = need_hours / weeks if weeks else need_hours
        study_days = max(1, 7 - rest_days)
        daily = weekly / study_days
        load = weekly / avail if avail else float("inf")
        burden = ("불가능 — 확보한 시간보다 많이 필요" if load > 1 else "높음" if load > 0.85
                  else "보통" if load > 0.6 else "낮음")
        plans.append({"name": name, "description": desc, "finish_date": dadd(td, use_days),
                      "buffer_days": days - use_days, "weekly_hours": round(weekly, 1),
                      "daily_hours": round(daily, 1), "study_days_per_week": study_days,
                      "units_per_week": round(remaining / weeks, 1) if weeks else remaining,
                      "load_pct": round(load * 100) if load != float("inf") else None, "burden": burden})
    return {"title": p.get("title", ""), "remaining_units": remaining, "days_left": days,
            "total_hours_needed": round(need_hours, 1), "weekly_available_hours": avail, "plans": plans,
            "disclaimer": "입력한 수치를 나눗셈한 계산 결과입니다. 실제 결과를 보장하는 예측이 아닙니다."}


# ------------------------------------------------------------------ 비상대책실
def incidents(eng, now) -> list:
    s = eng.s
    td = today(s, now)
    out = []
    sch = A.a_schedule(s, now)
    missed = [e for e in sch["overdue"] if int(num(e.get("importance"), 3)) >= 4]
    if missed:
        out.append({"id": "missed", "level": "HIGH", "title": f"중요 일정 누락 {len(missed)}건",
                    "cause": "중요도 4 이상인 할 일·마감이 기한을 넘겼는데 완료 표시가 없습니다: "
                             + ", ".join(f"{e['title']}({e['date']})" for e in missed[:5]),
                    "options": [{"action": "reschedule", "label": "오늘로 재배치하는 제안 받기 (적용 전 승인 필요)"}]})
    soon = [e for e in sch["deadlines"] if e["date"] <= dadd(td, 1)]
    if soon:
        out.append({"id": "deadline", "level": "MEDIUM", "title": f"마감 임박 {len(soon)}건",
                    "cause": "내일까지 마감인 미완료 항목: " + ", ".join(f"{e['title']}({e['date']})" for e in soon[:5]),
                    "options": [{"action": "daily_plan", "label": "오늘 계획 다시 세우기"}]})
    cut = iso(now - timedelta(hours=24))
    failed = [t for t in s["tasks"].values() if t["status"] == "FAILED" and t["updated_at"] >= cut]
    if len(failed) >= 2:
        out.append({"id": "failures", "level": "HIGH", "title": f"작업 {len(failed)}건 동시 실패",
                    "cause": "최근 24시간 실패: " + " / ".join(f"{t['title']}: {t['error']}" for t in failed[:4]),
                    "options": [{"action": "retry_failed", "label": "실패한 작업을 체크포인트부터 다시 시도"},
                                {"action": "pause", "label": "자동화 일시중지"}]})
    sync = eng.store.status(now)
    if sync["error"] or (sync["dirty_since"] and (now - parse(sync["dirty_since"])).total_seconds() > 600):
        out.append({"id": "storage", "level": "HIGH", "title": "저장 오류",
                    "cause": sync["error"] or "10분 넘게 영구 저장이 완료되지 않았습니다.",
                    "options": [{"action": "sync", "label": "지금 저장 다시 시도"},
                                {"action": "pause", "label": "자동화 일시중지 (추가 변경 방지)"}]})
    if not sync["durable"]:
        out.append({"id": "nostore", "level": "MEDIUM", "title": "영구 저장소 미연결",
                    "cause": "GitHub 저장소가 연결되지 않아 서버가 재시작되면 데이터가 사라질 수 있습니다.",
                    "options": [{"action": "export", "label": "지금 전체 데이터 내려받기"}]})
    ses = eng.active(now)
    if ses and ses.get("limit"):
        out.append({"id": "limit", "level": "MEDIUM", "title": "무료 한도 보호로 자동화 멈춤",
                    "cause": ses["limit"], "options": [{"action": "settings", "label": "설정에서 한도 확인"}]})
    life = A.a_life(s, now)
    if life["overload"]:
        out.append({"id": "overload", "level": "MEDIUM", "title": f"일정 과부하 {len(life['overload'])}일",
                    "cause": ", ".join(f"{o['date']} {o['hours']}시간" for o in life["overload"]),
                    "options": [{"action": "life_check", "label": "생활 코치에게 조정안 받기"}]})
    bad = [i for i in s["inbox"] if i.get("kind") == "integrity_error" and not i.get("ack")]
    if bad:
        out.append({"id": "integrity", "level": "HIGH", "title": "데이터 손상 의심",
                    "cause": bad[-1].get("message", ""),
                    "options": [{"action": "snapshots", "label": "스냅샷 목록에서 복구 (비밀번호 확인 필요)"}]})
    conflicts = len(sch["conflicts"])
    if conflicts >= 3:
        out.append({"id": "conflicts", "level": "MEDIUM", "title": f"일정 충돌 {conflicts}건",
                    "cause": "; ".join(c["text"] for c in sch["conflicts"][:3]),
                    "options": [{"action": "conflicts", "label": "충돌 점검 업무 실행"}]})
    return out


def emergency_action(eng, action: str, now) -> str:
    s = eng.s
    td = today(s, now)
    key = f"emergency:{action}:{td}:{local(s, now).hour}"
    mk = lambda kind: eng.add_task(kind, RULES[kind]["title"], RULES[kind]["agent"], key, now,
                                   ["비상대책실에서 사용자가 요청"], "user", importance=3)
    if action == "retry_failed":
        n = sum(1 for t in list(s["tasks"].values()) if t["status"] == "FAILED" and eng.task_action(t["id"], "retry", now))
        return f"실패한 작업 {n}건을 다시 대기열에 넣었습니다."
    if action == "pause":
        s["settings"].update(paused=True, updated_at=iso(now))
        eng.audit("automation.pause", "비상대책실", now)
        eng.store.mark(important=True)
        return "자동화를 일시중지했습니다. 설정에서 다시 켤 수 있습니다."
    kind = {"reschedule": "schedule.overdue", "daily_plan": "schedule.daily_plan", "life_check": "life.check",
            "conflicts": "schedule.conflicts"}.get(action)
    if kind:
        return "업무를 등록했습니다." if mk(kind) else "같은 업무가 이미 등록되어 있습니다."
    return ""


# ------------------------------------------------------------------ 디지털 분신
def twin(s: dict) -> dict:
    aps = [a for a in s["approvals"].values() if a["status"] != "PENDING"]
    ok = sum(1 for a in aps if a["status"] == "APPROVED")
    hours = Counter(local(s, parse(x["started_at"])).hour for x in s["sessions"])
    srcs = Counter(t["assigned_agent"] for t in s["tasks"].values() if t["source"] == "user")
    exports = Counter(s["meta"].get("exports", {}))
    rated = sorted([(a["name"], round(a["stats"]["rating_sum"] / a["stats"]["rating_n"], 1), a["stats"]["rating_n"])
                    for a in s["agents"].values() if a["stats"]["rating_n"]], key=lambda x: -x[1])
    goals = sorted([g for g in A.V(s, "goals") if g.get("status", "진행") == "진행"],
                   key=lambda g: -int(g.get("importance") or 3))
    return {
        "approval": {"total": len(aps), "approved": ok,
                     "note": f"승인 요청 {len(aps)}건 중 {ok}건 승인" if aps else "승인 기록 없음"},
        "active_hours": [f"{h}시" for h, _ in hours.most_common(3)],
        "frequent_agents": [{"agent": s["agents"][a]["name"], "count": n} for a, n in srcs.most_common(5) if a in s["agents"]],
        "preferred_formats": [f"{k} {v}회" for k, v in exports.most_common(3)],
        "ratings": [{"agent": n, "avg": r, "count": c} for n, r, c in rated],
        "goal_priority": [{"title": g["title"], "importance": int(g.get("importance") or 3)} for g in goals[:5]],
        "applied": "직원 평가가 2회 이상 쌓이면 평균 4점 이상인 직원의 업무에 우선순위 +5점, 2점 이하에는 -3점을 "
                   "적용합니다. 적용 여부는 각 업무의 우선순위 근거에 표시됩니다. 분신은 결정을 대신 내리지 않습니다.",
    }


# ------------------------------------------------------------------ 대시보드
def dashboard(eng, now) -> dict:
    s = eng.s
    td = today(s, now)

    class _T:  # a_chief가 요구하는 최소 컨텍스트
        pass
    c = _T()
    c.s, c.now = s, now
    a = a_chief(c)
    done_today = sum(1 for t in s["tasks"].values() if t["status"] == "DONE" and t.get("completed_at")
                     and local(s, parse(t["completed_at"])).date().isoformat() == td)
    done_week = sum(1 for t in s["tasks"].values() if t["status"] == "DONE" and t.get("completed_at")
                    and local(s, parse(t["completed_at"])).date().isoformat() >= dadd(td, -6))
    alerts = []
    if a["conflicts"]:
        alerts.append(f"시간이 겹치는 일정 {a['conflicts']}건")
    if a["overdue"]:
        alerts.append(f"기한이 지난 미완료 항목 {a['overdue']}건")
    if a["pending"]:
        alerts.append(f"승인을 기다리는 업무 {a['pending']}건")
    if a["budget_warn"]:
        alerts.append(f"예산 경고 {a['budget_warn']}건")
    return {"today": td, "report": chief_lines(a), "alerts": alerts, "done_today": done_today,
            "done_week": done_week, "goals": a["goals"], "events": A.day_events(s, td),
            "failed": sum(1 for t in s["tasks"].values() if t["status"] == "FAILED")}


def metrics(p: dict) -> dict:
    """에이전트 실제 성능 지표 (게임 경험치와 무관)."""
    total = p["units_done"] + p["units_failed"]
    pct = lambda a, b: round(100 * a / b) if b else None
    return {"units": total, "success_rate": pct(p["units_done"], total), "verify_pass_rate": pct(p["first_pass"], p["verified"]),
            "error_rate": pct(p["with_error"], total), "avg_retries": round(p["retries"] / total, 2) if total else None,
            "correction_rate": pct(p["corrected"], p["produced"]), "produced": p["produced"],
            "avg_ms": round(p["time_ms"] / p["time_n"]) if p["time_n"] else None,
            "satisfaction": round(p["rating_sum"] / p["rating_n"], 1) if p["rating_n"] else None, "ratings": p["rating_n"]}
