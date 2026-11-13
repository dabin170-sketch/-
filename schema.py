"""사용자 데이터 컬렉션 정의와 입력 검증. 프론트엔드 폼도 이 정의로 그린다."""
from __future__ import annotations

import re

from .util import valid_date

AGENT_IDS = ["chief", "schedule", "study", "writing", "docs", "invest", "finance",
             "project", "research", "life", "creative", "system"]


def F(k, label, t="text", **kw):
    return {"k": k, "label": label, "type": t, **kw}


COLLECTIONS = {
    "events": {"label": "일정", "agent": "schedule", "fields": [
        F("title", "제목", required=True), F("date", "날짜", "date", required=True),
        F("start", "시작", "time"), F("end", "종료", "time"),
        F("type", "종류", "select", options=["일정", "마감", "할일"], default="일정"),
        F("importance", "중요도(1~5)", "number", min=1, max=5, default=3),
        F("done", "완료", "bool"), F("note", "메모", "textarea")]},
    "goals": {"label": "목표", "agent": "chief", "fields": [
        F("title", "목표", required=True),
        F("area", "영역", "select", options=["학습", "집필", "재정", "프로젝트", "생활", "기타"], default="기타"),
        F("importance", "중요도(1~5)", "number", min=1, max=5, default=3),
        F("deadline", "기한", "date"), F("target", "목표 수치", "number"), F("current", "현재 수치", "number"),
        F("unit", "단위"), F("status", "상태", "select", options=["진행", "완료", "보류"], default="진행")]},
    "routines": {"label": "루틴", "agent": "life", "fields": [
        F("title", "루틴", required=True), F("time", "시간", "time"),
        F("target_per_week", "주간 목표 횟수", "number", min=1, max=7, default=7)]},
    "routine_logs": {"label": "루틴 기록", "agent": "life", "fields": [
        F("routine_id", "루틴", "ref", ref="routines", required=True),
        F("date", "날짜", "date", required=True), F("done", "실행함", "bool", default=True)]},
    "life_logs": {"label": "수면·활동 기록", "agent": "life", "fields": [
        F("date", "날짜", "date", required=True), F("sleep_hours", "수면(시간)", "number", min=0, max=24),
        F("activity_minutes", "활동(분)", "number", min=0), F("note", "메모")]},
    "subjects": {"label": "학습 과목", "agent": "study", "fields": [
        F("title", "과목", required=True), F("total_units", "전체 분량", "number", min=0),
        F("done_units", "시작 시점까지 한 분량", "number", min=0), F("unit_label", "분량 단위(쪽·강 등)"),
        F("minutes_per_unit", "단위당 소요(분)", "number", min=0),
        F("exam_date", "시험일", "date"),
        F("review_interval_days", "복습 주기(일)", "number", min=1, max=60, default=3)]},
    "study_logs": {"label": "학습 기록", "agent": "study", "fields": [
        F("subject_id", "과목", "ref", ref="subjects", required=True),
        F("date", "날짜", "date", required=True), F("units", "분량", "number", min=0),
        F("minutes", "시간(분)", "number", min=0),
        F("kind", "종류", "select", options=["진도", "복습", "오답"], default="진도"), F("note", "메모")]},
    "writings": {"label": "집필 프로젝트", "agent": "writing", "fields": [
        F("title", "작품", required=True), F("target_count", "목표 분량(자)", "number", min=0),
        F("current_count", "시작 시점 분량(자)", "number", min=0), F("deadline", "마감", "date"),
        F("schedule", "연재 주기"), F("status", "상태", "select", options=["집필 중", "완결", "보류"], default="집필 중"),
        F("characters", "캐릭터 설정", "textarea"), F("world", "세계관", "textarea")]},
    "writing_logs": {"label": "집필 기록", "agent": "writing", "fields": [
        F("writing_id", "작품", "ref", ref="writings", required=True),
        F("date", "날짜", "date", required=True), F("count", "쓴 분량(자)", "number", min=0), F("note", "메모")]},
    "projects": {"label": "프로젝트", "agent": "project", "fields": [
        F("title", "프로젝트", required=True), F("description", "설명", "textarea"), F("goal", "목표"),
        F("deadline", "마감", "date"),
        F("status", "상태", "select", options=["진행", "완료", "보류"], default="진행"),
        F("progress", "진행률(자동 계산)", "number", readonly=True)]},
    "project_tasks": {"label": "프로젝트 하위 작업", "agent": "project", "fields": [
        F("project_id", "프로젝트", "ref", ref="projects", required=True),
        F("title", "작업", required=True), F("due", "기한", "date"),
        F("done", "완료", "bool"), F("milestone", "마일스톤", "bool")]},
    "expenses": {"label": "가계부", "agent": "finance", "fields": [
        F("date", "날짜", "date", required=True), F("amount", "금액", "number", min=0, required=True),
        F("kind", "구분", "select", options=["지출", "수입"], default="지출"),
        F("category", "분류", "select",
          options=["식비", "교통", "주거", "통신", "학습", "문화", "쇼핑", "의료", "저축", "기타"], default="기타"),
        F("memo", "메모")]},
    "budgets": {"label": "월 예산", "agent": "finance", "fields": [
        F("category", "분류", "select",
          options=["식비", "교통", "주거", "통신", "학습", "문화", "쇼핑", "의료", "저축", "기타"], required=True),
        F("limit", "한도", "number", min=0, required=True)]},
    "holdings": {"label": "보유 종목", "agent": "invest", "fields": [
        F("name", "종목", required=True), F("ticker", "티커"),
        F("quantity", "수량", "number", min=0, required=True),
        F("avg_price", "평균 매수가", "number", min=0, required=True),
        F("current_price", "현재가(직접 입력)", "number", min=0), F("note", "메모")]},
    "invest_notes": {"label": "투자 일지", "agent": "invest", "fields": [
        F("date", "날짜", "date", required=True), F("title", "제목", required=True),
        F("content", "내용", "textarea")]},
    "ideas": {"label": "아이디어", "agent": "creative", "fields": [
        F("title", "아이디어", required=True), F("content", "내용", "textarea"),
        F("tags", "태그(쉼표로 구분)"), F("deadline", "공모전·제출 마감", "date"),
        F("status", "상태", "select", options=["메모", "발전 중", "완료"], default="메모")]},
    "documents": {"label": "문서", "agent": "docs", "fields": [
        F("title", "제목", required=True),
        F("type", "종류", "select", options=["일반", "일일 업무 보고서", "주간 업무 보고서", "학습 계획서",
                                          "프로젝트 진행 보고서", "집필 진도 보고서", "가계부 요약", "투자 기록",
                                          "일정표", "회의록", "업로드 자료", "AI 초안"], default="일반"),
        F("status", "상태", "select", options=["초안", "완료"], default="초안"),
        F("due_date", "마감", "date"), F("content", "내용", "textarea", max=200000)]},
    "memories": {"label": "기억", "agent": "chief", "fields": [
        F("agent_id", "직원", "select", options=AGENT_IDS + ["all"], default="all"),
        F("scope", "범위", "select", options=["단기", "프로젝트", "장기"], default="장기"),
        F("project_id", "프로젝트", "ref", ref="projects"),
        F("kind", "종류", "select", options=["확정 사실", "AI 가설", "실행 결과"], default="확정 사실"),
        F("content", "내용", "textarea", required=True), F("tags", "태그(쉼표로 구분, 프로젝트 규칙은 '규칙' 포함)"),
        F("key", "주제 키 (같은 키는 가장 최근 확정 지시가 우선)"),
        F("source", "출처", default="사용자 입력"),
        F("confidence", "신뢰도(0~1)", "number", min=0, max=1, default=1),
        F("expires_at", "만료일", "date"),
        F("sensitivity", "민감도", "select", options=["일반", "민감"], default="일반")]},
    "rules": {"label": "반복 업무 규칙", "agent": "system", "fields": [
        F("title", "규칙 이름", required=True),
        F("action", "실행할 업무", "select", options=[], required=True),
        F("every_days", "주기(일)", "number", min=1, max=365, default=7),
        F("enabled", "사용", "bool", default=True),
        F("run_when_closed", "서버 예약 실행 (브라우저를 닫아도 서버에서 실행)", "bool", default=False),
        F("last_executed_at", "마지막 실행", readonly=True)]},
    "reports": {"label": "직원 보고", "agent": "chief", "internal": True, "fields": []},
    "meetings": {"label": "회의", "agent": "chief", "internal": True, "fields": []},
}

