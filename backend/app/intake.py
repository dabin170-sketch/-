"""만능 AI 수집함 (IngestionService).

검증 → 형식 식별 → 기본 위험 검사 → 내용 추출 → 메타데이터 → 중복 확인 → 분류 → 관련 프로젝트 → 후속 작업 후보 → 승인 요청.
분석·분류는 자동, 일정·할 일에 반영하는 것은 사용자가 후보를 승인할 때만 한다.

지원: 텍스트(.txt .md .csv), PDF(글자가 있는 PDF), Word(.docx), 붙여 넣은 글
미지원(이유 표시): 이미지(OCR 엔진 없음), 음성(음성 인식기 없음), 웹 주소 본문 가져오기(보안·무료 한도)
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import date

from . import analysis as A
from .util import dadd, ddiff, iso, short_hash, today, uid

MAX_BYTES = 3_000_000
TEXT_EXT = {"txt", "md", "csv"}
SUPPORTED = TEXT_EXT | {"pdf", "docx"}
IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
AUDIO_EXT = {"mp3", "wav", "m4a", "ogg"}
MAGIC = {"pdf": [b"%PDF"], "docx": [b"PK"], "png": [b"\x89PNG"], "jpg": [b"\xff\xd8"], "jpeg": [b"\xff\xd8"],
         "gif": [b"GIF8"], "webp": [b"RIFF"], "mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"], "wav": [b"RIFF"],
         "ogg": [b"OggS"], "m4a": [b""]}
CATEGORIES = {
    "과제": ["과제", "레포트", "리포트", "제출", "보고서", "assignment"],
    "시험": ["시험", "중간고사", "기말고사", "퀴즈", "평가", "임용", "출제"],
    "발표": ["발표", "프레젠테이션", "ppt", "발표회"],
    "공모전": ["공모전", "공모", "응모", "접수", "시상", "상금"],
    "재무": ["영수증", "결제", "합계", "청구", "금액", "카드"],
    "일정": ["회의", "모임", "일시", "장소", "약속", "행사"],
    "학습자료": ["강의", "개념", "단원", "챕터", "정리", "요약"],
}
DEADLINE_WORDS = ["마감", "제출", "까지", "기한", "due", "deadline", "접수", "시험일", "발표일", "응시"]


class IntakeError(ValueError):
    pass


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def inspect(name: str, data: bytes) -> tuple[str, str]:
    """(파일 종류, 경고) — 확장자와 실제 내용(시그니처)이 맞는지, 위험 요소가 있는지 본다. 백신을 대신하지는 않는다."""
    ext = ext_of(name)
    if len(data) > MAX_BYTES:
        raise IntakeError(f"파일이 너무 큽니다 (최대 {MAX_BYTES // 1_000_000}MB).")
    if not data:
        raise IntakeError("빈 파일입니다.")
    if data[:2] == b"MZ" or data[:4] == b"\x7fELF" or ext in ("exe", "bat", "cmd", "sh", "js", "vbs", "ps1", "docm", "xlsm", "jar"):
        raise IntakeError("실행 파일·매크로 파일은 받을 수 없습니다.")
    if ext not in SUPPORTED | IMAGE_EXT | AUDIO_EXT:
        raise IntakeError(f"지원하지 않는 형식(.{ext or '확장자 없음'})입니다. 텍스트·PDF·Word 파일을 올리거나 내용을 붙여 넣어 주세요.")
    if ext in TEXT_EXT:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise IntakeError("UTF-8 텍스트가 아닙니다.")
        if "\x00" in text:
            raise IntakeError("텍스트 파일이 아닙니다.")
        return "text", ""
    sigs = MAGIC.get(ext, [])
    if sigs and not any(data.startswith(s) for s in sigs):
        raise IntakeError(f".{ext} 확장자인데 실제 내용이 그 형식이 아닙니다 (위장 파일 의심).")
    if ext == "pdf":
        head = data[:400_000]
        if any(k in head for k in (b"/JavaScript", b"/Launch", b"/EmbeddedFile")):
            raise IntakeError("PDF 안에 스크립트·실행·첨부 파일 요소가 있어 받지 않았습니다.")
        return "pdf", ""
    if ext == "docx":
        if b"vbaProject.bin" in data:
            raise IntakeError("매크로가 들어 있는 Word 파일은 받을 수 없습니다.")
        return "docx", ""
    return ("image" if ext in IMAGE_EXT else "audio"), ""


def extract_text(kind: str, data: bytes) -> str:
    if kind == "text":
        return data.decode("utf-8")
    if kind == "pdf":
        from pypdf import PdfReader
        try:
            r = PdfReader(io.BytesIO(data))
            if r.is_encrypted:
                raise IntakeError("암호가 걸린 PDF는 읽을 수 없습니다.")
            text = "\n".join((p.extract_text() or "") for p in r.pages[:60])
        except IntakeError:
            raise
        except Exception as e:
            raise IntakeError(f"PDF를 읽을 수 없습니다 (손상된 파일일 수 있음): {type(e).__name__}")
        if not text.strip():
            raise IntakeError("PDF에 글자가 없습니다 (스캔 이미지 PDF는 OCR이 필요해 지원하지 않음).")
        return text
    if kind == "docx":
        from docx import Document
        try:
            d = Document(io.BytesIO(data))
        except Exception:
            raise IntakeError("Word 파일을 열 수 없습니다 (손상된 파일일 수 있음).")
        return "\n".join([p.text for p in d.paragraphs] + [c.text for t in d.tables for r in t.rows for c in r.cells])
    raise IntakeError("이 형식에서는 글자를 뽑을 수 없습니다.")


# ------------------------------------------------------------------ 분석
DATE_RES = [
    (re.compile(r"(?P<y>20\d{2})\s*[-./년]\s*(?P<m>\d{1,2})\s*[-./월]\s*(?P<d>\d{1,2})\s*일?"), True),
    (re.compile(r"(?<![\d.])(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일"), False),
    (re.compile(r"(?<![\d./])(?P<m>1[0-2]|0?[1-9])/(?P<d>3[01]|[12]\d|0?[1-9])(?![\d/])"), False),
]
TIME_RE = re.compile(r"(?:(오전|오후)\s*)?(\d{1,2})\s*(?::(\d{2})|시\s*(\d{1,2})?\s*분?)")


def extract_deadlines(text: str, td: str) -> list:
    out, seen = [], set()
    t0 = date.fromisoformat(td)
    for ln_no, line in enumerate(text.split("\n"), 1):
        low = line.lower()
        kw = [w for w in DEADLINE_WORDS if w in low]
        for rx, has_year in DATE_RES:
            for m in rx.finditer(line):
                try:
                    y = int(m.group("y")) if has_year else t0.year
                    d = date(y, int(m.group("m")), int(m.group("d")))
                    if not has_year and (t0 - d).days > 30:
                        d = date(y + 1, d.month, d.day)
                except ValueError:
                    continue
                iso_d = d.isoformat()
                tm = TIME_RE.search(line[m.end(): m.end() + 20])
                hhmm = None
                if tm and (tm.group(3) or "시" in tm.group(0)):
                    h = int(tm.group(2)) + (12 if tm.group(1) == "오후" and int(tm.group(2)) < 12 else 0)
                    mi = int(tm.group(3) or tm.group(4) or 0)
                    if h < 24 and mi < 60:
                        hhmm = f"{h:02d}:{mi:02d}"
                conf = 0.5 + (0.25 if has_year else 0) + (0.2 if kw else 0) - (0.3 if d < t0 else 0)
                key = (iso_d, hhmm)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"extraction_type": "deadline" if kw else "date", "value": iso_d, "time": hhmm,
                            "label": kw[0] if kw else "", "confidence": round(max(0.1, min(0.95, conf)), 2),
                            "evidence_location": f"{ln_no}번째 줄", "snippet": line.strip()[:120], "past": d < t0})
    return out


def classify(text: str, name: str = "") -> dict:
    low = (name + "\n" + text).lower()
    scores = {c: sum(low.count(w) for w in ws) for c, ws in CATEGORIES.items()}
    total = sum(scores.values())
    if not total:
        return {"category": "기타", "confidence": 0.3, "reasons": ["분류 단서가 되는 낱말이 없음"], "scores": scores}
    best = max(scores, key=scores.get)
    hits = [w for w in CATEGORIES[best] if w in low]
    return {"category": best, "confidence": round(min(0.95, 0.4 + 0.6 * scores[best] / total), 2),
            "reasons": [f"'{w}' 포함" for w in hits[:4]], "scores": scores}


def related(s: dict, text: str) -> list:
    """프로젝트·학습 과목·집필 작품 중 본문과 관련 있는 것."""
    low = re.sub(r"\s+", "", text.lower())
    rows = []
    for coll, label in (("projects", "프로젝트"), ("subjects", "학습 과목"), ("writings", "집필 작품")):
        for r in A.V(s, coll):
            title = re.sub(r"\s+", "", str(r.get("title", "")).lower())
            if not title:
                continue
            if title in low:
                score, why = 1.0, f"제목 「{r['title']}」이(가) 그대로 나옴"
            else:
                words = [w for w in re.findall(r"[가-힣a-z0-9]{2,}", str(r["title"]).lower()) if w in low]
                score = round(sum(len(w) for w in words) / max(1, len(title)), 2)
                why = "공통 낱말: " + ", ".join(words) if words else ""
            if score >= 0.4:
                rows.append({"collection": coll, "type": label, "id": r["id"], "title": r["title"], "score": score,
                             "evidence": why})
    return sorted(rows, key=lambda x: -x["score"])[:3]


def title_of(text: str, name: str) -> str:
    for ln in text.split("\n"):
        t = ln.strip(" #*-\t")
        if 2 <= len(t) <= 40 and not re.fullmatch(r"[\d\s./:-]+", t):
            return t
    return re.sub(r"\.[a-z0-9]+$", "", name)[:40] or "수집 항목"


def propose(s: dict, item: dict) -> list:
    """후속 작업 후보 (아직 실행하지 않음)."""
    cat = item["classification"]["category"]
    title = item["title"]
    rel = item["related"]
    proj = next((r for r in rel if r["collection"] == "projects"), None)
    subj = next((r for r in rel if r["collection"] == "subjects"), None)
    cands = []
    deadlines = [x for x in item["extractions"] if x["extraction_type"] == "deadline" and not x["past"]]
    if not deadlines:
        deadlines = [x for x in item["extractions"] if not x["past"]][:1]
    for x in deadlines[:3]:
        label = {"마감": "마감", "제출": "제출 마감", "까지": "마감", "시험일": "시험", "발표일": "발표", "접수": "접수 마감"}.get(x["label"], x["label"] or "일정")
        cands.append({"action_type": "create_event", "confidence": x["confidence"], "evidence": f"{x['evidence_location']}: {x['snippet']}",
                      "proposed_payload": {"title": f"{title} {label}".strip(), "date": x["value"],
                                           "start": x["time"] or "", "end": "", "type": "마감" if x["extraction_type"] == "deadline" else "일정",
                                           "importance": 4 if x["extraction_type"] == "deadline" else 3}})
    if proj and deadlines and cat in ("과제", "발표", "공모전"):
        d = deadlines[0]
        cands.append({"action_type": "create_project_task", "confidence": round(min(d["confidence"], proj["score"]), 2),
                      "evidence": f"관련 프로젝트 「{proj['title']}」 ({proj['evidence']}) · 마감 {d['value']}",
                      "proposed_payload": {"project_id": proj["id"], "title": f"「{title}」 {'제출' if cat == '과제' else cat} 준비",
                                           "due": dadd(d["value"], -1)}})
    if subj and cat == "시험" and deadlines:
        d = deadlines[0]
        cands.append({"action_type": "set_exam_date", "confidence": round(min(d["confidence"], subj["score"]), 2),
                      "evidence": f"관련 과목 「{subj['title']}」 · {d['evidence_location']}: {d['snippet']}",
                      "proposed_payload": {"subject_id": subj["id"], "exam_date": d["value"]}})
    for c in cands:
        c["id"] = "cand_" + short_hash(item["content_hash"], c["action_type"], c["proposed_payload"].get("title", ""),
                                       c["proposed_payload"].get("date") or c["proposed_payload"].get("due") or c["proposed_payload"].get("exam_date"))
        c["approval_status"] = "needs_review" if c["confidence"] < 0.7 else "pending"
        c["dedup_key"] = f"{c['action_type']}:{re.sub(chr(32), '', c['proposed_payload'].get('title', c['proposed_payload'].get('subject_id', '')))}:" \
                         f"{c['proposed_payload'].get('date') or c['proposed_payload'].get('due') or c['proposed_payload'].get('exam_date')}"
    return cands


# ------------------------------------------------------------------ 서비스
class Intake:
    def __init__(self, eng):
        self.eng = eng

    @property
    def s(self):
        return self.eng.s

    def add(self, now, text: str = "", url: str = "", name: str = "", data: bytes | None = None) -> dict:
        items = self.s["intake"]
        if data is not None:
            kind, _ = inspect(name, data)
            h = hashlib.sha256(data).hexdigest()
            src = "file"
        elif url:
            if not re.match(r"^https?://[^\s/$.?#].[^\s]*$", url) or len(url) > 2000:
                raise IntakeError("올바른 웹 주소가 아닙니다.")
            kind, h, src, name = "url", hashlib.sha256(url.encode()).hexdigest(), "url", url
        else:
            if not text.strip():
                raise IntakeError("내용이 비어 있습니다.")
            if len(text) > 100_000:
                raise IntakeError("붙여 넣은 글이 너무 깁니다 (최대 10만 자).")
            kind, h, src = "text", hashlib.sha256(text.encode()).hexdigest(), "text"
            name = name or "붙여 넣은 글"
        dup = next((i for i in items.values() if i["content_hash"] == h), None)
        if dup:
            dup["upload_count"] = dup.get("upload_count", 1) + 1
            dup["updated_at"] = iso(now)
            self.eng.store.mark()
            return {**dup, "duplicate": True}
        iid = uid("in_")
        ref = None
        if data is not None and kind in ("pdf", "docx", "text"):
            ref = f"intake/{iid}.{ext_of(name)}"
            try:
                self.eng.store.write_file(ref, data)
            except Exception:
                ref = None
        item = {"id": iid, "owner_id": "owner", "source_type": src, "kind": kind, "original_filename": name[:200],
                "size": len(data) if data is not None else len(text or url), "storage_reference": ref, "content_hash": h,
                "raw_text": text if src == "text" else "", "url": url, "extracted_text": "", "title": "",
                "classification": None, "related": [], "extractions": [], "candidates": [], "project_id": None,
                "processing_status": "queued", "error": "", "upload_count": 1, "document_id": None,
                "created_at": iso(now), "updated_at": iso(now)}
        items[iid] = item
        if data is not None and kind in ("pdf", "docx", "text"):
            item["_pending_bytes"] = True
            self._bytes[iid] = data
        self.eng.audit("intake.add", f"{src}: {name[:80]}", now)
        self.eng.store.mark(important=True)
        return item

    _bytes: dict = {}

    def process_next(self, now) -> bool:
        q = sorted([i for i in self.s["intake"].values() if i["processing_status"] == "queued"], key=lambda i: i["created_at"])
        if not q:
            return False
        self.process(q[0], now)
        return True

    def process(self, item: dict, now) -> dict:
        item["processing_status"] = "processing"
        try:
            if item["kind"] == "image":
                return self._unsupported(item, now, "이미지 속 글자 인식(OCR) 엔진이 무료 서버에 없어 읽을 수 없습니다. 사진 속 글자를 텍스트로 붙여 넣어 주세요.")
            if item["kind"] == "audio":
                return self._unsupported(item, now, "음성 인식기가 무료 서버에 없어 음성 파일을 글로 바꿀 수 없습니다.")
            if item["kind"] == "url":
                item.update(extracted_text=item["url"], title=item["url"][:60],
                            classification={"category": "링크", "confidence": 1.0, "reasons": ["웹 주소"], "scores": {}})
                return self._unsupported(item, now, "웹 페이지 본문 가져오기는 보안(내부망 접근 위험)과 무료 한도 때문에 지원하지 않습니다. "
                                                    "링크만 보관했습니다. 내용이 필요하면 본문을 붙여 넣어 주세요.", keep_status="classified")
            if item["source_type"] == "text":
                text = item["raw_text"]
            else:
                data = self._bytes.pop(item["id"], None)
                if data is None and item.get("storage_reference"):
                    data = self.eng.store.read_file(item["storage_reference"])
                if data is None:
                    raise IntakeError("원본 파일을 다시 찾을 수 없습니다 (서버 재시작으로 임시 파일이 사라짐). 다시 올려 주세요.")
                text = extract_text(item["kind"], data)
            item.pop("_pending_bytes", None)
            td = today(self.s, now)
            item["extracted_text"] = text[:50_000]
            item["title"] = title_of(text, item["original_filename"])
            item["classification"] = classify(text, item["original_filename"])
            item["related"] = related(self.s, text)
            item["project_id"] = next((r["id"] for r in item["related"] if r["collection"] == "projects" and r["score"] >= 0.6), None)
            item["extractions"] = extract_deadlines(text, td)
            cands = propose(self.s, item)
            keys = {c["dedup_key"]: i for i in self.s["intake"].values() if i["id"] != item["id"] for c in i["candidates"]
                    if c["approval_status"] in ("pending", "needs_review", "applied")}
            for c in cands:
                if c["dedup_key"] in keys:
                    c["approval_status"] = "duplicate"
                    c["duplicate_of"] = keys[c["dedup_key"]]["id"]
            item["candidates"] = cands
            if not item.get("document_id"):  # 검색·자료 조사에 쓰이도록 내부 문서로 보관 (내부 작업이라 자동)
                did = "doc_in_" + item["id"]
                self.s["c"].setdefault("documents", {})[did] = {
                    "id": did, "title": item["title"] if item["source_type"] == "text" else item["original_filename"],
                    "type": "업로드 자료", "status": "완료", "due_date": "", "content": text[:200_000],
                    "project_id": item["project_id"] or "", "version": 1, "intake_id": item["id"],
                    "created_at": iso(now), "updated_at": iso(now)}
                item["document_id"] = did
            low = item["classification"]["confidence"] < 0.5 or any(c["approval_status"] == "needs_review" for c in cands)
            item["processing_status"] = "needs_review" if low else "classified"
            self.eng.log(None, "research", "INTAKE",
                         f"수집함 분석: 「{item['title']}」 {item['classification']['category']} · 날짜 {len(item['extractions'])}개 · 후보 {len(cands)}개", now)
        except IntakeError as e:
            item.update(processing_status="error", error=str(e))
        except Exception as e:
            item.update(processing_status="error", error=f"분석 중 오류: {type(e).__name__}: {e}"[:300])
        item["updated_at"] = iso(now)
        self.eng.store.mark(important=True)
        return item

    def _unsupported(self, item, now, why, keep_status="unsupported"):
        item.update(processing_status=keep_status, error=why, updated_at=iso(now))
        self.eng.store.mark(important=True)
        return item

    def decide(self, iid: str, cid: str, approve: bool, payload: dict | None, now) -> dict:
        """후보 승인 = 사용자의 개별 승인. 승인한 내용만 실제 일정·할 일·과목에 반영하고 검수 기록을 남긴다."""
        from . import verify as VF
        from .schema import Invalid, validate
        item = self.s["intake"].get(iid)
        c = item and next((x for x in item["candidates"] if x["id"] == cid), None)
        if not c:
            raise IntakeError("후보를 찾을 수 없습니다.")
        if c["approval_status"] not in ("pending", "needs_review", "duplicate"):
            raise IntakeError("이미 처리한 후보입니다.")
        if not approve:
            c["approval_status"] = "rejected"
            item["updated_at"] = iso(now)
            self.eng.store.mark(important=True)
            return c
        p = {**c["proposed_payload"], **{k: v for k, v in (payload or {}).items() if k in c["proposed_payload"]}}
        if payload:
            c["user_edited"] = {k: [c["proposed_payload"].get(k), v] for k, v in payload.items()
                                if k in c["proposed_payload"] and c["proposed_payload"].get(k) != v}
        rid = "in" + short_hash(cid)
        try:
            if c["action_type"] == "create_event":
                rec = validate("events", {k: p[k] for k in ("title", "date", "start", "end", "type", "importance") if p.get(k) not in (None, "")},
                               refs=self.s["c"])
                rid = "ev_" + rid
                coll = "events"
            elif c["action_type"] == "create_project_task":
                rec = validate("project_tasks", {"project_id": p["project_id"], "title": p["title"], "due": p.get("due") or ""},
                               refs=self.s["c"])
                rid = "pt_" + rid
                coll = "project_tasks"
            elif c["action_type"] == "set_exam_date":
                sub = self.s["c"].get("subjects", {}).get(p["subject_id"])
                if not sub:
                    raise IntakeError("과목이 삭제되었습니다.")
                rec = validate("subjects", {"exam_date": p["exam_date"]}, existing=sub, refs=self.s["c"])
                rid, coll = sub["id"], "subjects"
            else:
                raise IntakeError("알 수 없는 후보 종류")
        except Invalid as e:
            raise IntakeError(f"입력값 오류: {e}")
        c_ = self.s["c"].setdefault(coll, {})
        old = c_.get(rid, {})
        c_[rid] = {**old, **rec, "id": rid, "created_at": old.get("created_at", iso(now)), "updated_at": iso(now),
                   "version": int(old.get("version", 0)) + 1, "from_intake": iid}
        self.eng.history(coll, rid, "update" if old else "create", now)
        c.update(approval_status="applied", applied_record=f"{coll}/{rid}", applied_at=iso(now), final_payload=p)
        vt = {"events": "event", "project_tasks": "project_task"}.get(coll)
        if vt:
            run = VF.verify_artifact(self.eng, vt, rid, now)
            c["verification"] = run["status"]
        self.eng.audit("intake.apply", f"{c['action_type']} → {coll}/{rid}", now)
        item["updated_at"] = iso(now)
        self.eng.store.mark(important=True)
        return c
