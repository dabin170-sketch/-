"""내 인생 검색 엔진 (PersonalSearchService).

저장된 기록을 그때그때 직접 훑어 찾는다 (별도 인덱스를 두지 않음) → 삭제한 자료는 즉시 결과에서 빠진다.
키워드·전문 검색 + 유형·프로젝트 필터 + 제목 가중치·최신순 재정렬. 벡터(의미) 검색은 무료 서버에서 실행할 임베딩 모델이 없어 제공하지 않는다.
"""
from __future__ import annotations

import re

from . import analysis as A

SOURCES = [
    # (컬렉션, 유형 이름, 화면, 제목 필드, 본문 필드들, 날짜 필드, 프로젝트 필드)
    ("projects", "프로젝트", "projects", "title", ["description", "goal"], "deadline", "id"),
    ("project_tasks", "할 일", "projects", "title", [], "due", "project_id"),
    ("documents", "문서", "docs", "title", ["content"], "updated_at", "project_id"),
    ("events", "일정", "planner", "title", ["note"], "date", None),
    ("memories", "AI 기억", "memory", "content", ["tags", "key"], "updated_at", "project_id"),
    ("meetings", "회의록", "meetings", "title", ["conclusions", "transcript"], "created_at", None),
    ("writings", "창작 설정", "records", "title", ["characters", "world"], "deadline", None),
    ("subjects", "학습 과목", "records", "title", [], "exam_date", None),
    ("study_logs", "학습 기록", "records", "note", [], "date", None),
    ("ideas", "아이디어", "records", "title", ["content", "tags"], "deadline", None),
    ("goals", "목표", "planner", "title", [], "deadline", None),
]


def _flat(v) -> str:
    if isinstance(v, list):
        return " ".join(_flat(x) for x in v)
    if isinstance(v, dict):
        return " ".join(_flat(x) for x in v.values())
    return str(v or "")


def search(s: dict, q: str, project_id: str | None = None, types: list | None = None, limit: int = 40,
           include_sensitive: bool = False) -> dict:
    words = [w for w in re.split(r"\s+", q.lower().strip()) if w][:8]
    if not words:
        return {"query": q, "results": [], "count": 0}
    rows = []

    def consider(kind, label, page, rid, title, body, d, pid, extra=None):
        if types and kind not in types:
            return
        if project_id and pid != project_id:
            return
        t, b = str(title or "").lower(), body.lower()
        if not all(w in t or w in b for w in words):
            return
        score = sum(3 * t.count(w) + b.count(w) for w in words)
        i = b.find(words[0]) if words[0] in b else -1
        snippet = (body[max(0, i - 40): i + 120] if i >= 0 else body[:160]).replace("\n", " ")
        rows.append({"type": kind, "type_label": label, "id": rid, "title": str(title or "")[:120] or "(제목 없음)",
                     "snippet": snippet, "date": str(d or "")[:10], "project_id": pid or "",
                     "project_title": s["c"].get("projects", {}).get(pid or "", {}).get("title", ""),
                     "location": {"page": page, "collection": kind, "id": rid}, "source": label, "score": score,
                     **(extra or {})})

    for coll, label, page, tf, bfs, df, pf in SOURCES:
        for r in A.V(s, coll):
            if coll == "memories" and r.get("sensitivity") == "민감" and not include_sensitive:
                continue
            pid = r.get(pf) if pf else None
            consider(coll, label, page, r["id"], r.get(tf) or (r.get("content") if coll == "memories" else ""),
                     " ".join(_flat(r.get(f)) for f in bfs), r.get(df) or r.get("updated_at"), pid)
    for w in s["workflows"].values():
        consider("workflows", "실행 기록", "flows", w["id"], w["title"],
                 " ".join([w["goal"], _flat((w.get("result") or {}).get("lines")), (w.get("result") or {}).get("summary", "")]),
                 w["created_at"], w.get("project_id"))
    for f in s["files"].values():
        consider("files", "생성 결과물", "docs", f["id"], f["name"], "", f["created_at"],
                 s["c"].get("documents", {}).get(f["doc_id"], {}).get("project_id"), {"url": f"/api/files/{f['id']}"})
    for it in s["intake"].values():
        consider("intake", "수집함", "intake", it["id"], it.get("title") or it["original_filename"], it.get("extracted_text", ""),
                 it["created_at"], it.get("project_id"))
    rows.sort(key=lambda r: (-r["score"], r["date"]), reverse=False)
    rows.sort(key=lambda r: -r["score"])
    return {"query": q, "results": rows[:limit], "count": len(rows),
            "note": "키워드 검색입니다 (의미 기반 벡터 검색은 지원하지 않음). 찾은 기록에 없는 내용은 답하지 않습니다."}
