"""결과물 검수 서비스 — '직원이 끝냈다고 한 것'과 '실제로 끝난 것'을 구분해 기록한다.

검수 기록(verification run)은 상태 파일의 verifications 에 남는다. 검수 센터에서 같은 결과물을 다시 검수할 수 있다.
"""
from __future__ import annotations

from . import analysis as A
from .tools import C, TCtx, _norm, check_file
from .util import iso, uid, valid_date

VERSION = "v3.0"
ARTIFACT_OF_TOOL = {"doc.create": "document", "event.create": "event", "event.update": "event",
                    "task.create": "project_task", "research.collect": "research", "web.research": "research",
                    "quality.check": "document", "file.modify": "document", "record.delete": "deletion",
                    "intake.apply": "intake"}


def artifact_ids(tool: str, out: dict) -> list:
    if tool == "doc.create":
        return [out["doc_id"]]
    if tool in ("event.create", "task.create"):
        return out.get("created") or out.get("existing") or []
    if tool == "event.update":
        return [m["id"] for m in out.get("moved", [])]
    if tool == "quality.check":
        return [out.get("file_id")]
    if tool == "file.modify":
        return [out["doc_id"]]
    return [out.get("id") or ""]


def record(s: dict, now, artifact_type: str, artifact_id: str, checks: list, agent: str = "", workflow_id: str = "",
           step_id: str = "", title: str = "", source: str = "workflow") -> dict:
    run = {"id": uid("vr_"), "artifact_type": artifact_type, "artifact_id": artifact_id, "title": title[:120],
           "verifier_version": VERSION, "status": "passed" if all(c["passed"] for c in checks) else "failed",
           "checks": [{"check_type": c["name"], "passed": c["passed"], "evidence": c.get("detail", "") if c["passed"] else "",
                       "error_message": "" if c["passed"] else c.get("detail") or c["name"]} for c in checks],
           "agent_id": agent, "workflow_id": workflow_id, "step_id": step_id, "source": source,
           "fixable": artifact_type in ("document", "event", "project_task"),
           "completed_at": iso(now), "updated_at": iso(now)}
    vs = s["verifications"]
    vs[run["id"]] = run
    if len(vs) > 400:
        for k in sorted(vs, key=lambda k: vs[k]["completed_at"])[:100]:
            del vs[k]
    return run


def verify_artifact(eng, artifact_type: str, artifact_id: str, now) -> dict:
    """저장된 결과물을 지금 상태 그대로 다시 검수한다 (재검수)."""
    s = eng.s
    ctx = TCtx(eng, {"id": "recheck"}, {"id": "recheck", "agent": "system", "scratch": {}}, now)
    if artifact_type == "document":
        d = s["c"].get("documents", {}).get(artifact_id)
        if not d:
            checks = [C("문서 레코드 존재", False, "삭제되었거나 없는 문서")]
            title = artifact_id
        else:
            title = d["title"]
            files = [f for f in s["files"].values() if f["doc_id"] == artifact_id]
            checks = [C("문서 레코드 존재", True, f"v{d.get('version', 1)}"),
                      C("본문 있음", bool(str(d.get("content", "")).strip()), f"{len(str(d.get('content', '')))}자")]
            latest = {}
            for f in sorted(files, key=lambda f: f["created_at"]):
                latest[f["fmt"]] = f
            for f in latest.values():
                checks += check_file(ctx, f, f.get("sha256"), [d["title"]], require_notes=f["fmt"] == "pptx")
            if not files:
                checks.append(C("생성된 파일", True, "파일 없이 저장된 문서 (내보내기 시 생성)"))
    elif artifact_type == "event":
        e = s["c"].get("events", {}).get(artifact_id)
        title = (e or {}).get("title", artifact_id)
        if not e:
            checks = [C("DB 저장 확인", False, "삭제되었거나 없는 일정")]
        else:
            ok_time = not (e.get("start") and e.get("end")) or A.tmin(e["end"]) > A.tmin(e["start"])
            dups = [o for o in A.V(s, "events") if o["id"] != e["id"] and _norm(o.get("title")) == _norm(e.get("title"))
                    and o.get("date") == e.get("date") and o.get("start") == e.get("start")]
            clash = [o["title"] for o in A.day_events(s, e["date"]) if o["id"] != e["id"] and e.get("start") and e.get("end")
                     and A.tmin(o.get("start")) is not None and A.tmin(o.get("end")) is not None
                     and A.tmin(e["start"]) < A.tmin(o["end"]) and A.tmin(o["start"]) < A.tmin(e["end"])]
            checks = [C("DB 저장 확인", True), C("날짜 유효성", valid_date(e.get("date")) and ok_time, f"{e.get('date')} {e.get('start', '')}~{e.get('end', '')}"),
                      C("중복 일정 없음", not dups, ", ".join(d["id"] for d in dups)),
                      C("시간 충돌 없음", not clash, ", ".join(clash))]
    elif artifact_type == "project_task":
        t = s["c"].get("project_tasks", {}).get(artifact_id)
        title = (t or {}).get("title", artifact_id)
        if not t:
            checks = [C("DB 저장 확인", False, "삭제되었거나 없는 할 일")]
        else:
            same = [o for o in A.V(s, "project_tasks") if o["id"] != t["id"] and o.get("project_id") == t.get("project_id")
                    and _norm(o["title"]) == _norm(t["title"])]
            checks = [C("DB 저장 확인", True), C("프로젝트 연결", t.get("project_id") in s["c"].get("projects", {})),
                      C("중복 할 일 없음", not same), C("기한 유효성", not t.get("due") or valid_date(t["due"]), t.get("due") or "기한 없음")]
    else:
        raise ValueError("다시 검수할 수 없는 결과물 종류")
    return record(s, now, artifact_type, artifact_id, checks, "system", title=title, source="recheck")
