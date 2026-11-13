"""도구 레지스트리.

모든 도구는 실제 서버 함수다. 실행 엔진이 도구를 호출하고, 결과를 관찰·검증한다.
로컬 AI 모델은 도구를 직접 호출하지 않는다 (작은 모델의 도구 호출은 신뢰할 수 없으므로 지원하는 것처럼 표시하지 않는다).
모델은 'ai.generate' 단계에서 문장 생성만 맡는다.

도구 정의: name, label, category, risk(LOW/MEDIUM/HIGH), writes(쓰기 대상 컬렉션), fn(ctx, args) -> dict,
           verify(ctx, args, out) -> [ {name, passed, detail} ]
"""
from __future__ import annotations

import hashlib
import io
import re
from collections import Counter
from datetime import date

from . import analysis as A
from . import docs as D
from . import memory as M
from .util import dadd, ddiff, iso, short_hash, today

HTTP_CLIENT = None  # 테스트에서 httpx.Client(transport=MockTransport)로 바꿔 끼운다


class ToolError(Exception):
    """원인(cause)이 분류된 도구 실패. 수정(REVISE) 단계가 원인별로 대응한다."""

    def __init__(self, cause: str, msg: str, data: dict | None = None):
        super().__init__(msg)
        self.cause, self.msg, self.data = cause, msg, data or {}


class NeedAIJob(Exception):
    def __init__(self, task_type: str, system: str, prompt: str):
        self.task_type, self.system, self.prompt = task_type, system, prompt


class TCtx:
    def __init__(self, eng, wf: dict, step: dict, now):
        self.eng, self.s, self.wf, self.step, self.now = eng, eng.s, wf, step, now
        self.agent = step["agent"]

    def rid(self, prefix: str, *parts) -> str:
        """워크플로·단계 기준의 결정적 ID — 재시도해도 같은 레코드를 다시 만들지 않는다."""
        return prefix + short_hash(self.wf["id"], self.step["id"], *parts)


def C(name, passed, detail=""):
    return {"name": name, "passed": bool(passed), "detail": detail}


TOOLS: dict[str, dict] = {}


def tool(name, label, category, risk="LOW", writes=(), network=False):
    def deco(fn):
        TOOLS[name] = {"name": name, "label": label, "category": category, "risk": risk, "writes": list(writes),
                       "network": network, "fn": fn, "verify": None, "doc": (fn.__doc__ or "").strip()}
        return fn
    return deco


def verifier(name):
    def deco(fn):
        TOOLS[name]["verify"] = fn
        return fn
    return deco


def _norm(t) -> str:
    return re.sub(r"\s+", "", str(t or "")).lower()


def _stamp(ctx: TCtx, rec: dict) -> dict:
    rec.update(by_agent=ctx.agent, by_wf=ctx.wf["id"])
    return rec


def _put(ctx: TCtx, coll: str, rid: str, rec: dict) -> dict:
    c = ctx.s["c"].setdefault(coll, {})
    old = c.get(rid)
    now = iso(ctx.now)
    rec = {**(old or {}), **rec, "id": rid, "created_at": (old or {}).get("created_at", now), "updated_at": now,
           "version": int((old or {}).get("version", 0)) + 1}
    c[rid] = _stamp(ctx, rec)
    ctx.eng.history(coll, rid, "update" if old else "create", ctx.now)
    if not old:
        ctx.eng.perf(ctx.agent)["produced"] += 1
    return c[rid]


# ================================================================== 조회
@tool("project.search", "프로젝트 검색", "조회")
def t_project_search(ctx: TCtx, args):
    """프로젝트 제목·설명·목표에서 찾고 하위 작업 현황을 함께 돌려준다."""
    q = _norm(args.get("query"))
    pid = args.get("project_id")
    pr = {x["id"]: x for x in A.a_project(ctx.s, ctx.now)["items"]}
    rows = []
    for p in A.V(ctx.s, "projects"):
        text = _norm(" ".join(str(p.get(k, "")) for k in ("title", "description", "goal")))
        hit = (pid and p["id"] == pid) or (not pid and (not q or _norm(p["title"]) in q or q in text))
        if not hit:
            continue
        ts = sorted([t for t in A.V(ctx.s, "project_tasks") if t.get("project_id") == p["id"]],
                    key=lambda t: str(t.get("due") or "9"))
        info = pr.get(p["id"], {})
        rows.append({"id": p["id"], "title": p["title"], "deadline": p.get("deadline") or None,
                     "status": p.get("status", "진행"), "goal": p.get("goal", ""),
                     "total": len(ts), "done": sum(1 for t in ts if t.get("done")),
                     "delayed": info.get("delayed", []),
                     "tasks": [{"id": t["id"], "title": t["title"], "due": t.get("due") or None,
                                "done": bool(t.get("done")), "milestone": bool(t.get("milestone"))} for t in ts]})
    return {"projects": rows, "count": len(rows)}


@verifier("project.search")
def v_project_search(ctx, args, out):
    return [C("프로젝트 1개 이상 발견", out["count"] >= 1,
              f"{out['count']}개" if out["count"] else f"'{args.get('query') or args.get('project_id')}'와 맞는 프로젝트 없음")]


@tool("event.query", "일정 조회", "조회")
def t_event_query(ctx: TCtx, args):
    """기간·키워드로 일정을 조회한다."""
    td = today(ctx.s, ctx.now)
    d1 = args.get("start") or args.get("date") or td
    d2 = args.get("end") or args.get("date") or dadd(d1, int(args.get("days", 7)) - 1)
    kw = _norm(args.get("keyword"))
    evs = [e for e in A.V(ctx.s, "events") if d1 <= e.get("date", "") <= d2 and (not kw or kw in _norm(e.get("title")))]
    evs.sort(key=lambda e: (e["date"], e.get("start") or "99"))
    return {"start": d1, "end": d2, "events": [{k: e.get(k) for k in ("id", "title", "date", "start", "end", "type",
                                                                     "importance", "done")} for e in evs]}


@verifier("event.query")
def v_event_query(ctx, args, out):
    return [C("조회 기간이 올바름", out["start"] <= out["end"], f"{out['start']}~{out['end']}, {len(out['events'])}건")]


def _alternatives(s, d: str, dur: int, avoid: list | None = None, days: int = 4, limit: int = 5,
                  before: str | None = None, near: str | None = None) -> list:
    """d부터 여러 날에 걸쳐 dur분이 들어가는 빈 시간 후보(30분 단위, 08~22시). near가 있으면 원래 시간에 가까운 순."""
    if near:
        cand = _alternatives(s, d, dur, avoid, days, 40, before)
        cand.sort(key=lambda c: (c["date"], abs(A.tmin(c["start"]) - A.tmin(near))))
        return cand[:limit]
    out = []
    for k in range(days):
        day = dadd(d, k)
        if before and day >= before:
            break
        evs = [e for e in A.day_events(s, day) if e.get("id") not in (avoid or [])]
        busy = sorted((A.tmin(e.get("start")), A.tmin(e.get("end"))) for e in evs
                      if A.tmin(e.get("start")) is not None and A.tmin(e.get("end")) is not None)
        t = 8 * 60
        while t + dur <= 22 * 60 and len(out) < limit:
            if all(not (t < b and a < t + dur) for a, b in busy):
                out.append({"date": day, "start": A.hm(t), "end": A.hm(t + dur)})
                t += max(dur, 60)
            else:
                t += 30
        if len(out) >= limit:
            break
    return out