ID_RE = re.compile(r"^[A-Za-z0-9_\-]{6,40}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Invalid(ValueError):
    pass


def validate(name: str, data: dict, existing: dict | None = None, refs: dict | None = None) -> dict:
    """입력값을 정의에 맞춰 검증·정규화한다. 정의에 없는 필드는 버린다."""
    spec = COLLECTIONS.get(name)
    if not spec or spec.get("internal"):
        raise Invalid("알 수 없는 데이터 종류입니다.")
    out = dict(existing or {})
    for f in spec["fields"]:
        k = f["k"]
        if f.get("readonly"):
            continue
        if k not in data:
            if existing is None:
                if f.get("required"):
                    raise Invalid(f"'{f['label']}' 항목이 필요합니다.")
                if "default" in f:
                    out[k] = f["default"]
            continue
        v = data[k]
        t = f["type"]
        if v in (None, ""):
            if f.get("required"):
                raise Invalid(f"'{f['label']}' 항목이 필요합니다.")
            out[k] = "" if t not in ("number", "bool") else (False if t == "bool" else None)
            continue
        if t in ("text", "textarea"):
            v = str(v)
            limit = f.get("max", 300 if t == "text" else 20000)
            if len(v) > limit:
                raise Invalid(f"'{f['label']}' 항목이 너무 깁니다 (최대 {limit}자).")
            if t == "text":
                v = v.replace("\n", " ").strip()
        elif t == "date":
            if not valid_date(v):
                raise Invalid(f"'{f['label']}' 날짜 형식이 올바르지 않습니다.")
            v = str(v)
        elif t == "time":
            if not TIME_RE.match(str(v)):
                raise Invalid(f"'{f['label']}' 시간 형식이 올바르지 않습니다 (HH:MM).")
            v = str(v)
        elif t == "number":
            try:
                v = float(v)
            except Exception:
                raise Invalid(f"'{f['label']}' 항목은 숫자여야 합니다.")
            if v != v or v in (float("inf"), float("-inf")):
                raise Invalid(f"'{f['label']}' 값이 올바르지 않습니다.")
            if "min" in f and v < f["min"] or "max" in f and v > f["max"]:
                raise Invalid(f"'{f['label']}' 값이 허용 범위를 벗어났습니다.")
            if v == int(v):
                v = int(v)
        elif t == "bool":
            v = bool(v)
        elif t == "select":
            if f["options"] and v not in f["options"]:
                raise Invalid(f"'{f['label']}' 선택값이 올바르지 않습니다.")
        elif t == "ref":
            if refs is not None and v not in refs.get(f["ref"], {}):
                raise Invalid(f"'{f['label']}' 대상이 존재하지 않습니다.")
            v = str(v)
        out[k] = v
    if name == "events" and out.get("start") and out.get("end") and out["end"] <= out["start"]:
        raise Invalid("종료 시간은 시작 시간보다 늦어야 합니다.")
    return out
