"""직원별 분석 함수 — 전부 저장된 실제 데이터만 계산한다 (AI 추론 없음, 결정적)."""
from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date

from .util import dadd, ddiff, num, short_hash, today


def V(s: dict, name: str) -> list:
    return list(s["c"].get(name, {}).values())


def tmin(t):
    try:
        h, m = str(t).split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return None


def hm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def day_events(s: dict, d: str) -> list:
    return sorted([e for e in V(s, "events") if e.get("date") == d],
                  key=lambda e: (e.get("start") or "99:99", e.get("title", "")))


def find_conflicts(evs: list) -> list:
    timed = [(e, tmin(e.get("start")), tmin(e.get("end"))) for e in evs]
    timed = [x for x in timed if x[1] is not None and x[2] is not None and x[2] > x[1]]
    out = []
    for i in range(len(timed)):
        for j in range(i + 1, len(timed)):
            a, b = timed[i], timed[j]
            if a[1] < b[2] and b[1] < a[2]:
                out.append((a[0], b[0]))
    return out


def free_slots(evs: list, start: int = 8 * 60, end: int = 23 * 60, min_len: int = 30) -> list:
    busy = sorted((tmin(e.get("start")), tmin(e.get("end"))) for e in evs
                  if tmin(e.get("start")) is not None and tmin(e.get("end")) is not None)
    slots, cur = [], start
    for a, b in busy:
        if a - cur >= min_len:
            slots.append(f"{hm(cur)}~{hm(min(a, end))}")
        cur = max(cur, b)
    if end - cur >= min_len:
        slots.append(f"{hm(cur)}~{hm(end)}")
    return slots


def scheduled_minutes(evs: list) -> int:
    return sum(max(0, (tmin(e.get("end")) or 0) - (tmin(e.get("start")) or 0))
               for e in evs if tmin(e.get("start")) is not None and tmin(e.get("end")) is not None)


# ------------------------------------------------------------------ 일정
def a_schedule(s: dict, now) -> dict:
    td = today(s, now)
    evs = day_events(s, td)
    conf = []
    for k in range(7):
        d = dadd(td, k)
        for a, b in find_conflicts(day_events(s, d)):
            conf.append({"date": d, "a": a["id"], "b": b["id"],
                         "text": f"{d} 「{a['title']}」 {a['start']}~{a['end']} ↔ 「{b['title']}」 {b['start']}~{b['end']}"})
    overdue = sorted([e for e in V(s, "events") if e.get("type") in ("할일", "마감")
                      and not e.get("done") and e.get("date", "9999") < td], key=lambda e: e["date"])
    deadlines = sorted([e for e in V(s, "events") if e.get("type") == "마감" and not e.get("done")
                        and td <= e.get("date", "") <= dadd(td, 3)], key=lambda e: e["date"])
    lines = [f"오늘({td}) 등록된 일정 {len(evs)}건",
             f"앞으로 7일 안의 시간 충돌 {len(conf)}건",
             f"기한이 지난 미완료 항목 {len(overdue)}건",
             f"3일 안에 마감인 항목 {len(deadlines)}건"]
    return {"today": td, "events": evs, "conflicts": conf, "overdue": overdue, "deadlines": deadlines,
            "free": free_slots(evs), "lines": lines}


# ------------------------------------------------------------------ 학습
def a_study(s: dict, now) -> dict:
    td = today(s, now)
    subs = []
    for sub in V(s, "subjects"):
        logs = sorted([l for l in V(s, "study_logs") if l.get("subject_id") == sub["id"]],
                      key=lambda l: l.get("date", ""))
        total = num(sub.get("total_units"))
        done = num(sub.get("done_units")) + sum(num(l.get("units")) for l in logs if l.get("kind", "진도") == "진도")
        last = logs[-1]["date"] if logs else None
        interval = int(num(sub.get("review_interval_days"), 3) or 3)
        since = ddiff(td, last) if last else None
        last_review = max((l["date"] for l in logs if l.get("kind") in ("복습", "오답")), default=None)
        exam = sub.get("exam_date") or None
        dday = ddiff(exam, td) if exam else None
        remaining = max(total - done, 0)
        subs.append({
            "id": sub["id"], "title": sub["title"], "unit": sub.get("unit_label") or "단위",
            "total": total, "done": done, "remaining": remaining,
            "pct": round(100 * done / total) if total else None,
            "last": last, "since": since, "interval": interval, "last_review": last_review,
            "review_due": bool(done > 0 and (since is None or since >= interval)),
            "no_record": last is None,
            "dday": dday, "need_per_day": round(remaining / dday, 1) if dday and dday > 0 and remaining else None,
            "week_minutes": sum(num(l.get("minutes")) for l in logs if l.get("date", "") >= dadd(td, -6)),
            "recent": [{"date": l["date"], "units": num(l.get("units")), "kind": l.get("kind", "진도")}
                       for l in logs[-5:]],
            "minutes_per_unit": num(sub.get("minutes_per_unit")),
        })
    due = [x for x in subs if x["review_due"]]
    lines = [f"학습 과목 {len(subs)}개"]
    for x in subs:
        p = f"{x['pct']}%" if x["pct"] is not None else "전체 분량 미입력"
        lines.append(f"{x['title']}: 진도 {p}, 마지막 기록 {x['last'] or '없음'}"
                     + (f", 시험 D-{x['dday']}" if x["dday"] is not None and x["dday"] >= 0 else ""))
    if due:
        lines.append("복습 주기가 지난 과목: " + ", ".join(x["title"] for x in due))
    return {"today": td, "subjects": subs, "due": due, "lines": lines}