@tool("schedule.check", "일정 충돌 검사", "분석")
def t_schedule_check(ctx: TCtx, args):
    """새 일정이 기존 일정과 겹치는지 계산하고, 겹치면 대안 시간을 찾는다."""
    d, start, dur = args["date"], args["start"], int(args.get("duration_min") or 60)
    a, b = A.tmin(start), A.tmin(start) + dur
    conflicts = [e for e in A.day_events(ctx.s, d) if A.tmin(e.get("start")) is not None
                 and A.tmin(e.get("end")) is not None and a < A.tmin(e["end"]) and A.tmin(e["start"]) < b]
    return {"date": d, "start": start, "end": A.hm(b), "duration_min": dur,
            "conflicts": [{"id": e["id"], "title": e["title"], "start": e["start"], "end": e["end"]} for e in conflicts],
            "alternatives": _alternatives(ctx.s, d, dur, near=start) if conflicts else []}


@verifier("schedule.check")
def v_schedule_check(ctx, args, out):
    ok = not out["conflicts"] or bool(out["alternatives"])
    return [C("충돌 시 대안 확보", ok, f"충돌 {len(out['conflicts'])}건, 대안 {len(out['alternatives'])}개")]


@tool("schedule.conflicts", "기존 일정 충돌 찾기", "분석")
def t_schedule_conflicts(ctx: TCtx, args):
    """기간 안의 겹치는 일정 쌍을 찾고, 옮길 일정(중요도가 낮거나 나중에 등록된 쪽)과 대안 시간을 계산한다."""
    td = today(ctx.s, ctx.now)
    out = []
    for k in range(int(args.get("days", 7))):
        d = dadd(td, k)
        for x, y in A.find_conflicts(A.day_events(ctx.s, d)):
            mv = min((x, y), key=lambda e: (int(e.get("importance") or 3), _neg_ts(e.get("created_at", ""))))
            keep = y if mv is x else x
            dur = A.tmin(mv["end"]) - A.tmin(mv["start"])
            out.append({"date": d, "keep": {"id": keep["id"], "title": keep["title"], "start": keep["start"], "end": keep["end"]},
                        "move": {"id": mv["id"], "title": mv["title"], "start": mv["start"], "end": mv["end"]},
                        "alternatives": _alternatives(ctx.s, d, dur, avoid=[mv["id"]], limit=3, near=mv["start"])})
    return {"conflicts": out, "count": len(out)}


def _neg_ts(s: str) -> str:
    return "".join(chr(0x10FFFF - ord(c)) for c in s)


@verifier("schedule.conflicts")
def v_schedule_conflicts(ctx, args, out):
    return [C("충돌마다 대안 시간 제시", all(c["alternatives"] for c in out["conflicts"]),
              f"충돌 {out['count']}건")]


# ================================================================== 생성·수정
@tool("event.create", "일정 생성", "생성", risk="MEDIUM", writes=["events"])
def t_event_create(ctx: TCtx, args):
    """일정을 저장한다. 시간이 겹치면 저장하지 않고 대안과 함께 되돌려 보낸다(사용자가 고르면 다시 실행)."""
    items = args.get("items") or [args]
    created, existing = [], []
    for i, it in enumerate(items):
        title, d = str(it["title"]).strip(), it["date"]
        start, end = it.get("start") or "", it.get("end") or ""
        dup = [e for e in A.V(ctx.s, "events") if _norm(e.get("title")) == _norm(title) and e.get("date") == d
               and (e.get("start") or "") == start]
        rid = ctx.rid("ev_", i)
        if dup and dup[0]["id"] != rid:
            existing.append(dup[0]["id"])
            continue
        if start and end and not it.get("allow_conflict"):
            clash = [e for e in A.day_events(ctx.s, d) if e["id"] != rid and A.tmin(e.get("start")) is not None
                     and A.tmin(e.get("end")) is not None
                     and A.tmin(start) < A.tmin(e["end"]) and A.tmin(e["start"]) < A.tmin(end)]
            if clash:
                dur = A.tmin(end) - A.tmin(start)
                raise ToolError("conflict", f"「{title}」 {d} {start}~{end}이(가) 「{clash[0]['title']}」"
                                            f" {clash[0]['start']}~{clash[0]['end']}과 겹침",
                                {"item": i, "with": [c["title"] for c in clash],
                                 "alternatives": _alternatives(ctx.s, d, dur, before=it.get("before"), near=start)})
        if args.get("_dry"):
            continue
        rec = {"title": title, "date": d, "start": start, "end": end, "type": it.get("type", "일정"),
               "importance": int(it.get("importance") or 3), "done": False, "note": it.get("note", "")}
        created.append(_put(ctx, "events", rid, rec)["id"])
    return {"created": created, "existing": existing, "items": items}


TOOLS["event.create"]["dry"] = True  # 승인 전에 충돌만 미리 검사할 수 있음 (저장 안 함)


@verifier("event.create")
def v_event_create(ctx, args, out):
    evs = ctx.s["c"].get("events", {})
    saved = all(i in evs for i in out["created"])
    dups = [i for i in out["created"] if sum(1 for e in evs.values() if _norm(e["title"]) == _norm(evs[i]["title"])
                                             and e["date"] == evs[i]["date"] and e.get("start") == evs[i].get("start")) > 1]
    clashes = []
    for i in out["created"]:
        e = evs.get(i)
        if e and e.get("start") and e.get("end"):
            for o in A.day_events(ctx.s, e["date"]):
                if o["id"] != i and A.tmin(o.get("start")) is not None and A.tmin(o.get("end")) is not None and \
                        A.tmin(e["start"]) < A.tmin(o["end"]) and A.tmin(o["start"]) < A.tmin(e["end"]):
                    clashes.append(f"{e['title']}↔{o['title']}")
    allowed = all(it.get("allow_conflict") for it in out["items"])
    return [C("DB 저장 확인", saved, f"새로 저장 {len(out['created'])}건, 이미 있던 일정 {len(out['existing'])}건"),
            C("중복 일정 없음", not dups, ", ".join(dups)),
            C("시간 충돌 없음", not clashes or allowed, ", ".join(clashes) + (" (사용자가 겹침 허용)" if clashes and allowed else ""))]


