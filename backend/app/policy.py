"""정책 엔진 — 모든 자동 실행의 권한 판단을 한곳에서 한다.

판단 근거는 서버에 저장된 값(직원 권한, 승인 기록, 도구 위험도)뿐이다. AI가 쓴 문장은 승인 근거가 될 수 없다.

- 자동 실행: 내부 데이터 검색·요약·분류·위험 감지·후보 제안·내부 초안·검수 (위험도 LOW)
- 범위 승인: 일정 변경, 기존 문서 수정, 프로젝트 작업 추가·재배치, 반복 자동화 활성화 (MEDIUM)
           → 한 번의 계획 승인으로 그 워크플로 안의 MEDIUM 단계를 허용
- 개별 승인: 영구 삭제, 외부 발송·공유·공개 게시 (HIGH) → 단계마다 승인
- 거부: 결제·송금·투자 주문 등 금전 실행 (기능 자체를 제공하지 않음)
"""
from __future__ import annotations

ACTION_TYPES = {
    "event.create": "schedule.change", "event.update": "schedule.change", "task.create": "project.modify",
    "doc.create": "document.create", "file.modify": "document.modify", "record.delete": "data.delete_permanent",
    "intake.apply": "intake.apply",
}
DENIED = {"payment", "money.transfer", "invest.order"}
INDIVIDUAL = {"data.delete_permanent", "external.send", "external.share", "public.post"}


def action_type(tool: dict) -> str:
    return ACTION_TYPES.get(tool["name"], "internal.read" if tool["risk"] == "LOW" else "internal.write")


def decide(s: dict, agent_id: str, tool: dict, approved_step: bool = False, scope_approved: bool = False,
           sensitivity: str = "일반") -> dict:
    """반환: decision ∈ allow | scoped_approval | individual_approval | blocked | deny"""
    act = action_type(tool)
    agent = s["agents"].get(agent_id)
    base = {"action_type": act, "risk": tool["risk"], "agent_id": agent_id, "tool": tool["name"]}
    if not agent:
        return {**base, "decision": "deny", "reason": "알 수 없는 직원"}
    if act in DENIED:
        return {**base, "decision": "deny", "reason": "금전 실행은 LIFE OS가 제공하지 않는 기능"}
    missing = [w for w in tool["writes"] if w not in agent["permissions"]["write"]]
    if missing:
        return {**base, "decision": "blocked", "missing": missing,
                "reason": f"{agent['name']}에게 {', '.join(missing)} 쓰기 권한이 없음"}
    if tool["risk"] == "HIGH" or act in INDIVIDUAL:
        if approved_step:
            return {**base, "decision": "allow", "reason": "이 단계에 대한 사용자 개별 승인 있음"}
        return {**base, "decision": "individual_approval", "reason": "위험도 HIGH — 단계마다 사용자 승인 필요"}
    if tool["risk"] == "MEDIUM" or sensitivity == "민감":
        if approved_step:
            return {**base, "decision": "allow", "reason": "사용자가 이 단계를 승인(또는 대안을 선택)함"}
        if scope_approved:
            return {**base, "decision": "allow", "reason": "사용자가 승인한 계획 범위 안"}
        if agent["permissions"].get("auto_medium") and sensitivity != "민감":
            return {**base, "decision": "allow", "reason": f"{agent['name']}의 '중간 위험 자동 실행' 권한(사용자가 설정)"}
        return {**base, "decision": "scoped_approval", "reason": "위험도 MEDIUM — 계획 승인 필요"}
    return {**base, "decision": "allow", "reason": "내부 조회·분석 (자동 실행 허용)"}