# ------------------------------------------------------------------ 집필
def a_writing(s: dict, now) -> dict:
    td = today(s, now)
    items = []
    for w in V(s, "writings"):
        if w.get("status") in ("완결", "보류"):
            continue
        logs = [l for l in V(s, "writing_logs") if l.get("writing_id") == w["id"]]
        cur = num(w.get("current_count")) + sum(num(l.get("count")) for l in logs)
        target = num(w.get("target_count"))
        last7 = sum(num(l.get("count")) for l in logs if l.get("date", "") >= dadd(td, -6))
        last = max((l["date"] for l in logs), default=None)
        dl = w.get("deadline") or None
        days_left = ddiff(dl, td) if dl else None
        remaining = max(target - cur, 0)
        need = round(remaining / days_left) if days_left and days_left > 0 and remaining else None
        behind = bool(need and last7 / 7 < need * 0.8) or bool(target and remaining and last7 == 0)
        items.append({"id": w["id"], "title": w["title"], "current": cur, "target": target,
                      "pct": round(100 * cur / target) if target else None, "last7": last7, "last": last,
                      "deadline": dl, "days_left": days_left, "remaining": remaining,
                      "need_per_day": need, "behind": behind, "schedule": w.get("schedule", "")})
    lines = [f"집필 중인 작품 {len(items)}편"]
    for x in items:
        lines.append(f"{x['title']}: {int(x['current']):,}자"
                     + (f" / 목표 {int(x['target']):,}자({x['pct']}%)" if x["target"] else "")
                     + f", 최근 7일 {int(x['last7']):,}자"
                     + (f", 마감까지 하루 {x['need_per_day']:,}자 필요" if x["need_per_day"] else ""))
    return {"today": td, "items": items, "behind": [x for x in items if x["behind"]], "lines": lines}


# ------------------------------------------------------------------ 재무
def a_finance(s: dict, now) -> dict:
    td = today(s, now)
    month = td[:7]
    y, m = int(month[:4]), int(month[5:])
    dim, dom = calendar.monthrange(y, m)[1], int(td[8:])
    spend, income = defaultdict(float), 0.0
    for e in V(s, "expenses"):
        if not e.get("date", "").startswith(month):
            continue
        if e.get("kind") == "수입":
            income += num(e.get("amount"))
        else:
            spend[e.get("category") or "기타"] += num(e.get("amount"))
    total = sum(spend.values())
    budgets = []
    for b in V(s, "budgets"):
        lim, used = num(b.get("limit")), spend.get(b.get("category"), 0.0)
        pct = round(100 * used / lim) if lim else None
        proj = round(used / dom * dim) if dom else used
        budgets.append({"category": b.get("category"), "limit": lim, "used": used, "pct": pct, "projected": proj,
                        "state": "초과" if lim and used > lim else "위험" if lim and (pct >= 80 or proj > lim) else "정상"})
    warn = [b for b in budgets if b["state"] != "정상"]
    lines = [f"{month} 지출 합계 {int(total):,}원, 수입 {int(income):,}원",
             *[f"{c}: {int(v):,}원" for c, v in sorted(spend.items(), key=lambda x: -x[1])],
             *[f"예산 {b['category']}: {int(b['used']):,}/{int(b['limit']):,}원 ({b['pct']}%) — {b['state']}"
               for b in budgets]]
    return {"month": month, "spend": dict(spend), "total": total, "income": income,
            "budgets": budgets, "warn": warn, "lines": lines}