@tool("event.update", "일정 수정", "수정", risk="MEDIUM", writes=["events"])
def t_event_update(ctx: TCtx, args):
    """일정의 날짜·시간을 바꾼다. 충돌 해결에서는 사용자가 고른 대안 시간만 적용한다."""
    conflicts = args.get("conflicts") or []
    choices = args.get("choices") or {}
    moved, kept = [], []
    for c in conflicts:
        mid = c["move"]["id"]
        if mid in choices:
            ch = choices[mid]
            if args.get("_dry"):
                continue
            if ch == "keep":
                kept.append(c["move"]["title"])
                continue
            e = ctx.s["c"].get("events", {}).get(mid)
            if not e:
                raise ToolError("not_found", f"일정 {mid}이(가) 삭제되어 옮길 수 없음")
            old = f"{e['date']} {e['start']}~{e['end']}"
            e.update(date=ch["date"], start=ch["start"], end=ch["end"], updated_at=iso(ctx.now),
                     note=(e.get("note") or "") + f" (충돌 해결로 {old}에서 이동)", version=int(e.get("version", 1)) + 1)
            _stamp(ctx, e)
            ctx.eng.history("events", mid, "update", ctx.now)
            moved.append({"id": mid, "title": e["title"], "from": old, "to": f"{ch['date']} {ch['start']}~{ch['end']}"})
            continue
        opts = [{"label": f"「{c['move']['title']}」을(를) {a['date']} {a['start']}~{a['end']}로 옮기기",
                 "value": {"id": mid, **a}} for a in c["alternatives"]]
        opts.append({"label": f"그대로 두기 (「{c['keep']['title']}」과 겹친 채 유지)", "value": {"id": mid, "keep": True}})
        raise ToolError("conflict", f"{c['date']} 「{c['keep']['title']}」↔「{c['move']['title']}」 충돌 — 대안 선택 필요",
                        {"options": opts, "move_id": mid})
    return {"moved": moved, "kept": kept}


TOOLS["event.update"]["dry"] = True


@verifier("event.update")
def v_event_update(ctx, args, out):
    evs = ctx.s["c"].get("events", {})
    ok = all(evs.get(m["id"]) and f"{evs[m['id']]['date']} {evs[m['id']]['start']}~{evs[m['id']]['end']}" == m["to"]
             for m in out["moved"])
    return [C("변경 내용 저장 확인", ok, f"이동 {len(out['moved'])}건, 유지 {len(out['kept'])}건")]


@tool("task.create", "할 일 생성", "생성", risk="MEDIUM", writes=["project_tasks"])
def t_task_create(ctx: TCtx, args):
    """프로젝트에 할 일을 추가한다. 같은 제목의 할 일이 이미 있으면 만들지 않는다."""
    pid = args["project_id"]
    if pid not in ctx.s["c"].get("projects", {}):
        raise ToolError("not_found", "프로젝트가 존재하지 않음")
    have = {_norm(t["title"]): t["id"] for t in A.V(ctx.s, "project_tasks") if t.get("project_id") == pid}
    created, skipped = [], []
    for i, it in enumerate(args.get("items") or []):
        rid = ctx.rid("pt_", i)
        if _norm(it["title"]) in have and have[_norm(it["title"])] != rid:
            skipped.append(it["title"])
            continue
        rec = _put(ctx, "project_tasks", rid, {"project_id": pid, "title": it["title"][:300], "due": it.get("due") or "",
                                               "done": False, "milestone": bool(it.get("milestone"))})
        have[_norm(it["title"])] = rid
        created.append(rec["id"])
    return {"project_id": pid, "created": created, "skipped": skipped, "requested": len(args.get("items") or [])}


@verifier("task.create")
def v_task_create(ctx, args, out):
    pts = ctx.s["c"].get("project_tasks", {})
    titles = [_norm(t["title"]) for t in pts.values() if t.get("project_id") == out["project_id"]]
    dup = [t for t, n in Counter(titles).items() if n > 1]
    return [C("DB 저장 확인", all(i in pts for i in out["created"]), f"생성 {len(out['created'])}건"),
            C("중복 할 일 없음", not dup, ", ".join(dup)),
            C("요청 항목 모두 처리", len(out["created"]) + len(out["skipped"]) == out["requested"],
              f"요청 {out['requested']}, 생성 {len(out['created'])}, 이미 있음 {len(out['skipped'])}")]


@tool("record.find", "삭제 대상 찾기", "조회")
def t_record_find(ctx: TCtx, args):
    """제목이 정확히 같은 기록을 일정·문서·할 일·아이디어에서 찾는다. 여러 개면 모호하므로 실패한다."""
    q = _norm(args.get("target"))
    hits = [(c, r) for c in ("events", "documents", "project_tasks", "ideas", "memories")
            for r in ctx.s["c"].get(c, {}).values() if q and _norm(r.get("title") or r.get("content")) == q]
    if not hits:
        raise ToolError("not_found", f"제목이 '{args.get('target')}'인 기록이 없음")
    if len(hits) > 1:
        raise ToolError("bad_input", f"'{args.get('target')}' 기록이 {len(hits)}개라 어느 것을 지울지 모호함")
    c, r = hits[0]
    return {"collection": c, "id": r["id"], "title": r.get("title") or str(r.get("content"))[:40]}


@verifier("record.find")
def v_record_find(ctx, args, out):
    return [C("대상이 실제로 존재", out["id"] in ctx.s["c"].get(out["collection"], {}), f"{out['collection']}/{out['title']}")]


@tool("record.delete", "기록 삭제", "수정", risk="HIGH", writes=[])
def t_record_delete(ctx: TCtx, args):
    """사용자 데이터를 삭제한다 (위험도 HIGH — 항상 사용자 승인 후에만 실행)."""
    coll, rid = args["collection"], args["id"]
    c = ctx.s["c"].get(coll, {})
    if rid not in c:
        raise ToolError("not_found", "삭제할 기록이 없음")
    title = c[rid].get("title") or rid
    del c[rid]
    ctx.s["deleted"][rid] = iso(ctx.now)
    ctx.eng.history(coll, rid, "delete", ctx.now)
    ctx.eng.audit("data.delete", f"{coll}/{rid} (워크플로 {ctx.wf['id']}, 승인됨)", ctx.now)
    return {"collection": coll, "id": rid, "title": title}


@verifier("record.delete")
def v_record_delete(ctx, args, out):
    return [C("삭제 확인", out["id"] not in ctx.s["c"].get(out["collection"], {}))]


# ================================================================== 문서·파일
@tool("doc.compose", "문서 본문 작성", "분석")
def t_doc_compose(ctx: TCtx, args):
    """저장된 실제 데이터로 문서 본문(마크다운)을 만든다."""
    title, content, used = D.build(ctx.s, ctx.now, args["doc_type"], args)
    return {"title": title, "content": content, "used": used, "type": D.DOC_TYPES[args["doc_type"]],
            "required": required_facts(content, title)}


