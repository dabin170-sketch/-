"""장기 기억 엔진.

- 종류 분리: 확정 사실(사용자가 정한 것) / AI 가설(모델이 추정한 것) / 실행 결과(직원이 실제로 한 일)
- 프로젝트 범위 강제: 프로젝트 작업에서는 그 프로젝트의 기억과 공통 기억만, 다른 프로젝트 기억은 절대 섞지 않는다.
- 충돌 해결: 같은 '주제 키'의 확정 사실이 여러 개면 가장 최근 것이 우선. AI 가설은 확정 사실을 덮지 못한다.
- 규칙 해석: 확정된 프로젝트 규칙에서 숫자 제약(슬라이드 수, 리허설 횟수, 마감 며칠 전 등)을 읽어 실행에 반영한다.
"""
from __future__ import annotations

import re

from .util import iso, today, uid

KINDS = ["확정 사실", "AI 가설", "실행 결과"]
KIND_RANK = {"확정 사실": 0, "실행 결과": 1, "AI 가설": 2}


def kind_of(m: dict) -> str:
    k = m.get("kind")
    if k in KINDS:
        return k
    return "실행 결과" if str(m.get("source", "")).startswith("워크플로") else "확정 사실"


def tags_of(m: dict) -> set:
    return {t.strip() for t in str(m.get("tags", "")).split(",") if t.strip()}


def is_rule(m: dict) -> bool:
    return "규칙" in tags_of(m) and kind_of(m) == "확정 사실"