# ------------------------------------------------------------------ 투자
def a_invest(s: dict, now) -> dict:
    rows, cost_sum, value_sum, missing = [], 0.0, 0.0, 0
    for h in V(s, "holdings"):
        q, avg, cur = num(h.get("quantity")), num(h.get("avg_price")), h.get("current_price")
        cost = q * avg
        value = q * num(cur) if cur not in (None, "") else None
        if value is None:
            missing += 1
        rows.append({"id": h["id"], "name": h["name"], "quantity": q, "cost": cost, "value": value,
                     "pnl": (value - cost) if value is not None else None,
                     "pnl_pct": round(100 * (value - cost) / cost, 1) if value is not None and cost else None})
        cost_sum += cost
        value_sum += value if value is not None else cost
    for r in rows:
        r["weight"] = round(100 * (r["value"] if r["value"] is not None else r["cost"]) / value_sum, 1) if value_sum else 0
    conc = [r for r in rows if r["weight"] > 40 and len(rows) > 1]
    lines = [f"보유 종목 {len(rows)}개, 매수 원가 합계 {cost_sum:,.0f}"]
    for r in rows:
        lines.append(f"{r['name']}: 비중 {r['weight']}%"
                     + (f", 평가손익 {r['pnl']:+,.0f} ({r['pnl_pct']:+}%)" if r["pnl"] is not None else ", 현재가 미입력"))
    if conc:
        lines.append("비중 40% 초과 종목: " + ", ".join(r["name"] for r in conc))
    lines.append("※ 실시간 시세 연결 없음 — 사용자가 직접 입력한 현재가로만 계산했습니다.")
    return {"rows": rows, "cost": cost_sum, "value": value_sum, "missing": missing,
            "concentrated": conc, "lines": lines}


# ------------------------------------------------------------------ 프로젝트
def a_project(s: dict, now) -> dict:
    td = today(s, now)
    items = []
    for p in V(s, "projects"):
        if p.get("status") in ("완료", "보류"):
            continue
        ts = [t for t in V(s, "project_tasks") if t.get("project_id") == p["id"]]
        done = [t for t in ts if t.get("done")]
        delayed = sorted([t for t in ts if not t.get("done") and t.get("due") and t["due"] < td],
                         key=lambda t: t["due"])
        ms = sorted([t for t in ts if t.get("milestone") and not t.get("done") and t.get("due")],
                    key=lambda t: t["due"])
        items.append({"id": p["id"], "title": p["title"], "total": len(ts), "done": len(done),
                      "pct": round(100 * len(done) / len(ts)) if ts else 0,
                      "delayed": [{"id": t["id"], "title": t["title"], "due": t["due"]} for t in delayed],
                      "next_milestone": {"title": ms[0]["title"], "due": ms[0]["due"]} if ms else None,
                      "deadline": p.get("deadline") or None,
                      "dday": ddiff(p["deadline"], td) if p.get("deadline") else None})
    lines = [f"진행 중인 프로젝트 {len(items)}개"]
    for x in items:
        lines.append(f"{x['title']}: {x['done']}/{x['total']} 완료({x['pct']}%), 지연 {len(x['delayed'])}건"
                     + (f", 다음 마일스톤 「{x['next_milestone']['title']}」 {x['next_milestone']['due']}"
                        if x["next_milestone"] else ""))
    return {"today": td, "items": items, "delayed_count": sum(len(x["delayed"]) for x in items), "lines": lines}


# ------------------------------------------------------------------ 자료
def _norm(t: str) -> str:
    return re.sub(r"\s+", "", str(t or "")).lower()


def a_research(s: dict, now) -> dict:
    groups = []
    for coll, label in (("documents", "문서"), ("ideas", "아이디어"), ("memories", "기억")):
        by_title, by_body = defaultdict(list), defaultdict(list)
        for r in V(s, coll):
            if coll != "memories" and _norm(r.get("title")):
                by_title[_norm(r.get("title"))].append(r)
            body = _norm(r.get("content"))
            if len(body) >= 20:
                by_body[short_hash(body)].append(r)
        seen = set()
        for kind, grp in (("제목 동일", by_title), ("내용 동일", by_body)):
            for rs in grp.values():
                ids = tuple(sorted(r["id"] for r in rs))
                if len(rs) > 1 and ids not in seen:
                    seen.add(ids)
                    groups.append({"collection": label, "kind": kind, "ids": list(ids),
                                   "titles": [r.get("title") or str(r.get("content"))[:20] for r in rs]})
    lines = [f"문서 {len(V(s, 'documents'))}건, 아이디어 {len(V(s, 'ideas'))}건, 기억 {len(V(s, 'memories'))}건 보관 중",
             f"중복으로 보이는 자료 묶음 {len(groups)}개"]
    lines += [f"[{g['collection']}·{g['kind']}] " + " / ".join(g["titles"]) for g in groups]
    return {"groups": groups, "lines": lines}


def search(s: dict, q: str, limit: int = 30) -> list:
    """키워드·태그 기반 검색 (문서, 기억, 아이디어)."""
    words = [w for w in re.split(r"\s+", q.lower().strip()) if w]
    if not words:
        return []
    hits = []
    for coll in ("documents", "memories", "ideas"):
        for r in V(s, coll):
            text = " ".join(str(r.get(k, "")) for k in ("title", "content", "tags")).lower()
            score = sum(text.count(w) for w in words)
            if score and all(w in text for w in words):
                i = text.find(words[0])
                hits.append({"collection": coll, "id": r["id"], "title": r.get("title") or r.get("content", "")[:30],
                             "snippet": text[max(0, i - 30):i + 90], "score": score})
    return sorted(hits, key=lambda h: -h["score"])[:limit]