def required_facts(content: str, title: str) -> list:
    """문서에 반드시 들어가야 할 내용: 제목 + 본문 표의 첫 열 값 일부(실제 데이터 항목)."""
    req = [title]
    for ln in content.split("\n"):
        if ln.startswith("|") and not re.match(r"^\|[-|\s]+\|$", ln):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            for cell in cells[:3]:
                if cell and not re.fullmatch(r"[-\d.,:~%원자\s+]+", cell) and cell not in req and len(cell) <= 60:
                    req.append(cell)
        if len(req) >= 8:
            break
    return req


@verifier("doc.compose")
def v_doc_compose(ctx, args, out):
    return [C("본문 있음", len(out["content"].strip()) > 0, f"{len(out['content'])}자")]


@tool("doc.create", "문서 생성·파일 저장", "생성", risk="MEDIUM", writes=["documents"])
def t_doc_create(ctx: TCtx, args):
    """문서를 저장하고 실제 파일(DOCX 등)을 만들어 저장소 files/ 아래에 기록한다."""
    title, content, fmt = args["title"], args["content"], args.get("fmt", "docx")
    if args.get("fix"):  # 검증에서 빠졌다고 나온 내용을 보충
        content += "\n\n## 보충\n" + "\n".join(f"- {x}" for x in args["fix"])
    doc_id = ctx.rid("doc_")
    doc = _put(ctx, "documents", doc_id, {"title": title, "type": args.get("type", "AI 초안"), "status": "완료",
                                          "content": content, "due_date": "", "project_id": args.get("project_id", "")})
    data, mime, ext = D.export(title, content, fmt)
    fid = ctx.rid("file_", doc["version"])
    path = f"files/{fid}.{ext}"
    try:
        sha = ctx.eng.store.write_file(path, data)
    except Exception as e:
        raise ToolError("transient", f"파일 저장 실패: {e}")
    ctx.s["files"][fid] = {"id": fid, "doc_id": doc_id, "name": re.sub(r'[\\/:*?"<>|]+', "_", title)[:80] + "." + ext,
                           "path": path, "fmt": ext, "mime": mime, "size": len(data), "sha256": sha,
                           "durable": ctx.eng.store.b.durable, "created_at": iso(ctx.now), "updated_at": iso(ctx.now),
                           "by_agent": ctx.agent, "by_wf": ctx.wf["id"]}
    doc["file_id"] = fid
    return {"doc_id": doc_id, "file_id": fid, "url": f"/api/files/{fid}", "size": len(data), "sha256": sha,
            "fmt": ext, "required": args.get("required") or [title], "title": title}


def file_text(data: bytes, fmt: str) -> str:
    if fmt == "docx":
        from docx import Document
        d = Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        parts += [c.text for t in d.tables for r in t.rows for c in r.cells]
        return "\n".join(parts)
    return data.decode("utf-8", "replace")


@verifier("doc.create")
def v_doc_create(ctx, args, out):
    checks = []
    f = ctx.s["files"].get(out["file_id"], {})
    try:
        data = ctx.eng.store.read_file(f.get("path", ""))
    except Exception as e:
        data, err = None, str(e)
    else:
        err = ""
    checks.append(C("실제 파일 존재", data is not None and hashlib.sha256(data).hexdigest() == out["sha256"],
                    f"{f.get('path')} {out['size']:,}바이트" if data else f"저장소에서 다시 읽지 못함 {err}"))
    if data is None:
        return checks
    fmt_ok = data[:2] == b"PK" if out["fmt"] == "docx" else True
    checks.append(C("파일 형식 검사", fmt_ok, f"{out['fmt']} 서명 확인" if fmt_ok else "DOCX(zip) 서명이 아님"))
    try:
        text = file_text(data, out["fmt"])
        checks.append(C("문서 열기 가능", True, f"추출 {len(text):,}자"))
    except Exception as e:
        checks.append(C("문서 열기 가능", False, str(e)))
        return checks
    missing = [r for r in out["required"] if _norm(r) not in _norm(text)]
    checks.append(C("필수 내용 포함", not missing, "빠짐: " + ", ".join(missing) if missing
                    else f"필수 항목 {len(out['required'])}개 확인"))
    return checks


@tool("doc.search", "문서 내용 검색", "조회")
def t_doc_search(ctx: TCtx, args):
    """문서·기억·아이디어에서 키워드로 찾고 해당 원문을 함께 돌려준다."""
    words = [w for w in (args.get("keywords") or str(args.get("query", "")).split()) if w]
    seen, rows = set(), []
    for w in words:
        for h in A.search(ctx.s, w, limit=10):
            if h["id"] in seen:
                continue
            seen.add(h["id"])
            rec = ctx.s["c"].get(h["collection"], {}).get(h["id"], {})
            if args.get("project_id") and rec.get("project_id") and rec["project_id"] != args["project_id"]:
                continue
            rows.append({"id": h["id"], "collection": h["collection"], "title": h["title"],
                         "content": str(rec.get("content", ""))[:4000], "snippet": h["snippet"]})
    return {"hits": rows[: int(args.get("limit", 10))], "keywords": words}


@verifier("doc.search")
def v_doc_search(ctx, args, out):
    real = all(h["id"] in ctx.s["c"].get(h["collection"], {}) for h in out["hits"])
    return [C("검색 결과가 실제 기록과 일치", real, f"{len(out['hits'])}건")]


@tool("file.read", "파일 읽기", "조회")
def t_file_read(ctx: TCtx, args):
    """저장된 문서(업로드 자료 포함)의 본문을 읽는다."""
    docs = ctx.s["c"].get("documents", {})
    d = docs.get(args.get("doc_id", "")) or next((x for x in docs.values() if _norm(x["title"]) == _norm(args.get("title"))), None)
    if not d:
        raise ToolError("not_found", f"문서 '{args.get('doc_id') or args.get('title')}'를 찾을 수 없음")
    text = str(d.get("content", ""))
    return {"doc_id": d["id"], "title": d["title"], "content": text[:20000], "chars": len(text),
            "lines": text.count("\n") + 1 if text else 0, "version": d.get("version", 1)}


@verifier("file.read")
def v_file_read(ctx, args, out):
    return [C("본문을 읽음", out["chars"] > 0, f"{out['chars']:,}자")]


@tool("file.modify", "파일 수정", "수정", risk="MEDIUM", writes=["documents"])
def t_file_modify(ctx: TCtx, args):
    """문서 본문에 내용을 덧붙이거나 바꾼다. 버전을 올리고 변경 이력을 남긴다."""
    d = ctx.s["c"].get("documents", {}).get(args["doc_id"])
    if not d:
        raise ToolError("not_found", "수정할 문서가 없음")
    before = int(d.get("version", 1))
    text = args["text"]
    if args.get("mode") == "replace":
        d["content"] = text
    else:
        d["content"] = (d.get("content") or "") + (f"\n\n## {args['heading']}\n" if args.get("heading") else "\n\n") + text
    d.update(version=before + 1, updated_at=iso(ctx.now))
    _stamp(ctx, d)
    ctx.eng.history("documents", d["id"], "update", ctx.now)
    return {"doc_id": d["id"], "version_before": before, "version": before + 1, "text": text}


