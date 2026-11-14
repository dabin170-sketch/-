"""작업 계획 엔진: 사용자 요청 → 의도 파악 → 구조화된 실행 계획.

의도 파악과 계획 작성은 결정적 규칙(키워드·날짜 해석·템플릿)으로 한다. 작은 로컬 모델의 계획은 검증하기 어렵기 때문이다.
각 계획 단계에는 ID, 목표, 담당 직원, 입력, 도구, 선행 단계, 예상 산출물, 완료 기준, 실패 시 대응이 있다.
입력의 "$s1.필드" 는 앞 단계의 실제 산출물을, "$ctx.필드" 는 워크플로 맥락(기억·규칙)을 가리킨다.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import analysis as A
from . import docs as D
from . import memory as M
from .rules import DOC_KW, RULES
from .util import dadd, local, today

WD = "월화수목금토일"


# ------------------------------------------------------------------ 해석 도우미
def parse_date(text: str, td: str) -> str | None:
    t0 = date.fromisoformat(td)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text) or re.search(r"(?<![\d:])(\d{1,2})/(\d{1,2})(?![\d:])", text)
    if m:
        d = date(t0.year, int(m[1]), int(m[2]))
        return (d if d >= t0 else date(t0.year + 1, int(m[1]), int(m[2]))).isoformat()
    for word, n in (("오늘", 0), ("내일", 1), ("모레", 2), ("글피", 3)):
        if word in text:
            return dadd(td, n)
    m = re.search(r"(이번\s*주|다음\s*주|담주)?\s*([월화수목금토일])요일", text)
    if m:
        target = WD.index(m[2])
        base = t0 - timedelta(days=t0.weekday())
        if m[1] and m[1].replace(" ", "") in ("다음주", "담주"):
            base += timedelta(days=7)
            return (base + timedelta(days=target)).isoformat()
        d = base + timedelta(days=target)
        return (d if d >= t0 else d + timedelta(days=7)).isoformat()
    return None


def parse_time(text: str) -> str | None:
    m = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", text)
    if m:
        return f"{int(m[1]):02d}:{m[2]}"
    m = re.search(r"(오전|오후|저녁|밤|아침)?\s*(\d{1,2})\s*시\s*(반|(\d{1,2})\s*분)?", text)
    if not m:
        return None
    h = int(m[2])
    if m[1] in ("오후", "저녁", "밤") and h < 12:
        h += 12
    if not m[1] and 1 <= h <= 7:
        h += 12          # '3시'처럼 오전·오후가 없으면 낮 시간으로 본다
    mi = 30 if m[3] == "반" else int(m[4] or 0)
    return f"{h:02d}:{mi:02d}" if h < 24 else None


def parse_duration(text: str) -> int | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*시간\s*(반)?", text)
    if m:
        return int(float(m[1]) * 60 + (30 if m[2] else 0))
    m = re.search(r"(\d+)\s*분\s*(동안|짜리|간)?", text)
    return int(m[1]) if m and not re.search(r"\d+\s*시\s*\d+\s*분", text) else None


def quoted(text: str) -> list:
    items = re.findall(r"[「『\"'“‘]([^」』\"'”’]{1,80})[」』\"'”’]", text)
    if not items:
        m = re.search(r"[:：]\s*(.+)$", text)
        if m:
            items = [x.strip() for x in re.split(r"[,，/·]", m[1]) if x.strip()]
    return items


def match_project(s: dict, text: str, hint: str | None = None) -> dict | None:
    projects = A.V(s, "projects")
    if hint:
        return next((p for p in projects if p["id"] == hint), None)
    n = re.sub(r"\s+", "", text).lower()
    best, score = None, 0
    for p in projects:
        title = re.sub(r"\s+", "", p["title"]).lower()
        if title and title in n:
            sc = 100 + len(title)
        else:
            sc = sum(len(w) for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", p["title"].lower()) if w in n)
        if sc > score:
            best, score = p, sc
    return best if score >= 2 else None


TITLE_DROP = [r"\d{4}-\d{1,2}-\d{1,2}", r"\d{1,2}\s*월\s*\d{1,2}\s*일", r"\d{1,2}/\d{1,2}", r"오늘|내일|모레|글피",
              r"(이번|다음)\s*주", r"[월화수목금토일]요일", r"\d+(\.\d+)?\s*시간\s*반?",
              r"(오전|오후|저녁|아침|밤)?\s*\d{1,2}\s*시\s*(반|\d+\s*분)?", r"\d{1,2}:\d{2}", r"\d+\s*분\s*(동안|짜리)?",
              r"오전|오후|저녁|아침|밤", r"(잡아|추가해|등록해|넣어|예약해|만들어|해)\s*(줘|주세요|줄래)?(?=\s|$|[.!])", r"동안",
              r"(?<![가-힣])일정(?![가-힣])"]


def clean_title(g: str) -> str:
    t = g
    for p in TITLE_DROP:
        t = re.sub(p, " ", t)
    t = re.sub(r"(^|\s)(에|에서|으로|로|을|를|좀)(?=\s|$)", " ", t)
    t = re.sub(r"(?<=[가-힣])(에|으로)(?=\s|$)", "", t)
    return re.sub(r"\s+", " ", t).strip(" ,.!") or "새 일정"


def rule_key(text: str) -> str:
    for name, pat, _ in M.RULE_PATTERNS:
        if re.search(pat, text):
            return f"rule:{name}"
    return ""


# ------------------------------------------------------------------ 의도 파악
def interpret(s: dict, now, goal: str, agent: str | None = None, project_id: str | None = None) -> dict:
    td = today(s, now)
    g = goal.strip()
    proj = match_project(s, g, project_id)
    pid = proj["id"] if proj else None
    base = {"goal": g, "project_id": pid, "project_title": proj["title"] if proj else None}

    if re.search(r"기억해|기억해\s*줘|기억할\s*것|규칙\s*[:：]|규칙으로\s*(정|저장|해)", g):
        content = re.sub(r"(라고|이라고)?\s*(꼭\s*)?기억해\s*(줘|주세요|둬|두세요)?[.!]?$", "", g).strip()
        content = re.sub(r"^.*?규칙\s*[:：]\s*", "", content) if "규칙" in content and re.search(r"규칙\s*[:：]", content) else content
        return {**base, "template": "remember", "title": f"기억 저장: {content[:30]}", "lead": "chief",
                "params": {"content": content, "key": rule_key(content) or "",
                           "tags": "규칙" if "규칙" in g or rule_key(content) else "지시"}}
    if re.search(r"삭제|지워", g):
        target = (quoted(g) or [re.sub(r"(을|를)?\s*(삭제|지워)\S*", "", g).strip()])[0]
        return {**base, "template": "delete_record", "title": f"삭제: {target[:30]}", "lead": "system",
                "params": {"target": target}}
    if re.search(r"할\s*일|할일|작업|체크리스트|투두|to-?do", g, re.I) and (proj or "프로젝트" in g):
        return {**base, "template": "project_todos", "title": f"할 일 정리: {proj['title'] if proj else g[:20]}",
                "lead": "project", "params": {"items": quoted(g), "query": proj["title"] if proj else g}}
    if "발표" in g and re.search(r"준비|자료|만들|정리", g):
        return {**base, "template": "presentation", "title": f"발표 준비: {g[:30]}", "lead": "chief", "params": {}}
    if re.search(r"충돌|겹치", g) and not re.search(r"잡아|추가|등록|넣어|만들어", g):
        return {**base, "template": "conflict_resolve", "title": "일정 충돌 해결", "lead": "schedule",
                "params": {"days": 7}}
    d, t = parse_date(g, td), parse_time(g)
    if (d or t) and re.search(r"잡아|추가|등록|넣어|예약|만들어|일정|약속|회의|미팅", g) and not re.search(r"보고서|계획서|문서", g):
        rules = M.parse_rules(M.search(s, now, g, pid)["rules"])["values"]
        dur = parse_duration(g) or rules.get("duration_min") or 60
        title = (quoted(g) or [None])[0] or clean_title(g)
        return {**base, "template": "schedule", "title": f"일정 등록: {title}", "lead": "schedule",
                "params": {"title": title, "date": d or td, "start": t or "09:00", "duration_min": int(dur),
                           "assumed": [x for x, v in (("날짜(오늘로 가정)", d), ("시간(09:00으로 가정)", t)) if not v]}}
    if re.search(r"보고서|계획서|문서|일정표|회의록|요약본|기록부", g) and re.search(r"만들|작성|생성|뽑|정리|써", g):
        dt = "project_report" if "프로젝트" in g else None
        if not dt:  # 구체적인 문서 종류를 먼저 본다 ('이번 주 일정표' → 일정표)
            order = ["timetable", "ledger_summary", "invest_log", "study_plan", "writing_report", "project_report",
                     "weekly_report", "daily_report"]
            dt = next((k for k in order if any(w in g for w in DOC_KW[k])), "daily_report")
        return {**base, "template": "document", "title": f"문서 작성: {D.DOC_TYPES[dt]}", "lead": "docs",
                "params": {"doc_type": dt}}
    if re.search(r"자료\s*(조사|수집|찾)|참고\s*자료|조사해", g):
        return {**base, "template": "research", "title": f"자료 조사: {g[:30]}", "lead": "research",
                "params": {"query": g}}
    # 기존 규칙 업무에 해당하는 지시 (예: 예산 점검해 줘)
    best, sc = None, 0
    for k, r in RULES.items():
        if agent and r["agent"] != agent or not r["kw"] or k == "docs.generate":
            continue
        n = sum(1 for w in r["kw"] if w in g)
        if n > sc:
            best, sc = k, n
    if best:
        return {**base, "template": "rule_task", "title": RULES[best]["title"], "lead": RULES[best]["agent"],
                "params": {"kind": best}}
    return {**base, "template": "answer", "title": f"요청 처리: {g[:30]}", "lead": agent or "chief", "params": {}}


# ------------------------------------------------------------------ 계획 템플릿
def S(sid, goal, agent, tool, args, deps=(), expected="", criteria="도구 검증 기준 전부 통과", on_fail="retry",
      max_retries=2, timeout=30, revise_target=None, skip_if_empty=None):
    return {"id": sid, "skip_if_empty": skip_if_empty, "goal": goal, "agent": agent, "tool": tool, "args": args, "depends_on": list(deps),
            "expected_output": expected, "done_criteria": criteria, "on_fail": on_fail, "max_retries": max_retries,
            "timeout_sec": timeout, "revise_target": revise_target, "status": "PENDING", "attempts": 0,
            "errors": [], "output": None, "observation": "", "verify": None, "inputs_from": [], "input_digest": {},
            "scratch": {}, "approved": False, "started_at": None, "finished_at": None, "ms": 0}


def build(wf: dict, s: dict, now) -> list:
    p, t, pid = wf["params"], wf["template"], wf.get("project_id")
    td = today(s, now)
    if t == "remember":
        return [S("s1", "확정 사실로 기억 저장", "chief", "memory.save",
                  {"content": p["content"], "kind": "확정 사실", "key": p["key"], "project_id": pid or "",
                   "tags": p["tags"], "agent": "all"}, expected="기억 ID", on_fail="abort"),
                S("s2", "다음 실행에서 검색되는지 확인", "chief", "memory.search",
                  {"query": p["content"], "project_id": pid or ""}, ["s1"], "검색 결과", on_fail="abort"),
                S("s3", "보고", "chief", "report.compose",
                  {"summary": "기억했습니다: " + p["content"][:60],
                   "lines": ["종류: 확정 사실" + (" · 규칙" if "규칙" in p["tags"] else ""),
                             f"범위: {wf.get('project_title') or '공통'}", "$s2.superseded"]}, ["s2"])]
    if t == "delete_record":
        return [S("s1", "삭제할 기록 찾기", "system", "record.find", {"target": p["target"]}, expected="기록 1건",
                  on_fail="abort"),
                S("s2", "기록 삭제 (위험도 HIGH — 항상 승인)", "system", "record.delete",
                  {"collection": "$s1.collection", "id": "$s1.id"}, ["s1"], "삭제 확인", on_fail="abort"),
                S("s3", "결과 보고", "chief", "report.compose",
                  {"summary": "요청한 기록을 삭제했습니다", "lines": ["$s2.title"]}, ["s2"])]
    if t == "project_todos":
        return [S("s1", "프로젝트 데이터 검색", "project", "project.search",
                  {"query": p["query"], "project_id": pid}, expected="프로젝트와 하위 작업 목록", on_fail="abort"),
                S("s2", "관련 할 일 도출 (명령·확정 규칙·빈틈)", "project", "todo.derive",
                  {"project": "$s1.projects.0", "items": p.get("items"), "rules": "$ctx.rules"}, ["s1"],
                  "추가할 할 일 목록과 이유", on_fail="skip", max_retries=0),
                S("s3", "할 일 생성", "project", "task.create",
                  {"project_id": "$s1.projects.0.id", "items": "$s2.items"}, ["s2"], "생성된 할 일 ID",
                  skip_if_empty="items"),
                S("s4", "결과 보고", "chief", "report.compose",
                  {"summary": "할 일 정리 완료", "lines": ["$s2.items", "$s3.created", "$s3.skipped"]}, ["s3"])]
    if t == "document":
        return [S("s1", "데이터 수집·본문 작성", "docs", "doc.compose", {"doc_type": p["doc_type"], **{k: v for k, v in p.items() if k != "doc_type"}},
                  expected="제목·본문·필수 항목"),
                S("s2", "문서 생성 및 실제 파일 저장", "docs", "doc.create",
                  {"title": "$s1.title", "content": "$s1.content", "type": "$s1.type", "required": "$s1.required",
                   "project_id": pid or ""}, ["s1"], "DOCX 파일과 다운로드 링크",
                  "파일 존재·형식·열기·필수 내용 포함"),
                S("s3", "결과 보고", "chief", "report.compose",
                  {"summary": "문서를 만들고 검증했습니다", "lines": ["$s2.title", "$s1.used"],
                   "links": [{"label": "DOCX 내려받기", "url": "$s2.url"}]}, ["s2"])]
    if t == "schedule":
        return [S("s1", "해당 날짜 일정 조회", "schedule", "event.query", {"date": p["date"]}, expected="그날 일정"),
                S("s2", "시간 충돌 검사·대안 계산", "schedule", "schedule.check",
                  {"date": p["date"], "start": p["start"], "duration_min": p["duration_min"]}, ["s1"], "충돌·대안"),
                S("s3", "일정 생성", "schedule", "event.create",
                  {"title": p["title"], "date": p["date"], "start": p["start"], "end": "$s2.end"}, ["s2"],
                  "저장된 일정", "DB 저장·중복 없음·충돌 없음", on_fail="ask"),
                S("s4", "결과 보고", "chief", "report.compose",
                  {"summary": f"「{p['title']}」 일정 처리", "lines": ["$s2.conflicts", "$s3.created", "$s3.items"]}, ["s3"])]
    if t == "conflict_resolve":
        return [S("s1", "앞으로 일정 조회", "schedule", "event.query", {"days": p.get("days", 7)}, expected="일정 목록"),
                S("s2", "충돌 발견·대안 계산", "schedule", "schedule.conflicts", {"days": p.get("days", 7)}, ["s1"],
                  "충돌 쌍과 대안 시간"),
                S("s3", "사용자가 고른 대안 적용", "schedule", "event.update", {"conflicts": "$s2.conflicts"}, ["s2"],
                  "변경된 일정", on_fail="ask"),
                S("s4", "결과 보고", "chief", "report.compose",
                  {"summary": "일정 충돌 점검 완료", "lines": ["$s2.conflicts", "$s3.moved", "$s3.kept"]}, ["s3"])]
    if t == "presentation":
        return [
            S("s1", "발표 일정과 목표 확인", "chief", "presentation.locate", {"goal": wf["goal"]}, expected="발표일·주제·키워드"),
            S("s2", "참고 자료 조사", "research", "research.collect",
              {"keywords": "$s1.keywords", "project_id": pid, "min_sources": "$ctx.rules.min_sources"}, ["s1"],
              "출처가 있는 자료 목록", on_fail="skip", max_retries=1),
            S("s3", "핵심 개념 정리", "study", "concepts.extract",
              {"sources": "$s2.sources", "topic": "$s1.topic", "max": "$ctx.rules.max_slides_body"}, ["s2"],
              "근거 문장이 붙은 핵심 개념"),
            S("s4", "핵심 요약 (로컬 AI, 없으면 추출 요약)", "study", "ai.generate",
              {"task_type": "summary", "data": "$s3.concepts", "fallback": "extractive",
               "instruction": "위 자료의 핵심을 발표용으로 3문장 이내로 요약하라."}, ["s3"], "요약문", timeout=180),
            S("s5", "발표자료 구성", "docs", "outline.build",
              {"topic": "$s1.topic", "date": "$s1.date", "concepts": "$s3.concepts", "sources": "$s2.sources",
               "summary": "$s4.text", "max_slides": "$ctx.rules.max_slides"}, ["s4"], "슬라이드 구성"),
            S("s6", "발표자료(PPTX)·대본 파일 생성", "docs", "doc.create",
              {"title": "$s5.title", "content": "$s5.content", "type": "AI 초안", "required": "$s5.required",
               "project_id": pid or "", "formats": ["pptx", "docx"], "require_notes": True}, ["s5"],
              "PPTX(슬라이드 노트에 대본) + DOCX", "파일 존재·형식·열기·필수 내용·슬라이드별 대본"),
            S("s7", "오류·누락 검사", "system", "quality.check",
              {"file_id": "$s6.file_id", "concepts": "$s3.concepts", "sources": "$s2.sources", "date": "$s1.date",
               "max_slides": "$ctx.rules.max_slides"}, ["s6"], "누락 목록", on_fail="revise", max_retries=1,
              revise_target="s5"),
            S("s8", "남은 준비 일정(리허설) 계산", "chief", "blocks.plan",
              {"label": "발표 리허설", "before": "$s1.date", "count": "$ctx.rules.rehearsals", "duration_min": 60},
              ["s7"], "리허설 후보 시간", on_fail="skip", max_retries=0),
            S("s9", "결과 보고", "chief", "report.compose",
              {"summary": "발표 준비 자료를 만들고 검사했습니다",
               "lines": ["$s1.topic", "$s1.date", "$s2.web_note", "$s7.problems", "$s8.items"],
               "links": [{"label": "발표자료 PPTX", "url": "$s6.url"}]}, ["s8"])]
    if t == "time_blocks":
        return [S("s1", "빈 시간에서 후보 고르기", p.get("agent", "schedule"), "blocks.plan",
                  {"label": p["label"], "before": p.get("before"), "count": p.get("count", 2),
                   "duration_min": p.get("duration_min", 60)}, expected="시간 후보", on_fail="abort"),
                S("s2", "일정 등록", "schedule", "event.create", {"items": "$s1.items"}, ["s1"], "저장된 일정",
                  "DB 저장·중복 없음·충돌 없음", on_fail="ask", skip_if_empty="items"),
                S("s3", "결과 보고", "chief", "report.compose",
                  {"summary": f"{p['label']} 시간 확보", "lines": ["$s1.items", "$s2.created"]}, ["s2"])]
    if t == "research":
        return [S("s1", "자료 수집", "research", "research.collect",
                  {"keywords": [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", p["query"])
                                if w not in ("자료", "조사", "조사해", "참고", "찾아", "수집", "줘", "해줘")][:5],
                   "project_id": pid, "min_sources": "$ctx.rules.min_sources"}, expected="출처 있는 자료",
                  on_fail="abort", max_retries=1),
                S("s2", "자료 모음 문서 저장", "research", "doc.create",
                  {"title": f"참고 자료 모음 — {p['query'][:30]}", "content": "$s1.digest", "type": "일반",
                   "required": "$s1.titles", "project_id": pid or ""}, ["s1"], "DOCX 파일"),
                S("s3", "결과 보고", "chief", "report.compose",
                  {"summary": "참고 자료를 모았습니다", "lines": ["$s1.titles", "$s1.web_note"],
                   "links": [{"label": "자료 모음 DOCX", "url": "$s2.url"}]}, ["s2"])]
    # answer: 규칙으로 처리할 수 없는 일반 요청
    return [S("s1", "관련 기억 검색", wf["lead"], "memory.search", {"query": wf["goal"], "project_id": pid or ""},
              expected="관련 기억"),
            S("s2", "관련 데이터 검색", "research", "doc.search", {"query": wf["goal"], "limit": 6}, ["s1"], "관련 자료"),
            S("s3", "답변 작성 (로컬 AI 필요)", wf["lead"], "ai.generate",
              {"task_type": "answer", "data": ["$s1.active", "$s2.hits"], "require": "$s2.hits",
               "instruction": f"사용자 요청: {wf['goal'][:300]}\n자료에 있는 사실만 근거로 10문장 이내로 답하라."},
              ["s2"], "답변", on_fail="skip", max_retries=0, timeout=180, skip_if_empty="require"),
            S("s4", "결과 보고", "chief", "report.compose",
              {"summary": "요청 처리 결과", "lines": ["$s3.text", "$s2.hits"]}, ["s3"])]


def context_rules(rules: list) -> dict:
    parsed = M.parse_rules(rules)
    v = dict(parsed["values"])
    v.setdefault("max_slides", 10)
    v["max_slides_body"] = max(1, v["max_slides"] - 3)
    v.setdefault("rehearsals", 2)
    v.setdefault("min_sources", 1)
    return {"values": v, "applied": parsed["applied"]}


def validate(plan: list) -> list:
    """계획 검사: 단계 ID 중복, 존재하지 않는 선행 단계, 순환 의존을 막는다."""
    ids = [x["id"] for x in plan]
    errs = []
    if len(ids) != len(set(ids)):
        errs.append("단계 ID 중복")
    for x in plan:
        for d in x["depends_on"]:
            if d not in ids:
                errs.append(f"{x['id']}: 없는 선행 단계 {d}")
    seen, stack = set(), set()

    def dfs(n):
        if n in stack:
            errs.append(f"순환 의존: {n}")
            return
        if n in seen:
            return
        stack.add(n)
        for d in next(x for x in plan if x["id"] == n)["depends_on"]:
            if d in ids:
                dfs(d)
        stack.discard(n)
        seen.add(n)
    for i in ids:
        dfs(i)
    return errs
