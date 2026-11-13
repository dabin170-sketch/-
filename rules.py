"""규칙 기반 업무 엔진: 업무 발견 규칙과 단계별 실행 핸들러.

모든 업무는 (1) 저장된 데이터에서 근거를 찾았을 때만 생성되고 (2) 단계마다 체크포인트를 남기며
(3) 결과를 실제 데이터로 계산한다. 새로운 목표나 프로젝트를 지어내지 않는다.
"""
from __future__ import annotations

from datetime import date

from . import analysis as A
from . import docs as D
from .util import dadd, ddiff, iso, local, parse, short_hash, today, week_key


class NeedAI(Exception):
    """이 단계는 로컬 AI 추론이 필요하다."""

    def __init__(self, system: str, prompt: str):
        self.system, self.prompt = system, prompt


class Ctx:
    def __init__(self, eng, task: dict, now):
        self.eng, self.s, self.t, self.now = eng, eng.s, task, now
        self.cs: dict = task["checkpoint"]["state"]
        self.p: dict = task.get("params") or {}

    def upsert(self, coll: str, rid: str, rec: dict) -> None:
        c = self.s["c"].setdefault(coll, {})
        old = c.get(rid, {})
        c[rid] = {**old, **rec, "id": rid, "created_at": old.get("created_at", iso(self.now)),
                  "updated_at": iso(self.now)}


RULES: dict[str, dict] = {}


def reg(kind, agent, title, steps, discover=None, est=3, kw=(), writes=(), needs_ai=False, param_free=True):
    RULES[kind] = {"kind": kind, "agent": agent, "title": title, "steps": steps, "discover": discover,
                   "est": est, "kw": list(kw), "writes": list(writes), "needs_ai": needs_ai,
                   "param_free": param_free}


def memory_lines(s: dict, agent: str, now) -> list:
    """이 직원이 참고할 기억 (만료되지 않은 것, 최신 10건). 로컬 AI 프롬프트의 자료로 들어간다."""
    td = today(s, now)
    ms = [m for m in A.V(s, "memories") if m.get("agent_id") in (agent, "all")
          and not (m.get("expires_at") and m["expires_at"] < td)]
    ms.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    return [f"[기억] {str(m.get('content', ''))[:200]}" for m in ms[:10]]


def save_report(c: Ctx) -> None:
    """결과를 직원 보고로 저장한다 (작업 ID를 키로 써서 재실행해도 중복되지 않는다)."""
    r = c.cs["result"]
    c.upsert("reports", "rep_" + c.t["id"], {
        "agent": c.t["assigned_agent"], "kind": c.t["kind"], "title": c.t["title"], "task_id": c.t["id"],
        "summary": r["summary"], "lines": r["lines"], "ai": bool(r.get("ai")), "date": today(c.s, c.now)})


def simple(analyze, summarize):
    def collect(c: Ctx):
        c.cs["a"] = analyze(c.s, c.now) if analyze.__code__.co_argcount == 2 else analyze(c)

    def think(c: Ctx):
        c.cs["result"] = summarize(c.cs["a"], c)

    return [("자료 수집", collect, "LOW"), ("분석", think, "LOW"), ("결과 저장", save_report, "LOW")]


# ================================================================ 총괄 관리자
def a_chief(c: Ctx) -> dict:
    s, now = c.s, c.now
    sch, st, pr, fi = A.a_schedule(s, now), A.a_study(s, now), A.a_project(s, now), A.a_finance(s, now)
    wr, lf = A.a_writing(s, now), A.a_life(s, now)
    pending = sum(1 for a in s["approvals"].values() if a["status"] == "PENDING")
    queued = sum(1 for t in s["tasks"].values() if t["status"] in ("NEW", "ANALYZING", "QUEUED", "BLOCKED"))
    return {"today": sch["today"], "events": len(sch["events"]), "conflicts": len(sch["conflicts"]),
            "overdue": len(sch["overdue"]), "deadlines": len(sch["deadlines"]), "review_due": len(st["due"]),
            "delayed": pr["delayed_count"], "budget_warn": len(fi["warn"]), "behind": len(wr["behind"]),
            "overload": len(lf["overload"]), "pending": pending, "queued": queued,
            "goals": A.goals_summary(s)}