@verifier("file.modify")
def v_file_modify(ctx, args, out):
    d = ctx.s["c"]["documents"][out["doc_id"]]
    return [C("버전 증가", d["version"] == out["version"] > out["version_before"], f"v{out['version_before']}→v{d['version']}"),
            C("변경 내용 반영", _norm(out["text"]) in _norm(d["content"]))]


# ================================================================== 외부 자료 (선택)
@tool("web.research", "웹 자료 조사 (위키백과)", "외부", network=True)
def t_web_research(ctx: TCtx, args):
    """한국어 위키백과 공개 API로 자료를 찾는다. 설정에서 켠 경우에만, 하루 호출 한도 안에서 실행한다."""
    st = ctx.s["settings"]
    if not st.get("web_research"):
        raise ToolError("disabled", "웹 자료 조사가 꺼져 있음 (설정에서 켤 수 있음)")
    u = ctx.eng.usage(ctx.now)
    if u.get("web_calls", 0) >= st.get("web_daily_limit", 30):
        raise ToolError("limit", "오늘 웹 조사 한도에 도달")
    import httpx
    client = HTTP_CLIENT or httpx.Client(timeout=10)
    q = " ".join(args.get("keywords") or [args.get("query", "")])[:100]
    base = "https://ko.wikipedia.org/w/api.php"
    hdr = {"User-Agent": "LIFE-OS/1.0 (personal assistant; contact via GitHub)"}
    try:
        u["web_calls"] = u.get("web_calls", 0) + 1
        r = client.get(base, params={"action": "query", "list": "search", "srsearch": q, "srlimit": int(args.get("limit", 4)),
                                     "format": "json", "utf8": 1}, headers=hdr)
        r.raise_for_status()
        titles = [h["title"] for h in r.json().get("query", {}).get("search", [])]
        pages = []
        if titles:
            u["web_calls"] += 1
            r2 = client.get(base, params={"action": "query", "prop": "extracts|info|revisions", "exintro": 1,
                                          "explaintext": 1, "inprop": "url", "rvprop": "timestamp",
                                          "titles": "|".join(titles), "format": "json", "utf8": 1}, headers=hdr)
            r2.raise_for_status()
            pages = list(r2.json().get("query", {}).get("pages", {}).values())
    except httpx.HTTPError as e:
        raise ToolError("transient", f"위키백과 접속 실패: {e}")
    fetched = iso(ctx.now)
    seen, rows, dropped = set(), [], 0
    for p in pages:
        url = p.get("fullurl") or f"https://ko.wikipedia.org/wiki/{p.get('title', '')}"
        key = _norm(p.get("title"))
        if key in seen or not p.get("extract"):
            dropped += 1
            continue
        seen.add(key)
        rows.append({"id": "web:" + short_hash(url), "collection": "web", "title": p["title"], "url": url,
                     "content": p["extract"][:3000], "snippet": p["extract"][:200], "fetched_at": fetched,
                     "last_modified": (p.get("revisions") or [{}])[0].get("timestamp"), "source": "위키백과"})
    return {"sources": rows, "dropped_duplicates": dropped, "query": q, "fetched_at": fetched}


@verifier("web.research")
def v_web_research(ctx, args, out):
    real = all(r["url"].startswith("https://") for r in out["sources"])
    return [C("실제 출처 존재", bool(out["sources"]) and real, f"{len(out['sources'])}건"),
            C("정보 기준 시각 기록", all(r.get("fetched_at") for r in out["sources"]), out["fetched_at"]),
            C("중복 자료 제거", len({_norm(r['title']) for r in out['sources']}) == len(out["sources"]),
              f"중복·빈 결과 {out['dropped_duplicates']}건 제거")]


# ================================================================== 기억·통계·DB
@tool("memory.search", "기억 검색", "기억")
def t_memory_search(ctx: TCtx, args):
    """관련 기억을 찾는다 (프로젝트 범위 강제, 같은 주제는 최신 확정 지시 우선)."""
    return M.search(ctx.s, ctx.now, args.get("query", ""), args.get("project_id") or None, args.get("agent"))


@verifier("memory.search")
def v_memory_search(ctx, args, out):
    pid = args.get("project_id") or ""
    leak = [m for m in out["active"] if m["project_id"] and m["project_id"] != pid]
    return [C("프로젝트 범위 준수", not leak, f"범위: {out['scope']}, {len(out['active'])}건")]


@tool("memory.save", "기억 저장", "기억")
def t_memory_save(ctx: TCtx, args):
    """확정 사실·AI 가설·실행 결과를 구분해 기억으로 저장한다."""
    m = M.save(ctx.s, ctx.now, args["content"], args.get("kind", "실행 결과"), args.get("agent", "all"),
               args.get("project_id", ""), args.get("key", ""), args.get("tags", ""),
               f"워크플로 {ctx.wf['id']}" if args.get("kind") != "확정 사실" else "사용자 지시")
    return {"memory_id": m["id"], "kind": m["kind"], "key": m["key"], "project_id": m["project_id"]}


@verifier("memory.save")
def v_memory_save(ctx, args, out):
    found = M.search(ctx.s, ctx.now, "", out["project_id"] or None)["active"]
    return [C("저장 확인", out["memory_id"] in ctx.s["c"].get("memories", {})),
            C("다음 실행에서 검색됨", out["kind"] != "확정 사실" or "규칙" not in args.get("tags", "")
              or any(m["id"] == out["memory_id"] for m in found))]


STATS = {"schedule": A.a_schedule, "study": A.a_study, "writing": A.a_writing, "finance": A.a_finance,
         "invest": A.a_invest, "project": A.a_project, "life": A.a_life, "creative": A.a_creative, "research": A.a_research}


@tool("stats.compute", "통계 계산", "분석")
def t_stats(ctx: TCtx, args):
    """일정·학습·집필·재무·투자·프로젝트·생활·창작 통계를 계산한다."""
    topic = args.get("topic", "schedule")
    if topic not in STATS:
        raise ToolError("bad_input", f"알 수 없는 통계 주제: {topic}")
    a = STATS[topic](ctx.s, ctx.now)
    return {"topic": topic, "lines": a["lines"]}


@verifier("stats.compute")
def v_stats(ctx, args, out):
    return [C("계산 결과 있음", bool(out["lines"]), f"{len(out['lines'])}줄")]


DB_READABLE = ["events", "goals", "routines", "subjects", "study_logs", "writings", "writing_logs", "projects",
               "project_tasks", "expenses", "budgets", "holdings", "invest_notes", "ideas", "documents", "life_logs"]