def _tokens(text: str) -> list:
    return [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", str(text).lower())]


def in_scope(m: dict, project_id: str | None, agent: str | None) -> bool:
    pid = m.get("project_id") or ""
    if project_id:
        if pid and pid != project_id:
            return False                      # 다른 프로젝트 기억은 제외
        if not pid and m.get("scope") == "프로젝트":
            return False
    elif pid:
        return False                          # 프로젝트 밖 작업에서는 프로젝트 기억을 쓰지 않는다
    if agent and m.get("agent_id") not in (None, "", "all", agent):
        return False
    return True


def search(s: dict, now, query: str = "", project_id: str | None = None, agent: str | None = None,
           kinds: list | None = None, limit: int = 20) -> dict:
    """관련 기억 검색. 반환: {active, superseded, rules, scope}"""
    td = today(s, now)
    words = _tokens(query)
    pool = []
    for m in s["c"].get("memories", {}).values():
        if m.get("expires_at") and m["expires_at"] < td:
            continue
        if not in_scope(m, project_id, agent):
            continue
        if kinds and kind_of(m) not in kinds:
            continue
        text = " ".join(str(m.get(k, "")) for k in ("content", "tags", "key")).lower()
        score = sum(text.count(w) for w in words)
        if words and not score and not is_rule(m):
            continue                           # 확정 규칙은 질의와 무관하게 항상 포함
        pool.append((m, score + (5 if is_rule(m) else 0)))

    # 같은 주제 키 → 확정 사실 우선, 그다음 최신 우선
    active, superseded, by_key = [], [], {}
    for m, sc in pool:
        key = (m.get("key") or "").strip()
        if not key:
            active.append((m, sc))
            continue
        by_key.setdefault(key, []).append((m, sc))
    for key, items in by_key.items():
        items.sort(key=lambda x: (KIND_RANK[kind_of(x[0])], _neg(x[0].get("updated_at", ""))))
        winner = items[0]
        active.append(winner)
        for m, _ in items[1:]:
            why = ("확정 사실이 있어 AI 가설을 쓰지 않음" if kind_of(m) == "AI 가설" and kind_of(winner[0]) == "확정 사실"
                   else f"같은 주제({key})의 더 최근 지시({winner[0].get('updated_at', '')[:10]})가 우선")
            superseded.append({"id": m["id"], "content": m.get("content", ""), "reason": why})
    active.sort(key=lambda x: (-x[1], KIND_RANK[kind_of(x[0])], _neg(x[0].get("updated_at", ""))))
    rows = [{"id": m["id"], "kind": kind_of(m), "content": m.get("content", ""), "key": m.get("key", ""),
             "project_id": m.get("project_id") or "", "updated_at": m.get("updated_at", ""),
             "rule": is_rule(m), "sensitive": m.get("sensitivity") == "민감"} for m, _ in active[:limit]]
    return {"active": rows, "superseded": superseded, "rules": [r for r in rows if r["rule"]],
            "scope": f"프로젝트 {project_id}" if project_id else "공통(프로젝트 기억 제외)"}


def _neg(s: str) -> str:
    """문자열 내림차순 정렬용."""
    return "".join(chr(0x10FFFF - ord(c)) for c in s)


def save(s: dict, now, content: str, kind: str, agent: str = "all", project_id: str = "", key: str = "",
         tags: str = "", source: str = "", confidence: float = 1.0) -> dict:
    """기억 저장. 같은 키·같은 종류·같은 프로젝트의 실행 결과는 새 것으로 교체한다 (무한히 쌓이지 않게)."""
    mems = s["c"].setdefault("memories", {})
    if kind == "실행 결과" and key:
        for m in list(mems.values()):
            if m.get("key") == key and kind_of(m) == "실행 결과" and (m.get("project_id") or "") == project_id:
                del mems[m["id"]]
                s["deleted"][m["id"]] = iso(now)
    rid = uid("mem_")
    rec = {"id": rid, "agent_id": agent, "scope": "프로젝트" if project_id else "장기", "project_id": project_id,
           "content": content[:2000], "tags": tags, "key": key, "kind": kind, "source": source or "직원 저장",
           "confidence": confidence, "expires_at": "", "sensitivity": "일반", "user_editable": True,
           "created_at": iso(now), "updated_at": iso(now)}
    mems[rid] = rec
    return rec


# ------------------------------------------------------------------ 규칙 해석
RULE_PATTERNS = [
    ("max_slides", r"(?:슬라이드|장표|페이지)[^0-9]{0,12}(\d+)\s*(?:장|쪽|페이지)", "발표 슬라이드 최대 {}장"),
    ("rehearsals", r"리허설[^0-9]{0,8}(\d+)\s*(?:회|번)", "리허설 {}회"),
    ("lead_days", r"마감\s*(\d+)\s*일\s*전", "마감 {}일 전까지 완료"),
    ("min_sources", r"(?:자료|출처|참고)[^0-9]{0,8}(\d+)\s*(?:개|건|편)\s*이상", "참고 자료 {}개 이상"),
    ("duration_min", r"(?:회의|일정)[^0-9]{0,10}(\d+)\s*분", "기본 일정 길이 {}분"),
]


def parse_rules(rules: list) -> dict:
    """확정 규칙 문장에서 숫자 제약을 읽는다. 최신 규칙이 앞에 오므로 먼저 나온 값을 쓴다."""
    out, applied = {}, []
    for r in sorted(rules, key=lambda r: r.get("updated_at", ""), reverse=True):
        text = r["content"]
        for name, pat, label in RULE_PATTERNS:
            if name in out:
                continue
            m = re.search(pat, text)
            if m:
                out[name] = int(m.group(1))
                applied.append({"rule_id": r["id"], "name": name, "value": out[name], "label": label.format(out[name]),
                                "text": text})
        for line in re.findall(r"(?:할 일|체크)\s*[:：]\s*(.+)", text):
            items = [x.strip() for x in re.split(r"[,/·]", line) if x.strip()]
            out.setdefault("checklist", [])
            out["checklist"] += [x for x in items if x not in out["checklist"]]
            applied.append({"rule_id": r["id"], "name": "checklist", "value": items, "label": "기본 할 일: " + ", ".join(items),
                            "text": text})
    return {"values": out, "applied": applied}