def chief_lines(a: dict) -> list:
    L = [f"오늘 일정 {a['events']}건, 7일 내 시간 충돌 {a['conflicts']}건",
         f"기한이 지난 미완료 항목 {a['overdue']}건, 3일 내 마감 {a['deadlines']}건",
         f"복습이 필요한 과목 {a['review_due']}개, 진도가 부족한 작품 {a['behind']}편",
         f"지연된 프로젝트 작업 {a['delayed']}건, 예산 경고 {a['budget_warn']}건",
         f"일정 과부하 예상 {a['overload']}일",
         f"승인 대기 {a['pending']}건, 대기 중인 업무 {a['queued']}건"]
    if a["goals"]["rate"] is not None:
        L.append(f"진행 중인 목표 {a['goals']['count']}개, 평균 달성률 {a['goals']['rate']}%")
    return L


def d_briefing(s, now):
    td = today(s, now)
    return [{"key": f"chief.briefing:{td}", "evidence": [f"{td} 첫 접속 — 하루 업무 현황 점검"], "valid_until": td}]


reg("chief.briefing", "chief", "오늘의 업무 현황 브리핑",
    simple(a_chief, lambda a, c: {"summary": f"{a['today']} 현황: 일정 {a['events']}건 · 충돌 {a['conflicts']}건 · "
                                             f"복습 필요 {a['review_due']}과목 · 지연 {a['delayed']}건 · 승인 대기 {a['pending']}건",
                                  "lines": chief_lines(a)}),
    d_briefing, est=2, kw=["브리핑", "현황", "보고", "요약", "우선순위"])


# ================================================================ 일정 관리자
def d_daily_plan(s, now):
    a = A.a_schedule(s, now)
    if not a["events"] and not a["overdue"]:
        return []
    return [{"key": f"schedule.daily_plan:{a['today']}", "valid_until": a["today"],
             "evidence": [f"오늘 일정 {len(a['events'])}건", f"기한 지난 미완료 항목 {len(a['overdue'])}건"]}]


def s_daily_plan(a, c):
    L = [f"{e.get('start') or '종일'}{'~' + e['end'] if e.get('end') else ''} {e['title']}"
         + (" (완료)" if e.get("done") else "") for e in a["events"]]
    if a["free"]:
        L.append("빈 시간: " + ", ".join(a["free"]))
    L += [f"밀린 항목: {e['title']} (원래 {e['date']})" for e in a["overdue"]]
    L += [f"마감 임박: {e['title']} ({e['date']})" for e in a["deadlines"]]
    return {"summary": f"오늘 일정 {len(a['events'])}건, 빈 시간 {len(a['free'])}구간, 밀린 항목 {len(a['overdue'])}건",
            "lines": L}


reg("schedule.daily_plan", "schedule", "오늘 일정 점검 및 일일 계획", simple(A.a_schedule, s_daily_plan),
    d_daily_plan, est=3, kw=["오늘", "일일", "계획", "일정 점검", "하루"])


def d_conflicts(s, now):
    a = A.a_schedule(s, now)
    if not a["conflicts"]:
        return []
    texts = sorted(x["text"] for x in a["conflicts"])
    return [{"key": "schedule.conflicts:" + short_hash(*texts), "evidence": texts[:5],
             "due": a["conflicts"][0]["date"]}]


reg("schedule.conflicts", "schedule", "일정 시간 충돌 점검",
    simple(A.a_schedule, lambda a, c: {
        "summary": f"앞으로 7일 안에 시간이 겹치는 일정 {len(a['conflicts'])}건" if a["conflicts"]
        else "앞으로 7일 안에 시간이 겹치는 일정 없음",
        "lines": [x["text"] for x in a["conflicts"]], "conflicts": a["conflicts"]}),
    d_conflicts, est=2, kw=["충돌", "겹치", "중복"])


def d_overdue(s, now):
    a = A.a_schedule(s, now)
    if not a["overdue"]:
        return []
    return [{"key": "schedule.overdue:" + short_hash(*sorted(e["id"] for e in a["overdue"])),
             "evidence": [f"「{e['title']}」 기한 {e['date']} — 미완료" for e in a["overdue"][:5]],
             "due": a["today"]}]