@tool("db.query", "데이터베이스 조회", "조회")
def t_db_query(ctx: TCtx, args):
    """허용된 컬렉션을 조건(같음·포함)으로 읽는다. 읽기 전용."""
    coll = args.get("collection")
    if coll not in DB_READABLE:
        raise ToolError("bad_input", f"조회할 수 없는 컬렉션: {coll}")
    where, contains = args.get("where") or {}, _norm(args.get("contains"))
    rows = [r for r in A.V(ctx.s, coll) if all(r.get(k) == v for k, v in where.items())
            and (not contains or contains in _norm(" ".join(str(v) for v in r.values())))]
    return {"collection": coll, "count": len(rows), "rows": rows[: int(args.get("limit", 50))]}


@verifier("db.query")
def v_db_query(ctx, args, out):
    return [C("조회 완료", out["count"] >= 0, f"{out['count']}건")]


@tool("approval.request", "승인 요청 생성", "승인")
def t_approval(ctx: TCtx, args):
    """사용자 확인이 필요한 지점. 실행 엔진이 승인을 받은 뒤에만 이 단계를 통과시킨다."""
    if not ctx.step.get("approved"):
        raise ToolError("needs_approval", args.get("action", "사용자 확인 필요"))
    return {"approved": True, "action": args.get("action", "")}


@verifier("approval.request")
def v_approval(ctx, args, out):
    return [C("사용자 승인", out["approved"])]


# ================================================================== 협업용 분석 단계
STOP = set("그리고 그러나 하지만 또한 위해 대한 있는 있다 한다 하는 이다 것이 것은 수가 등을 에서 으로 이번 발표 준비 자료 "
           "the and for with this that are from".split())


def keywords(text: str, n: int = 6) -> list:
    words = [w for w in re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9]+", str(text)) if w.lower() not in STOP and len(w) >= 2]
    stems = [re.sub(r"(은|는|이|가|을|를|의|에|와|과|로|으로|에서|하기|하는|해줘|해 줘)$", "", w) for w in words]
    return [w for w, _ in Counter(x for x in stems if len(x) >= 2 and x.lower() not in STOP).most_common(n)]


@tool("presentation.locate", "발표 일정·목표 확인", "분석")
def t_pres_locate(ctx: TCtx, args):
    """다가오는 발표 일정(14일 이내)과 관련 목표·프로젝트를 찾고 주제 키워드를 정한다."""
    td = today(ctx.s, ctx.now)
    goal = args.get("goal", "")
    evs = sorted([e for e in A.V(ctx.s, "events") if "발표" in str(e.get("title")) and td <= e.get("date", "") <= dadd(td, 14)],
                 key=lambda e: e["date"])
    gk = set(keywords(goal, 8))
    if gk and evs:
        evs.sort(key=lambda e: (-len(gk & set(keywords(e["title"], 8))), e["date"]))
    ev = evs[0] if evs else None
    topic_src = re.sub(r"(이번\s*주|다음\s*주|발표\s*준비\S*|준비해\s*줘|해\s*줘)", " ", goal)
    topic = (ev and re.sub(r"발표", "", ev["title"]).strip()) or topic_src.strip() or goal
    goals = [g["title"] for g in A.V(ctx.s, "goals") if g.get("status", "진행") == "진행"
             and set(keywords(g["title"])) & set(keywords(topic + " " + goal))]
    return {"event": ev and {k: ev.get(k) for k in ("id", "title", "date", "start", "end")},
            "date": ev["date"] if ev else None, "days_left": ddiff(ev["date"], td) if ev else None,
            "topic": topic[:80], "keywords": keywords(topic + " " + goal, 6) or [topic[:20]], "goals": goals}


@verifier("presentation.locate")
def v_pres_locate(ctx, args, out):
    return [C("발표 주제 확정", bool(out["topic"]), out["topic"]),
            C("발표 일정 확인", True, f"{out['date']} (D-{out['days_left']})" if out["date"] else "등록된 발표 일정 없음 — 날짜 없이 진행")]


@tool("research.collect", "참고 자료 수집", "조회")
def t_research_collect(ctx: TCtx, args):
    """내부 문서·기억·아이디어를 키워드로 모으고, 웹 조사가 켜져 있으면 위키백과 자료를 더한다. 중복은 제거한다."""
    kws = args.get("keywords") or []
    internal = t_doc_search(ctx, {"keywords": kws, "limit": 8, "project_id": args.get("project_id")})["hits"]
    web, web_note = [], "웹 조사 꺼짐"
    if ctx.s["settings"].get("web_research"):
        try:
            res = t_web_research(ctx, {"keywords": kws[:3], "limit": 3})
            web, web_note = res["sources"], f"위키백과 {len(res['sources'])}건"
        except ToolError as e:
            web_note = f"웹 조사 실패: {e.msg}"
    fetched = iso(ctx.now)
    seen, sources = set(), []
    for r in internal + web:
        k = _norm(r["title"]) + short_hash(_norm(r["content"])[:300])
        if k in seen:
            continue
        seen.add(k)
        sources.append({**r, "fetched_at": r.get("fetched_at") or fetched})
    digest = []
    for r in sources:
        digest += [f"## {r['title']}", f"- 출처: {r.get('url') or '내부 ' + {'documents': '문서', 'memories': '기억', 'ideas': '아이디어'}.get(r['collection'], r['collection'])}",
                   f"- 기준 시각: {r['fetched_at']}" + (f" (원문 수정 {r['last_modified']})" if r.get("last_modified") else ""),
                   f"- 요약: {r['snippet'][:200]}", ""]
    return {"sources": sources, "keywords": kws, "internal": len(internal), "web": len(web), "web_note": web_note,
            "dropped_duplicates": len(internal) + len(web) - len(sources), "titles": [r["title"] for r in sources],
            "digest": "\n".join(digest) or "(수집된 자료 없음)"}


@verifier("research.collect")
def v_research_collect(ctx, args, out):
    real = all((r["collection"] == "web" and r.get("url")) or r["id"] in ctx.s["c"].get(r["collection"], {})
               for r in out["sources"])
    need = int(args.get("min_sources") or 1)
    return [C(f"자료 {need}건 이상", len(out["sources"]) >= need, f"내부 {out['internal']}건 · {out['web_note']}"),
            C("실제 출처 존재", real), C("정보 기준 시각 기록", all(r.get("fetched_at") for r in out["sources"])),
            C("중복 자료 제거", True, f"{out['dropped_duplicates']}건 제거")]


def _sentences(text: str) -> list:
    return [x.strip() for x in re.split(r"(?<=[.!?。])\s+|\n+", str(text)) if 8 <= len(x.strip()) <= 220]


