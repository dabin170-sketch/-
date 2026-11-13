"""문서 공장: 실제 데이터로 문서 본문(마크다운)을 만들고 DOCX / HTML / MD 파일로 내보낸다."""
from __future__ import annotations

import html
import io
import re

from . import analysis as A
from .util import dadd, today

DOC_TYPES = {
    "daily_report": "일일 업무 보고서", "weekly_report": "주간 업무 보고서", "study_plan": "학습 계획서",
    "project_report": "프로젝트 진행 보고서", "writing_report": "집필 진도 보고서",
    "ledger_summary": "가계부 요약", "invest_log": "투자 기록", "timetable": "일정표", "minutes": "회의록",
}
EXPORT_FORMATS = {"docx": "Word 문서(.docx)", "md": "마크다운(.md)", "html": "웹 문서(.html, 인쇄하여 PDF 저장 가능)"}


def tbl(headers: list, rows: list) -> list:
    if not rows:
        return ["(해당 자료 없음)"]
    esc = lambda v: str(v).replace("|", "/").replace("\n", " ")
    return ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers),
            *["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]]


def _tasks_between(s: dict, d1: str, d2: str) -> list:
    from .util import iso, local, parse
    out = []
    for t in s["tasks"].values():
        if t["status"] == "DONE" and t.get("completed_at"):
            d = local(s, parse(t["completed_at"])).date().isoformat()
            if d1 <= d <= d2:
                out.append((d, t))
    return sorted(out, key=lambda x: x[1]["completed_at"])


def build(s: dict, now, doc_type: str, params: dict) -> tuple[str, str, list]:
    """(제목, 본문 마크다운, 사용한 자료 목록)을 돌려준다."""
    td = today(s, now)
    name = lambda aid: s["agents"].get(aid, {}).get("name", aid)
    L: list[str] = []
    used: list[str] = []
    if doc_type in ("daily_report", "weekly_report"):
        if doc_type == "daily_report":
            d1 = d2 = params.get("date") or td
            title = f"일일 업무 보고서 ({d1})"
        else:
            d2 = params.get("end") or td
            d1 = dadd(d2, -6)
            title = f"주간 업무 보고서 ({d1} ~ {d2})"
        done = _tasks_between(s, d1, d2)
        failed = [t for t in s["tasks"].values() if t["status"] == "FAILED" and d1 <= t["updated_at"][:10] <= d2]
        L += ["## 요약", f"- 완료한 업무 {len(done)}건, 실패 {len(failed)}건",
              f"- 승인 대기 {sum(1 for a in s['approvals'].values() if a['status'] == 'PENDING')}건", "",
              "## 완료한 업무",
              *tbl(["날짜", "담당", "업무", "결과"],
                   [[d, name(t["assigned_agent"]), t["title"], (t.get("result") or {}).get("summary", "")]
                    for d, t in done]), ""]
        if failed:
            L += ["## 실패한 업무", *tbl(["담당", "업무", "오류"],
                                      [[name(t["assigned_agent"]), t["title"], t.get("error", "")] for t in failed]), ""]
        sch = A.a_schedule(s, now)
        L += ["## 일정 현황", *[f"- {x}" for x in sch["lines"]]]
        used = [f"작업 기록 {len(done) + len(failed)}건", "일정"]
    elif doc_type == "study_plan":
        st = A.a_study(s, now)
        title = f"학습 계획서 ({td})"
        L += ["## 과목별 현황",
              *tbl(["과목", "진도", "남은 분량", "마지막 기록", "시험", "하루 필요 분량"],
                   [[x["title"], f"{x['pct']}%" if x["pct"] is not None else "-", f"{x['remaining']:g}{x['unit']}",
                     x["last"] or "없음", f"D-{x['dday']}" if x["dday"] is not None else "-",
                     f"{x['need_per_day']:g}{x['unit']}" if x["need_per_day"] else "-"] for x in st["subjects"]]), "",
              "## 복습이 필요한 과목",
              *([f"- {x['title']}: 마지막 기록 {x['last'] or '없음'}, 복습 주기 {x['interval']}일" for x in st["due"]]
                or ["- 없음"]), "",
              "## 앞으로 7일 계획",
              *tbl(["날짜", "할 일"], [[dadd(td, k), " / ".join(
                  [f"{x['title']} {x['need_per_day']:g}{x['unit']}" for x in st["subjects"] if x["need_per_day"]]
                  + [f"{x['title']} 복습" for x in st["due"] if k % max(1, x["interval"]) == 0]) or "-"]
                  for k in range(7)]),
              "", "※ 위 분량은 남은 분량을 남은 일수로 나눈 계산값이며 결과를 보장하는 예측이 아닙니다."]
        used = ["학습 과목", "학습 기록"]
    elif doc_type == "project_report":
        pr = A.a_project(s, now)
        title = f"프로젝트 진행 보고서 ({td})"
        for x in pr["items"]:
            L += [f"## {x['title']}", f"- 진행률: {x['pct']}% ({x['done']}/{x['total']})",
                  f"- 마감: {x['deadline'] or '미정'}" + (f" (D-{x['dday']})" if x["dday"] is not None else ""),
                  f"- 다음 마일스톤: {x['next_milestone']['title']} ({x['next_milestone']['due']})"
                  if x["next_milestone"] else "- 다음 마일스톤: 없음",
                  "- 지연 작업:", *([f"  - {d['title']} (기한 {d['due']})" for d in x["delayed"]] or ["  - 없음"]), ""]
        if not pr["items"]:
            L.append("진행 중인 프로젝트가 없습니다.")
        used = ["프로젝트", "프로젝트 하위 작업"]
    elif doc_type == "writing_report":
        wr = A.a_writing(s, now)
        title = f"집필 진도 보고서 ({td})"
        L += [*tbl(["작품", "현재 분량", "목표", "진행률", "최근 7일", "마감", "하루 필요"],
                   [[x["title"], f"{int(x['current']):,}자", f"{int(x['target']):,}자" if x["target"] else "-",
                     f"{x['pct']}%" if x["pct"] is not None else "-", f"{int(x['last7']):,}자",
                     x["deadline"] or "-", f"{x['need_per_day']:,}자" if x["need_per_day"] else "-"]
                    for x in wr["items"]]), "",
              "## 진도가 부족한 작품", *([f"- {x['title']}" for x in wr["behind"]] or ["- 없음"])]
        used = ["집필 프로젝트", "집필 기록"]
    elif doc_type == "ledger_summary":
        fi = A.a_finance(s, now)
        title = f"가계부 요약 ({fi['month']})"
        L += ["## 합계", f"- 지출 {int(fi['total']):,}원", f"- 수입 {int(fi['income']):,}원", "",
              "## 분류별 지출", *tbl(["분류", "금액"], [[c, f"{int(v):,}원"] for c, v in
                                                  sorted(fi["spend"].items(), key=lambda x: -x[1])]), "",
              "## 예산 대비", *tbl(["분류", "사용", "한도", "사용률", "월말 예상", "상태"],
                                [[b["category"], f"{int(b['used']):,}", f"{int(b['limit']):,}", f"{b['pct']}%",
                                  f"{int(b['projected']):,}", b["state"]] for b in fi["budgets"]]),
              "", "※ 월말 예상은 지금까지의 하루 평균 지출을 그대로 연장한 단순 계산입니다."]
        used = ["가계부", "월 예산"]
    elif doc_type == "invest_log":
        iv = A.a_invest(s, now)
        title = f"투자 기록 ({td})"
        L += [*tbl(["종목", "수량", "매수 원가", "평가액", "손익", "수익률", "비중"],
                   [[r["name"], f"{r['quantity']:g}", f"{r['cost']:,.0f}",
                     f"{r['value']:,.0f}" if r["value"] is not None else "현재가 미입력",
                     f"{r['pnl']:+,.0f}" if r["pnl"] is not None else "-",
                     f"{r['pnl_pct']:+}%" if r["pnl_pct"] is not None else "-", f"{r['weight']}%"]
                    for r in iv["rows"]]), "",
              "## 투자 일지", *([f"- {n['date']} {n['title']}: {str(n.get('content', ''))[:200]}" for n in
                              sorted(A.V(s, 'invest_notes'), key=lambda n: n['date'])[-10:]] or ["- 기록 없음"]),
              "", "※ 실시간 시세·공시 데이터는 연결되어 있지 않습니다. 직접 입력한 값으로만 계산했으며 투자 권유가 아닙니다."]
        used = ["보유 종목", "투자 일지"]
    elif doc_type == "timetable":
        title = f"일정표 ({td} ~ {dadd(td, 6)})"
        for k in range(7):
            d = dadd(td, k)
            evs = A.day_events(s, d)
            L += [f"## {d}", *tbl(["시간", "일정", "종류", "완료"],
                                  [[f"{e.get('start') or ''}~{e.get('end') or ''}" if e.get("start") else "종일",
                                    e["title"], e.get("type", "일정"), "✔" if e.get("done") else ""] for e in evs]), ""]
        used = ["일정"]
    elif doc_type == "minutes":
        m = s["c"].get("meetings", {}).get(params.get("meeting_id", ""))
        if not m:
            raise ValueError("회의를 찾을 수 없습니다.")
        title = f"회의록 — {m['title']}"
        L += [f"- 일시: {m['created_at']}", f"- 형식: {m['mode_label']}",
              f"- 참석: {', '.join(name(p) for p in m['participants'])}",
              f"- 사용한 자료: {', '.join(m['sources'])}", "", "## 직원별 의견"]
        for t in m["transcript"]:
            L += [f"### {name(t['agent'])} ({'AI 생성' if t['mode'] == 'ai' else '규칙 기반'})",
                  *[f"- {x}" for x in t["lines"]], ""]
        L += ["## 결정 사항", *([f"- {x}" for x in m["conclusions"]] or ["- 없음"]), "",
              "## 후속 업무", *([f"- {x['title']}" for x in m["action_items"]] or ["- 없음"]), "",
              "## 승인 필요 항목", *([f"- {x}" for x in m.get("needs_approval", [])] or ["- 없음"])]
        used = ["회의 기록"]
    else:
        raise ValueError("지원하지 않는 문서 종류입니다.")
    return title, "\n".join(L), used


# ------------------------------------------------------------------ 내보내기
def _blocks(md: str):
    """마크다운을 (종류, 내용) 블록으로 나눈다."""
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"-+", c) for c in cells):
                    rows.append(cells)
                i += 1
            yield "table", rows
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", ln)
        if m:
            yield f"h{len(m.group(1))}", m.group(2)
        elif re.match(r"^\s*-\s+", ln):
            yield "li", re.sub(r"^\s*-\s+", "", ln)
        elif ln.strip():
            yield "p", ln
        i += 1