# ------------------------------------------------------------------ 생활
def a_life(s: dict, now) -> dict:
    td = today(s, now)
    limit = num(s["settings"].get("daily_hour_limit"), 10)
    days, over = [], []
    for k in range(7):
        d = dadd(td, k)
        evs = day_events(s, d)
        hours = round(scheduled_minutes(evs) / 60, 1)
        longest_free = max([(tmin(x.split("~")[1]) - tmin(x.split("~")[0])) for x in free_slots(evs)] or [0])
        days.append({"date": d, "hours": hours, "count": len(evs), "longest_free_min": longest_free})
        if hours > limit:
            over.append({"date": d, "hours": hours})
    routines = []
    week_start = dadd(td, -6)
    for r in V(s, "routines"):
        logs = [l for l in V(s, "routine_logs") if l.get("routine_id") == r["id"] and l.get("done")
                and week_start <= l.get("date", "") <= td]
        target = int(num(r.get("target_per_week"), 7) or 7)
        n = len({l["date"] for l in logs})
        routines.append({"id": r["id"], "title": r["title"], "done": n, "target": target,
                         "pct": min(100, round(100 * n / target))})
    ll = [l for l in V(s, "life_logs") if week_start <= l.get("date", "") <= td]
    sleep = [num(l.get("sleep_hours")) for l in ll if l.get("sleep_hours") not in (None, "")]
    sleep_avg = round(sum(sleep) / len(sleep), 1) if sleep else None
    low = [r for r in routines if r["pct"] < 50]
    lines = [f"앞으로 7일 중 일정이 하루 {limit:g}시간을 넘는 날 {len(over)}일"
             + (": " + ", ".join(f"{o['date']}({o['hours']}시간)" for o in over) if over else ""),
             *[f"루틴 「{r['title']}」 최근 7일 {r['done']}/{r['target']}회 ({r['pct']}%)" for r in routines],
             f"최근 7일 평균 수면 {sleep_avg}시간" if sleep_avg is not None else "최근 7일 수면 기록 없음"]
    return {"today": td, "days": days, "overload": over, "routines": routines, "low_routines": low,
            "sleep_avg": sleep_avg, "limit": limit, "lines": lines}


# ------------------------------------------------------------------ 창작
def _tokens(r: dict) -> set:
    tags = {t.strip().lower() for t in str(r.get("tags", "")).split(",") if t.strip()}
    words = {w.lower() for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", str(r.get("title", "")))}
    return tags | words


def a_creative(s: dict, now) -> dict:
    td = today(s, now)
    ideas = V(s, "ideas")
    pairs = []
    for i in range(len(ideas)):
        for j in range(i + 1, len(ideas)):
            shared = _tokens(ideas[i]) & _tokens(ideas[j])
            if shared:
                pairs.append({"a": ideas[i]["id"], "b": ideas[j]["id"], "shared": sorted(shared),
                              "text": f"「{ideas[i]['title']}」 ↔ 「{ideas[j]['title']}」 (공통: {', '.join(sorted(shared))})"})
    dl = sorted([{"title": i["title"], "deadline": i["deadline"], "dday": ddiff(i["deadline"], td)}
                 for i in ideas if i.get("deadline") and 0 <= ddiff(i["deadline"], td) <= 30
                 and i.get("status") != "완료"], key=lambda x: x["dday"])
    lines = [f"저장된 아이디어 {len(ideas)}개, 서로 연결되는 쌍 {len(pairs)}개",
             *[p["text"] for p in pairs[:10]],
             *[f"마감 임박: 「{d['title']}」 D-{d['dday']}" for d in dl]]
    return {"pairs": pairs, "deadlines": dl, "count": len(ideas), "lines": lines}


# ------------------------------------------------------------------ 목표·통계
def goals_summary(s: dict) -> dict:
    gs = [g for g in V(s, "goals") if g.get("status", "진행") == "진행"]
    rates = [min(1.0, num(g.get("current")) / num(g.get("target"))) for g in gs if num(g.get("target")) > 0]
    return {"count": len(gs), "rate": round(100 * sum(rates) / len(rates)) if rates else None}


def days_in_month(d: str) -> int:
    dt = date.fromisoformat(d)
    return calendar.monthrange(dt.year, dt.month)[1]


OPINIONS = {"schedule": a_schedule, "study": a_study, "writing": a_writing, "finance": a_finance,
            "invest": a_invest, "project": a_project, "research": a_research, "life": a_life,
            "creative": a_creative}