@tool("concepts.extract", "핵심 개념 정리", "분석")
def t_concepts(ctx: TCtx, args):
    """수집한 자료 본문에서 자주 나오는 핵심어를 뽑고, 각 핵심어가 들어간 원문 문장을 근거로 붙인다 (통계 기반, AI 아님)."""
    sources = args.get("sources") or []
    n = max(1, min(int(args.get("max") or 5), 8))
    text = "\n".join(s["content"] for s in sources)
    terms = [t for t in keywords(text, n * 3) if t not in set(args.get("exclude") or [])][:n]
    concepts = []
    for t in terms:
        sents, ids = [], []
        for s in sources:
            for sen in _sentences(s["content"]):
                if t in sen and sen not in sents:
                    sents.append(sen)
                    ids.append(s["id"])
                if len(sents) >= 2:
                    break
        if sents:
            concepts.append({"term": t, "sentences": sents, "source_ids": sorted(set(ids))})
    return {"concepts": concepts, "topic": args.get("topic", ""), "from_sources": [s["id"] for s in sources]}


@verifier("concepts.extract")
def v_concepts(ctx, args, out):
    grounded = all(c["source_ids"] and c["sentences"] for c in out["concepts"])
    return [C("근거 문장이 있는 개념만 사용", grounded, f"개념 {len(out['concepts'])}개"),
            C("개념 1개 이상", bool(out["concepts"]) or not args.get("sources"),
              "자료가 없어 개념을 뽑지 못함" if not args.get("sources") else "")]


@tool("outline.build", "발표자료 구성", "분석")
def t_outline(ctx: TCtx, args):
    """주제·핵심 개념·출처로 발표 슬라이드 구성을 만든다. 확정 규칙의 최대 슬라이드 수를 지킨다."""
    topic, concepts, sources = args.get("topic", ""), args.get("concepts") or [], args.get("sources") or []
    max_slides = int(args.get("max_slides") or 10)
    body_slots = max(1, max_slides - 3)  # 표지·목차·마무리
    use = concepts[:body_slots]
    for f in args.get("fix") or []:      # 품질 검사에서 빠졌다고 지적된 개념은 반드시 넣는다
        if f not in [c["term"] for c in use]:
            add = next((c for c in concepts if c["term"] == f), {"term": f, "sentences": [], "source_ids": []})
            use = (use[: body_slots - 1] + [add]) if len(use) >= body_slots else use + [add]
    slides = [{"title": topic or "발표", "bullets": [f"발표일: {args['date']}" if args.get("date") else "발표일 미정"]},
              {"title": "목차", "bullets": [c["term"] for c in use] or ["(정리된 개념 없음)"]}]
    for c in use:
        slides.append({"title": c["term"], "bullets": c["sentences"] or ["(근거 문장 없음 — 직접 보충 필요)"]})
    if args.get("summary"):
        slides.insert(2, {"title": "핵심 요약", "bullets": [x for x in str(args["summary"]).split("\n") if x.strip()][:4]})
        if len(slides) + 1 > max_slides:
            slides.pop(2)
    slides.append({"title": "참고 자료", "bullets": [f"{s['title']}" + (f" ({s['url']})" if s.get("url") else "")
                                                for s in sources] or ["(참고 자료 없음)"]})
    slides = slides[:max_slides]
    md = []
    for i, sl in enumerate(slides, 1):
        md += [f"## 슬라이드 {i}. {sl['title']}", *[f"- {b}" for b in sl["bullets"]], ""]
    required = [c["term"] for c in use] + [s["title"] for s in sources][:5]
    if args.get("date"):
        required.append(args["date"])
    return {"title": f"{topic} 발표자료", "content": "\n".join(md), "slides": len(slides), "max_slides": max_slides,
            "required": required, "used_concepts": [c["term"] for c in use]}


@verifier("outline.build")
def v_outline(ctx, args, out):
    return [C("슬라이드 수 규칙 준수", out["slides"] <= out["max_slides"], f"{out['slides']}/{out['max_slides']}장")]


@tool("quality.check", "오류·누락 검사", "분석")
def t_quality(ctx: TCtx, args):
    """만들어진 파일을 저장소에서 다시 읽어 개념·출처·발표일 누락과 슬라이드 수를 검사한다."""
    f = ctx.s["files"].get(args.get("file_id", ""))
    if not f:
        raise ToolError("not_found", "검사할 파일이 없음")
    data = ctx.eng.store.read_file(f["path"])
    if data is None:
        raise ToolError("file_missing", "파일을 저장소에서 읽을 수 없음")
    text = file_text(data, f["fmt"])
    missing_concepts = [c["term"] for c in args.get("concepts") or [] if c["term"] not in text]
    missing_sources = [s["title"] for s in args.get("sources") or [] if _norm(s["title"]) not in _norm(text)]
    problems = []
    if args.get("date") and args["date"] not in text:
        problems.append(f"발표일 {args['date']} 누락")
    if not args.get("sources"):
        problems.append("참고 자료가 하나도 없음 — 자료를 올리거나 웹 조사를 켜야 함")
    slides = len(re.findall(r"슬라이드 \d+\.", text))
    mx = int(args.get("max_slides") or 99)
    if slides > mx:
        problems.append(f"슬라이드 {slides}장 > 규칙 {mx}장")
    return {"file_id": f["id"], "missing_concepts": missing_concepts, "missing_sources": missing_sources,
            "problems": problems, "slides": slides}


@verifier("quality.check")
def v_quality(ctx, args, out):
    return [C("핵심 개념 누락 없음", not out["missing_concepts"], ", ".join(out["missing_concepts"])),
            C("출처 누락 없음", not out["missing_sources"], ", ".join(out["missing_sources"])),
            C("치명적 문제 없음", not [p for p in out["problems"] if "슬라이드" in p or "발표일" in p], "; ".join(out["problems"]))]


@tool("todo.derive", "할 일 도출", "분석")
def t_todo_derive(ctx: TCtx, args):
    """명령에 적힌 항목 + 확정된 프로젝트 규칙의 기본 할 일 + 데이터에서 보이는 빈틈으로 할 일 목록을 만든다."""
    p = args.get("project") or {}
    rules = args.get("rules") or {}
    have = {_norm(t["title"]) for t in p.get("tasks", [])}
    td = today(ctx.s, ctx.now)
    items = []

    def add(title, why, due=None, milestone=False):
        if _norm(title) in have or any(_norm(i["title"]) == _norm(title) for i in items):
            return
        items.append({"title": title, "why": why, "due": due, "milestone": milestone})
    for x in args.get("items") or []:
        add(x, "명령에 적힌 항목")
    for x in rules.get("checklist", []):
        add(x, "확정된 프로젝트 규칙의 기본 할 일")
    dl = p.get("deadline")
    if dl:
        lead = int(rules.get("lead_days") or 1)
        due = dadd(dl, -lead)
        if not any(t.get("due") and t["due"] >= due for t in p.get("tasks", [])):
            add("최종 점검", f"마감 {dl} 직전 점검 작업이 없음" + (f" (규칙: 마감 {lead}일 전 완료)" if rules.get("lead_days") else ""),
                due=max(due, td), milestone=True)
    for d in p.get("delayed", []):
        add(f"「{d['title']}」 일정 다시 잡기", f"기한 {d['due']}이 지났는데 미완료")
    return {"project_id": p.get("id"), "items": items}