def overdue_collect(c: Ctx):
    c.cs["a"] = {"today": today(c.s, c.now),
                 "overdue": [{"id": e["id"], "title": e["title"], "date": e["date"]}
                             for e in A.a_schedule(c.s, c.now)["overdue"]]}


def overdue_propose(c: Ctx):
    a = c.cs["a"]
    c.cs["proposal"] = [f"「{e['title']}」 {e['date']} → {a['today']}(오늘)로 옮기기" for e in a["overdue"]]


def overdue_apply(c: Ctx):
    a, moved = c.cs["a"], []
    evs = c.s["c"].get("events", {})
    for e in a["overdue"]:
        ev = evs.get(e["id"])
        if ev and not ev.get("done") and ev.get("date") == e["date"]:  # 그 사이 바뀌지 않은 항목만
            ev["date"] = a["today"]
            ev["note"] = (ev.get("note") or "") + f" (원래 {e['date']} — 재배치됨)"
            ev["updated_at"] = iso(c.now)
            moved.append(e["title"])
    c.cs["result"] = {"summary": f"밀린 항목 {len(moved)}건을 오늘로 재배치", "lines": moved}
    save_report(c)


reg("schedule.overdue", "schedule", "밀린 일정 재배치 제안",
    [("밀린 항목 수집", overdue_collect, "LOW"), ("재배치안 작성", overdue_propose, "LOW"),
     ("일정 변경 적용", overdue_apply, "MEDIUM")],
    d_overdue, est=3, kw=["밀린", "재배치", "미완료", "옮겨"], writes=["events"])


# ================================================================ 학습 코치
def d_review(s, now):
    a = A.a_study(s, now)
    wk = week_key(a["today"])
    return [{"key": f"study.review:{x['id']}:{x['last'] or 'none'}:{wk}", "title": f"복습 계획 수립: {x['title']}",
             "params": {"subject_id": x["id"]},
             "evidence": [f"「{x['title']}」 마지막 학습 기록 {x['last'] or '없음'}"
                          + (f" ({x['since']}일 전)" if x["since"] is not None else ""),
                          f"설정된 복습 주기 {x['interval']}일"]} for x in a["due"]]


def s_review(a, c):
    subs = [x for x in a["subjects"] if not c.p.get("subject_id") or x["id"] == c.p["subject_id"]]
    subs = [x for x in subs if x["review_due"]] or subs
    L = []
    for x in subs:
        recent = [r for r in x["recent"] if r["kind"] == "진도"]
        scope = (f"{recent[0]['date']}~{recent[-1]['date']}에 공부한 {sum(r['units'] for r in recent):g}{x['unit']}"
                 if recent else f"지금까지 공부한 {x['done']:g}{x['unit']}")
        L.append(f"[{x['title']}] 복습 범위: {scope}")
        for n, gap in enumerate((0, 1, 3, 7), 1):
            L.append(f"  {n}차 복습 {dadd(a['today'], gap)}")
        if x["minutes_per_unit"] and recent:
            L.append(f"  1회 예상 소요 약 {round(sum(r['units'] for r in recent) * x['minutes_per_unit'] * 0.4)}분 "
                     "(첫 학습 시간의 40%로 계산)")
    return {"summary": f"복습 계획 {len(subs)}과목 작성 (1·2·4·8일 간격)", "lines": L}


reg("study.review", "study", "복습 계획 수립", simple(A.a_study, s_review), d_review, est=4,
    kw=["복습", "회독", "오답"], param_free=False)


def d_exam(s, now):
    a = A.a_study(s, now)
    wk = week_key(a["today"])
    return [{"key": f"study.exam_plan:{x['id']}:{wk}", "title": f"시험 대비 계획: {x['title']}",
             "params": {"subject_id": x["id"]}, "due": dadd(a["today"], x["dday"]),
             "evidence": [f"「{x['title']}」 시험 D-{x['dday']}", f"남은 분량 {x['remaining']:g}{x['unit']}"]}
            for x in a["subjects"] if x["dday"] is not None and 0 < x["dday"] <= 90 and x["remaining"] > 0]