def to_docx(title: str, md: str) -> bytes:
    from docx import Document
    from docx.oxml.ns import qn
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Malgun Gothic"
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "맑은 고딕")
    doc.add_heading(title, 0)
    for kind, val in _blocks(md):
        if kind == "table":
            width = max(len(r) for r in val)
            t = doc.add_table(rows=len(val), cols=width)
            t.style = "Table Grid"
            for r, row in enumerate(val):
                for c, cell in enumerate(row):
                    t.cell(r, c).text = cell
                    if r == 0:
                        for p in t.cell(r, c).paragraphs:
                            for run in p.runs:
                                run.bold = True
        elif kind.startswith("h"):
            doc.add_heading(val, int(kind[1]))
        elif kind == "li":
            doc.add_paragraph(val, style="List Bullet")
        else:
            doc.add_paragraph(val)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def to_html(title: str, md: str) -> bytes:
    e = html.escape
    out = ["<!doctype html><html lang='ko'><head><meta charset='utf-8'>",
           f"<title>{e(title)}</title><style>body{{font-family:'Malgun Gothic',sans-serif;max-width:800px;"
           "margin:40px auto;padding:0 16px;line-height:1.6}table{border-collapse:collapse;width:100%}"
           "td,th{border:1px solid #999;padding:4px 8px;text-align:left}th{background:#f3efe6}</style></head><body>",
           f"<h1>{e(title)}</h1>"]
    in_ul = False
    for kind, val in _blocks(md):
        if kind != "li" and in_ul:
            out.append("</ul>")
            in_ul = False
        if kind == "table":
            out.append("<table>" + "".join(
                "<tr>" + "".join(f"<{'th' if r == 0 else 'td'}>{e(c)}</{'th' if r == 0 else 'td'}>" for c in row) + "</tr>"
                for r, row in enumerate(val)) + "</table>")
        elif kind.startswith("h"):
            out.append(f"<h{int(kind[1]) + 1}>{e(val)}</h{int(kind[1]) + 1}>")
        elif kind == "li":
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{e(val)}</li>")
        else:
            out.append(f"<p>{e(val)}</p>")
    if in_ul:
        out.append("</ul>")
    out.append("</body></html>")
    return "".join(out).encode()


def export(title: str, md: str, fmt: str) -> tuple[bytes, str, str]:
    if fmt == "docx":
        return to_docx(title, md), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
    if fmt == "html":
        return to_html(title, md), "text/html; charset=utf-8", "html"
    if fmt == "md":
        return f"# {title}\n\n{md}\n".encode(), "text/markdown; charset=utf-8", "md"
    raise ValueError("지원하지 않는 형식입니다.")