@verifier("todo.derive")
def v_todo_derive(ctx, args, out):
    return [C("추가할 할 일 1개 이상", bool(out["items"]), f"{len(out['items'])}개" if out["items"] else "이미 모든 항목이 있음")]


@tool("blocks.plan", "시간 확보 계획", "분석")
def t_blocks_plan(ctx: TCtx, args):
    """기한 전까지의 빈 시간에서 필요한 횟수만큼 시간 블록(리허설·복습·집필 등) 후보를 고른다."""
    td = today(ctx.s, ctx.now)
    before = args.get("before") or dadd(td, 7)
    n, dur, label = int(args.get("count") or 2), int(args.get("duration_min") or 60), args.get("label", "준비")
    picked = []
    for k in range(1, max(2, ddiff(before, td))):      # 하루에 하나씩 나누어 배치
        day = dadd(td, k)
        if day >= before or len(picked) >= n:
            break
        picked += _alternatives(ctx.s, day, dur, days=1, limit=1, before=before)
    items = [{"title": f"{label} {i + 1}회차", "date": c["date"], "start": c["start"], "end": c["end"], "type": "할일",
              "before": before} for i, c in enumerate(picked)]
    return {"items": items, "requested": n, "before": before}


@verifier("blocks.plan")
def v_blocks_plan(ctx, args, out):
    return [C("필요한 횟수만큼 빈 시간 확보", len(out["items"]) >= out["requested"],
              f"{len(out['items'])}/{out['requested']}회 (기한 {out['before']} 전)")]


def human(x) -> list:
    """산출물을 사람이 읽을 문장으로 바꾼다."""
    if x in (None, "", [], {}):
        return []
    if isinstance(x, list):
        return [ln for v in x for ln in human(v)]
    if isinstance(x, dict):
        if "content" in x and "reason" in x:
            return [f"이전 기억 대체: {x['content'][:60]} — {x['reason']}"]
        if "keep" in x and "move" in x:
            return [f"{x['date']} 「{x['keep']['title']}」 {x['keep']['start']}~{x['keep']['end']} ↔ 「{x['move']['title']}」 "
                    f"{x['move']['start']}~{x['move']['end']} 충돌 — 대안 {len(x['alternatives'])}개 제시"]
        if "from" in x and "to" in x:
            return [f"「{x['title']}」 {x['from']} → {x['to']}"]
        if "title" in x:
            extra = " ".join(str(x[k]) for k in ("date", "start") if x.get(k))
            if x.get("end"):
                extra += f"~{x['end']}"
            if x.get("due"):
                extra += f" 기한 {x['due']}"
            return [f"{x['title']}" + (f" ({extra.strip()})" if extra.strip() else "") + (f" — {x['why']}" if x.get("why") else "")]
        if "term" in x:
            return [x["term"]]
        if "content" in x:
            return [str(x["content"])[:120]]
        return [", ".join(f"{k}: {v}" for k, v in x.items() if not isinstance(v, (list, dict)))]
    return [ln for ln in str(x).split("\n") if ln.strip()]


@tool("report.compose", "결과 보고서 작성", "분석")
def t_report(ctx: TCtx, args):
    """앞 단계들의 실제 산출물을 모아 사용자 보고를 만든다."""
    lines = []
    for item in args.get("lines") or []:
        for ln in human(item):
            if ln not in lines:
                lines.append(ln)
    return {"summary": args.get("summary") or (lines[0] if lines else "작업 완료"), "lines": lines,
            "links": args.get("links") or []}


@verifier("report.compose")
def v_report(ctx, args, out):
    return [C("보고 내용 있음", bool(out["summary"]))]


# ================================================================== 로컬 AI 문장 생성 (+비AI 대체)
def extractive_summary(text: str, n: int = 3) -> str:
    sents = _sentences(text)
    if not sents:
        return ""
    freq = Counter(w for w in re.findall(r"[가-힣A-Za-z]{2,}", text) if w not in STOP)
    scored = sorted(enumerate(sents), key=lambda x: -sum(freq[w] for w in re.findall(r"[가-힣A-Za-z]{2,}", x[1])) / (len(x[1]) ** 0.5))
    return "\n".join(s for _, s in sorted(scored[:n]))


def _flat_text(x) -> str:
    if isinstance(x, list):
        return "\n".join(_flat_text(v) for v in x)
    if isinstance(x, dict):
        if "sentences" in x:
            return "\n".join(x["sentences"])
        return str(x.get("content") or x.get("title") or "")
    return str(x)


@tool("ai.generate", "AI 문장 생성", "AI")
def t_ai_generate(ctx: TCtx, args):
    """로컬 AI 모델로 문장을 생성한다. 모델이 없으면 허용된 경우에만 비AI 알고리즘(추출 요약)으로 대체하고 그렇게 표시한다."""
    if ctx.step.get("scratch", {}).get("force_fallback") and args.get("fallback") == "extractive":
        data = "\n".join(_flat_text(x) for x in (args.get("data") or []))[:3000]
        return {"text": extractive_summary(data, int(args.get("sentences", 3))), "mode": "rule_fallback",
                "model": "추출 요약 (AI 응답 실패로 대체, AI 아님)", "task_type": args["task_type"]}
    if "ai_text" in ctx.step.get("scratch", {}):
        sc = ctx.step["scratch"]
        return {"text": sc["ai_text"], "mode": "ai", "model": sc.get("ai_model", ""), "task_type": args["task_type"]}
    data = "\n".join(_flat_text(x) for x in (args.get("data") or []))[:3000]
    ses = ctx.eng.active(ctx.now)
    if ses and ses.get("ai_ready") and ctx.eng.usage(ctx.now)["model_calls"] < ctx.s["settings"]["ai_daily_limit"]:
        raise NeedAIJob(args["task_type"], ctx.s["agents"][ctx.agent]["system_prompt"],
                        f"<자료>\n{data}\n</자료>\n\n{args.get('instruction', '')}")
    if args.get("fallback") == "extractive":
        return {"text": extractive_summary(data, int(args.get("sentences", 3))), "mode": "rule_fallback",
                "model": "추출 요약 (AI 아님)", "task_type": args["task_type"]}
    raise ToolError("no_ai", "로컬 AI가 꺼져 있거나 한도에 도달 — 이 단계는 규칙으로 대체할 수 없음")


@verifier("ai.generate")
def v_ai_generate(ctx, args, out):
    text = out["text"].strip()
    echo = text and text in str(args.get("data"))[:3000] and out["mode"] == "ai" and len(text) > 200
    return [C("생성 결과 있음", len(text) >= 5, f"{len(text)}자 · {'로컬 AI ' + out['model'] if out['mode'] == 'ai' else out['model']}"),
            C("자료를 그대로 복사하지 않음", not echo)]


def catalog() -> list:
    return [{k: v for k, v in t.items() if k not in ("fn", "verify")} for t in TOOLS.values()]