def s_exam(a, c):
    subs = [x for x in a["subjects"] if (not c.p.get("subject_id") or x["id"] == c.p["subject_id"])
            and x["dday"] and x["dday"] > 0]
    L = []
    for x in subs:
        L.append(f"[{x['title']}] 시험 D-{x['dday']}, 남은 분량 {x['remaining']:g}{x['unit']} "
                 f"→ 하루 {x['need_per_day'] or 0:g}{x['unit']}")
        if x["minutes_per_unit"] and x["need_per_day"]:
            L.append(f"  하루 약 {round(x['need_per_day'] * x['minutes_per_unit'])}분 필요, "
                     f"최근 7일 실제 학습 {int(x['week_minutes'])}분")
        weeks = max(1, -(-x["dday"] // 7))
        per_week = x["remaining"] / weeks
        for w in range(min(weeks, 6)):
            L.append(f"  {w + 1}주차({dadd(a['today'], 7 * w)}~): 누적 {min(x['total'], x['done'] + per_week * (w + 1)):.0f}{x['unit']}까지")
    L.append("※ 남은 분량을 남은 기간으로 나눈 계산이며 합격을 보장하는 예측이 아닙니다.")
    return {"summary": f"시험 대비 계획 {len(subs)}과목 계산", "lines": L}


reg("study.exam_plan", "study", "시험 대비 계획", simple(A.a_study, s_exam), d_exam, est=4,
    kw=["시험", "대비", "진도", "학습 계획"], param_free=False)


# ================================================================ 집필 편집자
def d_writing(s, now):
    a = A.a_writing(s, now)
    wk = week_key(a["today"])
    return [{"key": f"writing.progress:{x['id']}:{wk}", "title": f"집필 진도 점검: {x['title']}",
             "params": {"writing_id": x["id"]}, "due": x["deadline"],
             "evidence": [f"「{x['title']}」 최근 7일 집필 {int(x['last7']):,}자",
                          f"마감까지 하루 {x['need_per_day']:,}자 필요" if x["need_per_day"]
                          else f"목표까지 {int(x['remaining']):,}자 남음"]} for x in a["behind"]]


def s_writing(a, c):
    items = [x for x in a["items"] if not c.p.get("writing_id") or x["id"] == c.p["writing_id"]]
    L = []
    for x in items:
        L.append(f"[{x['title']}] 현재 {int(x['current']):,}자" + (f" / 목표 {int(x['target']):,}자" if x["target"] else ""))
        L.append(f"  최근 7일 하루 평균 {round(x['last7'] / 7):,}자, 마지막 기록 {x['last'] or '없음'}")
        if x["need_per_day"]:
            L.append(f"  마감({x['deadline']})까지 {x['days_left']}일 — 하루 {x['need_per_day']:,}자 필요")
            gap = x["need_per_day"] - round(x["last7"] / 7)
            L.append(f"  현재 속도와의 차이: 하루 {gap:+,}자")
    return {"summary": f"집필 진도 점검 {len(items)}편, 진도 부족 {sum(1 for x in items if x['behind'])}편", "lines": L}


reg("writing.progress", "writing", "집필 진도 점검", simple(A.a_writing, s_writing), d_writing, est=3,
    kw=["집필", "원고", "연재", "진도", "분량"], param_free=False)


# ================================================================ 문서 제작자
DOC_KW = {"daily_report": ["일일", "오늘"], "weekly_report": ["주간", "이번 주"], "study_plan": ["학습", "공부"],
          "project_report": ["프로젝트"], "writing_report": ["집필", "원고"],
          "ledger_summary": ["가계부", "지출", "예산"], "invest_log": ["투자", "종목"],
          "timetable": ["일정표", "시간표"]}


def _done_dates(s) -> set:
    return {local(s, parse(t["completed_at"])).date().isoformat()
            for t in s["tasks"].values() if t["status"] == "DONE" and t.get("completed_at")}


def d_docs(s, now):
    td = today(s, now)
    dates = _done_dates(s)
    out = []
    prev = sorted(d for d in dates if dadd(td, -7) <= d < td)
    if prev:
        d = prev[-1]
        out.append({"key": f"docs.daily:{d}", "title": f"일일 업무 보고서 작성 ({d})",
                    "params": {"doc_type": "daily_report", "date": d},
                    "evidence": [f"{d}에 완료된 업무가 있으나 보고서가 아직 없음"]})
    last_sunday = dadd(td, -(date.fromisoformat(td).weekday() + 1))
    if any(dadd(last_sunday, -6) <= d <= last_sunday for d in dates):
        out.append({"key": f"docs.weekly:{week_key(last_sunday)}",
                    "title": f"주간 업무 보고서 작성 ({dadd(last_sunday, -6)}~{last_sunday})",
                    "params": {"doc_type": "weekly_report", "end": last_sunday},
                    "evidence": [f"지난주({dadd(last_sunday, -6)}~{last_sunday})에 완료된 업무 기록이 있음"]})
    return out


def doc_build(c: Ctx):
    title, content, used = D.build(c.s, c.now, c.p.get("doc_type", "daily_report"), c.p)
    c.cs["doc"] = {"title": title, "content": content, "used": used}


def doc_verify(c: Ctx):
    d = c.cs["doc"]
    if not d["content"].strip():
        raise ValueError("문서 내용이 비어 있습니다.")
    title2, content2, _ = D.build(c.s, c.now, c.p.get("doc_type", "daily_report"), c.p)
    c.cs["verified"] = content2 == d["content"]  # 같은 데이터로 다시 계산해 일치하는지 확인
    if not c.cs["verified"]:  # 그 사이 데이터가 바뀌었으면 최신 내용으로 교체
        c.cs["doc"].update(title=title2, content=content2)


def doc_save(c: Ctx):
    d = c.cs["doc"]
    doc_id = "doc_" + c.t["id"]
    old = c.s["c"].get("documents", {}).get(doc_id, {})
    c.upsert("documents", doc_id, {
        "title": d["title"], "type": D.DOC_TYPES.get(c.p.get("doc_type"), "일반"), "status": "완료",
        "content": d["content"], "version": old.get("version", 0) + 1, "auto": True, "due_date": ""})
    c.cs["result"] = {"summary": f"문서 「{d['title']}」 생성 완료 — 문서 공장에서 DOCX·MD·HTML로 내려받을 수 있음",
                      "lines": ["사용한 자료: " + ", ".join(d["used"]), f"본문 {len(d['content']):,}자"],
                      "doc_id": doc_id}
    save_report(c)


reg("docs.generate", "docs", "문서 자동 작성",
    [("데이터 수집·본문 생성", doc_build, "LOW"), ("사실·데이터 검증", doc_verify, "LOW"),
     ("문서 저장", doc_save, "MEDIUM")],
    d_docs, est=4, kw=["보고서", "문서", "작성", "정리"], writes=["documents"], param_free=False)


def d_doc_deadline(s, now):
    td = today(s, now)
    return [{"key": f"docs.deadline:{d['id']}:{d['due_date']}", "title": f"마감 임박 문서 점검: {d['title']}",
             "params": {"doc_id": d["id"]}, "due": d["due_date"],
             "evidence": [f"문서 「{d['title']}」 마감 {d['due_date']}, 상태 초안"]}
            for d in A.V(s, "documents") if d.get("status") == "초안" and d.get("due_date")
            and d["due_date"] <= dadd(td, 1)]


def a_doc_deadline(c: Ctx):
    td = today(c.s, c.now)
    docs = [d for d in A.V(c.s, "documents") if d.get("status") == "초안" and d.get("due_date")
            and (d["id"] == c.p.get("doc_id") if c.p.get("doc_id") else d["due_date"] <= dadd(td, 1))]
    return {"today": td, "docs": [{"title": d["title"], "due": d["due_date"], "chars": len(d.get("content") or ""),
                                   "headings": sum(1 for ln in (d.get("content") or "").split("\n") if ln.startswith("#")),
                                   "dday": ddiff(d["due_date"], td)} for d in docs]}


reg("docs.deadline", "docs", "마감 임박 문서 점검",
    simple(a_doc_deadline, lambda a, c: {
        "summary": f"마감이 임박한 초안 문서 {len(a['docs'])}건 점검",
        "lines": [f"「{d['title']}」 마감 {d['due']} ({'D-' + str(d['dday']) if d['dday'] >= 0 else str(-d['dday']) + '일 지남'}), "
                  f"현재 {d['chars']:,}자, 소제목 {d['headings']}개" + (" — 본문이 비어 있음" if d["chars"] == 0 else "")
                  for d in a["docs"]]}),
    d_doc_deadline, est=2, kw=["마감", "초안", "미완성"], param_free=False)


# ================================================================ 투자·재무
def d_invest(s, now):
    n = len(A.V(s, "holdings"))
    return [{"key": f"invest.review:{week_key(today(s, now))}",
             "evidence": [f"보유 종목 {n}개 기록됨 — 주간 점검"]}] if n else []


reg("invest.review", "invest", "포트폴리오 주간 점검",
    simple(A.a_invest, lambda a, c: {
        "summary": f"보유 {len(a['rows'])}종목, 평가 기준 합계 {a['value']:,.0f} (원가 {a['cost']:,.0f})"
                   + (f", 현재가 미입력 {a['missing']}종목" if a["missing"] else ""),
        "lines": a["lines"]}),
    d_invest, est=3, kw=["포트폴리오", "수익률", "종목", "투자", "위험"])


def d_finance(s, now):
    a = A.a_finance(s, now)
    if not a["budgets"] and not a["total"]:
        return []
    return [{"key": f"finance.budget:{a['month']}:{week_key(today(s, now))}:{len(a['warn'])}",
             "evidence": [f"{a['month']} 지출 {int(a['total']):,}원",
                          *[f"예산 {b['category']} {b['pct']}% 사용 — {b['state']}" for b in a["warn"][:4]]]}]


reg("finance.budget", "finance", "월 예산·지출 점검",
    simple(A.a_finance, lambda a, c: {
        "summary": f"{a['month']} 지출 {int(a['total']):,}원, 예산 경고 {len(a['warn'])}건", "lines": a["lines"]}),
    d_finance, est=3, kw=["예산", "지출", "가계부", "소비", "저축"])


# ================================================================ 프로젝트 매니저
def d_project(s, now):
    a = A.a_project(s, now)
    items = [x for x in a["items"] if x["total"]]
    if not items:
        return []
    delayed = sorted(d["id"] for x in items for d in x["delayed"])
    return [{"key": f"project.status:{week_key(a['today'])}:{short_hash(*delayed)}",
             "evidence": [f"진행 중인 프로젝트 {len(items)}개", f"기한이 지난 하위 작업 {len(delayed)}건"]}]


def project_save(c: Ctx):
    for x in c.cs["a"]["items"]:
        p = c.s["c"].get("projects", {}).get(x["id"])
        if p and p.get("progress") != x["pct"]:
            p["progress"] = x["pct"]
            p["updated_at"] = iso(c.now)
    save_report(c)


_proj = simple(A.a_project, lambda a, c: {
    "summary": f"프로젝트 {len(a['items'])}개 점검, 지연 작업 {a['delayed_count']}건",
    "lines": a["lines"] + [f"지연: [{x['title']}] {d['title']} (기한 {d['due']})"
                           for x in a["items"] for d in x["delayed"]]})
reg("project.status", "project", "프로젝트 진행 상태 점검",
    [_proj[0], _proj[1], ("진행률 반영·결과 저장", project_save, "LOW")],
    d_project, est=3, kw=["프로젝트", "진행", "마일스톤", "지연"], writes=["projects"])


# ================================================================ 자료·생활·창작
def d_research(s, now):
    g = A.a_research(s, now)["groups"]
    return [{"key": "research.duplicates:" + short_hash(*sorted("".join(x["ids"]) for x in g)),
             "evidence": [f"[{x['collection']}] {x['kind']}: " + " / ".join(x["titles"]) for x in g[:5]]}] if g else []


reg("research.duplicates", "research", "중복 자료 탐지",
    simple(A.a_research, lambda a, c: {"summary": f"중복으로 보이는 자료 묶음 {len(a['groups'])}개", "lines": a["lines"]}),
    d_research, est=2, kw=["중복", "자료", "정리", "분류", "검색"])


def d_life(s, now):
    a = A.a_life(s, now)
    if not a["overload"] and not a["routines"] and a["sleep_avg"] is None:
        return []
    ev = [f"{o['date']} 일정 {o['hours']}시간 (기준 {a['limit']:g}시간 초과)" for o in a["overload"]]
    ev += [f"루틴 「{r['title']}」 최근 7일 달성률 {r['pct']}%" for r in a["low_routines"]]
    return [{"key": f"life.check:{week_key(a['today'])}:{short_hash(*[o['date'] for o in a['overload']])}",
             "evidence": ev or ["루틴·생활 기록 주간 점검"]}]


def s_life(a, c):
    L = list(a["lines"])
    for o in a["overload"]:
        L.append(f"제안: {o['date']}에 30분 이상 휴식 시간을 확보하거나 일정 하나를 다른 날로 옮기기")
    for r in a["low_routines"]:
        L.append(f"제안: 루틴 「{r['title']}」 목표 횟수({r['target']}회)를 현실에 맞게 조정하거나 시간을 고정하기")
    return {"summary": f"일정 과부하 {len(a['overload'])}일, 달성률 낮은 루틴 {len(a['low_routines'])}개", "lines": L}


reg("life.check", "life", "생활 리듬·일정 과부하 점검", simple(A.a_life, s_life), d_life, est=3,
    kw=["루틴", "수면", "휴식", "과부하", "습관", "생활"])


def d_creative(s, now):
    a = A.a_creative(s, now)
    if not a["pairs"] and not a["deadlines"]:
        return []
    return [{"key": "creative.links:" + short_hash(*sorted(p["a"] + p["b"] for p in a["pairs"]),
                                                    *[d["title"] + d["deadline"] for d in a["deadlines"]]),
             "evidence": [p["text"] for p in a["pairs"][:4]]
                         + [f"「{d['title']}」 마감 D-{d['dday']}" for d in a["deadlines"][:3]]}]


reg("creative.links", "creative", "아이디어 연결 탐색",
    simple(A.a_creative, lambda a, c: {
        "summary": f"아이디어 {a['count']}개 중 연결되는 쌍 {len(a['pairs'])}개, 마감 임박 {len(a['deadlines'])}건",
        "lines": a["lines"]}),
    d_creative, est=2, kw=["아이디어", "연결", "기획", "브레인스토밍", "공모전"])


# ================================================================ 시스템 관리자
def d_health(s, now):
    td = today(s, now)
    return [{"key": f"system.health:{td}", "evidence": [f"{td} 시스템 일일 점검"], "valid_until": td}]


def s_health(a, c):
    L = [f"저장 상태: {a['sync']['state']} ({a['sync']['backend']})" + (f" — {a['sync']['error']}" if a["sync"]["error"] else ""),
         f"마지막 스냅샷: {a['sync']['last_snapshot'] or '없음'}",
         f"최근 24시간 실패 작업 {a['failed_24h']}건, 승인 대기 {a['pending']}건",
         f"오늘 실행 단계 {a['usage']['steps']}/{a['limits']['max_steps_per_day']}, "
         f"AI 호출 {a['usage']['model_calls']}/{a['limits']['ai_daily_limit']}, GitHub 커밋 {a['usage']['commits']}회",
         f"이번 달 서버 가동 추정 {a['month_hours']}시간 / Render 무료 750시간",
         f"상태 파일 크기 약 {a['size_kb']}KB"]
    warn = [w for w in a["warnings"]]
    return {"summary": "시스템 점검 완료 — " + (f"주의 {len(warn)}건" if warn else "이상 없음"), "lines": L + warn}


reg("system.health", "system", "시스템 일일 점검",
    simple(lambda c: c.eng.system_snapshot(c.now), s_health), d_health, est=2,
    kw=["시스템", "점검", "사용량", "백업", "오류", "한도"])


def _old_tasks(s, now) -> list:
    cut = dadd(today(s, now), -30)
    return [t for t in s["tasks"].values() if t["status"] in ("DONE", "STOPPED", "FAILED")
            and t["updated_at"][:10] < cut]


def d_cleanup(s, now):
    old = _old_tasks(s, now)
    return [{"key": f"system.cleanup:{today(s, now)[:7]}",
             "evidence": [f"30일 넘게 지난 종료 작업 {len(old)}건이 쌓여 있음"]}] if len(old) >= 20 else []


def cleanup_collect(c: Ctx):
    c.cs["ids"] = [t["id"] for t in _old_tasks(c.s, c.now)]


def cleanup_propose(c: Ctx):
    c.cs["proposal"] = [f"30일 넘게 지난 종료 작업 {len(c.cs['ids'])}건의 기록을 삭제"]


def cleanup_apply(c: Ctx):
    n = 0
    for tid in c.cs["ids"]:
        t = c.s["tasks"].get(tid)
        if t and t["status"] in ("DONE", "STOPPED", "FAILED") and tid != c.t["id"]:
            del c.s["tasks"][tid]
            c.s["deleted"][tid] = iso(c.now)
            n += 1
    c.s["task_logs"] = [l for l in c.s["task_logs"] if l.get("task_id") in c.s["tasks"] or not l.get("task_id")]
    c.cs["result"] = {"summary": f"오래된 종료 작업 {n}건 삭제", "lines": [f"삭제 {n}건"]}
    save_report(c)


reg("system.cleanup", "system", "오래된 작업 기록 정리",
    [("대상 수집", cleanup_collect, "LOW"), ("삭제안 작성", cleanup_propose, "LOW"),
     ("기록 삭제", cleanup_apply, "HIGH")],
    d_cleanup, est=2, kw=["정리", "삭제", "청소"], writes=["tasks"])


# ================================================================ 사용자 지시
def interpret(c: Ctx):
    text, agent = c.p.get("text", ""), c.t["assigned_agent"]
    best, score, params = None, 0, {}
    for r in RULES.values():
        if r["agent"] != agent or not r["kw"] or r["kind"] == "docs.generate":
            continue
        sc = sum(1 for k in r["kw"] if k in text)
        if sc > score:
            best, score, params = r["kind"], sc, {}
    if agent == "docs" or any(k in text for k in ("보고서", "문서로", "문서 작성", "일정표")):
        for dt, kws in DOC_KW.items():
            sc = sum(1 for k in kws if k in text) + 1
            if sc > max(score, 1):
                best, score, params = "docs.generate", sc, {"doc_type": dt}
    c.cs["mapped"] = {"kind": best, "params": params} if best else None


def execute(c: Ctx):
    m = c.cs.get("mapped")
    text = c.p.get("text", "")
    if m:
        r = RULES[m["kind"]]
        title = r["title"] + (f" — {D.DOC_TYPES[m['params']['doc_type']]}" if m["params"].get("doc_type") else "")
        child = c.eng.add_task(m["kind"], title, r["agent"], f"user:{c.t['id']}", c.now,
                               [f"사용자 지시: {text[:80]}"], "user", params=m["params"],
                               importance=c.t.get("user_importance", 2))
        c.cs["result"] = {"summary": f"지시를 규칙 기반 업무 「{title}」(으)로 해석해 등록",
                          "lines": [f"원래 지시: {text}", "해석 방식: 키워드 규칙 (AI 추론 아님)"],
                          "child": child["id"] if child else None}
        return save_report(c)
    if "ai_text" not in c.cs:
        fn = A.OPINIONS.get(c.t["assigned_agent"])
        lines = fn(c.s, c.now)["lines"] if fn else chief_lines(a_chief(c))
        data = "\n".join(lines + memory_lines(c.s, c.t["assigned_agent"], c.now))[:2500]
        raise NeedAI(c.s["agents"][c.t["assigned_agent"]]["system_prompt"],
                     f"<자료>\n{data}\n</자료>\n\n사용자 지시: {text[:500]}\n\n"
                     "위 자료에 있는 사실만 근거로 지시를 수행하라. 10문장 이내로 답하라.")
    out = str(c.cs["ai_text"]).strip()
    c.cs["result"] = {"summary": "로컬 AI가 작성한 답변: " + out[:60], "ai": True,
                      "lines": [f"원래 지시: {text}", "— 아래는 브라우저 로컬 AI 모델의 생성 결과입니다 —",
                                *out.split("\n")]}
    save_report(c)


reg("user.instruction", "chief", "사용자 지시 처리",
    [("지시 해석", interpret, "LOW"), ("실행", execute, "LOW")], None, est=3, param_free=False)


def rule_actions() -> list:
    """반복 규칙에서 고를 수 있는 업무 목록."""
    acts = [{"value": k, "label": f"{r['title']}"} for k, r in RULES.items()
            if r["param_free"] and k not in ("system.cleanup", "schedule.overdue")]
    acts += [{"value": f"docs.generate:{dt}", "label": f"문서 작성 — {label}"}
             for dt, label in D.DOC_TYPES.items() if dt != "minutes"]
    return acts
