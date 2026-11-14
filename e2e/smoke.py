"""브라우저 종단 간 점검 (테스트 12: 모바일 접속 포함).
사용법:  python e2e/smoke.py [http://localhost:8000]   — 빈 데이터로 띄운 서버를 대상으로 실행
"""
import sys
import time
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
OUT = sys.argv[2] if len(sys.argv) > 2 else "."
TODAY = (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()
OLD = (datetime.now(timezone.utc) + timedelta(hours=9) - timedelta(days=3)).date().isoformat()


def wait_task(page, kind, status, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        st = page.evaluate("fetch('/api/state').then(r=>r.json())")
        ts = [t for t in st["tasks"] if t["kind"] == kind]
        if ts and all(t["status"] == status for t in ts):
            return st
        time.sleep(0.5)
    raise AssertionError(f"{kind} 가 {status} 상태가 되지 않음: {[(t['kind'], t['status']) for t in st['tasks']]}")


def wf_done(page, template, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        st = page.evaluate("fetch('/api/state').then(r=>r.json())")
        ws = [w for w in st["workflows"] if w["template"] == template]
        if ws and ws[0]["status"] == "DONE":
            return ws[0]
        if ws and ws[0]["status"] == "FAILED":
            raise AssertionError(ws[0]["recovery"])
        time.sleep(0.5)
    raise AssertionError(f"{template} 워크플로 미완료: {[(w['template'], w['status']) for w in st['workflows']]}")


def main():
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        # ---- 모바일 화면 (390x844)
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                                  has_touch=True, accept_downloads=True, locale="ko-KR", timezone_id="Asia/Seoul")
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text else None)
        page.goto(BASE)
        page.get_by_placeholder("비밀번호 (8자 이상)").fill("correct horse battery")
        page.get_by_placeholder("비밀번호 확인").fill("correct horse battery")
        page.get_by_role("button", name="회사 설립하기").click()
        page.get_by_placeholder("예: 임용시험 합격").fill("임용시험 합격")
        page.get_by_role("button", name="등록하고 사무실로").click()
        page.wait_for_selector("canvas")
        wait_task(page, "chief.briefing", "DONE")                      # 접속만으로 직원이 출근해 일함
        page.screenshot(path=f"{OUT}/m1_office.png")
        assert page.locator("button[aria-label*='루미']").count() == 1

        # 일정 2건(시간 겹침) + 밀린 마감 1건 입력 → 자율 발견
        csrf = page.evaluate("fetch('/api/me').then(r=>r.json()).then(j=>j.csrf)")
        for body in ({"title": "전공 수업", "date": TODAY, "start": "10:00", "end": "12:00"},
                     {"title": "멘토링", "date": TODAY, "start": "11:00", "end": "13:00"},
                     {"title": "과제 제출", "date": OLD, "type": "마감"}):
            r = page.evaluate("([b,c])=>fetch('/api/c/events',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':c},body:JSON.stringify(b)}).then(r=>r.status)", [body, csrf])
            assert r == 200
        page.evaluate("c=>fetch('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json','X-CSRF-Token':c},body:JSON.stringify({discovery_interval_sec:30})})", csrf)
        wait_task(page, "schedule.conflicts", "DONE", timeout=90)
        wait_task(page, "schedule.overdue", "WAITING_APPROVAL", timeout=30)

        # 업무 센터 → 승인
        page.get_by_role("button", name="업무 센터").click()
        page.screenshot(path=f"{OUT}/m2_tasks.png")
        page.get_by_role("button", name="승인", exact=True).first.click()
        st = wait_task(page, "schedule.overdue", "DONE")
        assert [e for e in st["c"]["events"].values() if e["title"] == "과제 제출"][0]["date"] == TODAY

        # 문서 공장 → 생성 → DOCX 내려받기
        page.get_by_role("button", name="문서 공장").click()
        page.get_by_role("button", name="일정표", exact=True).click()
        wait_task(page, "docs.generate", "DONE")
        page.wait_for_selector("text=일정표 (")
        with page.expect_download() as dl:
            page.get_by_role("button", name="docx").first.click()
        path = dl.value.path()
        from docx import Document
        cells = [c.text for t in Document(path).tables for r in t.rows for c in r.cells]
        assert "전공 수업" in cells, cells
        page.screenshot(path=f"{OUT}/m3_docs.png")

        # 업무 실행(워크플로): 충돌 발견 → 대안 선택 → 등록
        TOMORROW = (datetime.now(timezone.utc) + timedelta(hours=9) + timedelta(days=1)).date().isoformat()
        page.evaluate("([b,c])=>fetch('/api/c/events',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':c},body:JSON.stringify(b)})",
                      [{"title": "전공 수업 B", "date": TOMORROW, "start": "14:00", "end": "16:00"}, csrf])
        page.get_by_role("button", name="업무 지휘실").click()
        page.get_by_placeholder("예: 내일 오후 3시").fill("내일 오후 3시에 팀 회의 1시간 잡아줘")
        page.get_by_role("button", name="맡기기").click()
        page.wait_for_selector("text=충돌 발견, 대안을 골라 주세요", timeout=30000)
        page.screenshot(path=f"{OUT}/m6_flow_choice.png")
        page.locator("div[role=dialog] button", has_text="로 등록").first.click()
        wf_done(page, "schedule")
        page.get_by_role("button", name="닫기").click()
        # 문서 워크플로 → 검증된 파일 내려받기
        page.get_by_placeholder("예: 내일 오후 3시").fill("이번 주 일정표 문서 만들어줘")
        page.get_by_role("button", name="맡기기").click()
        wf_done(page, "document")
        page.wait_for_selector("div[role=dialog] >> text=⬇")
        page.screenshot(path=f"{OUT}/m7_flow_doc.png")
        with page.expect_download() as dl2:
            page.locator("div[role=dialog] button", has_text="⬇").first.click()
        cells2 = [c.text for t in Document(dl2.value.path()).tables for r in t.rows for c in r.cells]
        assert "팀 회의" in cells2, cells2
        page.get_by_role("button", name="닫기").click()

        # 3.0: AI 수집함 — PDF 공지 업로드 → 분석 → 후보 승인 → 일정 반영
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas
        import io as _io
        pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
        buf = _io.BytesIO()
        cv = canvas.Canvas(buf)
        cv.setFont("HYGothic-Medium", 12)
        for i, ln in enumerate(["교육평가 과제 공지", "제출 마감 2026년 12월 2일 오후 6시", "주제: 형성평가 사례 분석"]):
            cv.drawString(50, 800 - 20 * i, ln)
        cv.save()
        page.get_by_role("button", name="AI 수집함").click()
        page.locator("input[type=file]").set_input_files(files=[{"name": "평가과제.pdf", "mimeType": "application/pdf", "buffer": buf.getvalue()}])
        page.wait_for_selector("text=분류: 과제", timeout=30000)
        page.screenshot(path=f"{OUT}/m8_intake.png", full_page=True)
        page.get_by_role("button", name="승인하고 반영").first.click()
        page.wait_for_selector("text=반영됨", timeout=15000)
        st = page.evaluate("fetch('/api/state').then(r=>r.json())")
        assert any(e["date"] == "2026-12-02" and e.get("start") == "18:00" for e in st["c"]["events"].values())
        page.get_by_role("button", name="내 인생 검색").click()
        page.get_by_placeholder("예: IEP").fill("형성평가")
        page.get_by_role("button", name="검색", exact=True).click()
        page.wait_for_selector("h2:has-text(\"결과 \")")
        assert page.locator("text=수집함").count() >= 1
        page.screenshot(path=f"{OUT}/m9_search.png", full_page=True)
        page.get_by_role("button", name="검수 센터").click()
        page.wait_for_selector("text=검수 기록")
        assert page.locator("text=통과").count() >= 1
        page.screenshot(path=f"{OUT}/m10_verify.png", full_page=True)

        # 새로고침 후에도 데이터 유지 + 직원 패널
        page.reload()
        page.get_by_role("button", name="AI 직원 사무실").click()
        page.wait_for_selector("canvas")
        page.locator("button[aria-label*='타임']").click()
        page.wait_for_selector("text=완료한 업무")
        page.screenshot(path=f"{OUT}/m4_agent.png")
        page.get_by_role("button", name="닫기").click()
        for name in ("홈", "AI 수집함", "업무 지휘실", "AI가 발견한 일", "에이전트 관리", "AI 회의실", "일정·목표", "학습·창작·재무", "프로젝트",
                     "검수 센터", "내 인생 검색", "기억 저장소", "인생 시뮬레이터", "비상대책실", "설정·무료 운영"):
            page.get_by_role("button", name=name, exact=True).click()
            page.wait_for_timeout(300)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), f"{name}: 가로 넘침"
        page.get_by_role("button", name="홈", exact=True).click()
        page.wait_for_timeout(500)
        page.screenshot(path=f"{OUT}/m5_dashboard.png", full_page=True)
        assert page.evaluate("fetch('/api/state').then(r=>r.json()).then(s=>s.sync.state)") in ("LOCAL_ONLY", "SAVED", "PENDING")
        ctx.close()

        # ---- 데스크톱 화면: 재접속(로그인) 후 이전 상태 복구 확인
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="ko-KR", timezone_id="Asia/Seoul")
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE)
        page.get_by_placeholder("비밀번호").fill("correct horse battery")
        page.get_by_role("button", name="출근시키기").click()
        page.get_by_role("button", name="AI 직원 사무실").click()
        page.wait_for_selector("canvas")
        st = page.evaluate("fetch('/api/state').then(r=>r.json())")
        assert len(st["c"]["events"]) == 6 and any(t["kind"] == "schedule.conflicts" and t["status"] == "DONE" for t in st["tasks"])
        page.wait_for_timeout(1500)
        page.screenshot(path=f"{OUT}/d1_office.png")
        page.get_by_role("button", name="일정·목표").click()
        page.get_by_role("button", name="원형 시간표").click()
        page.wait_for_timeout(300)
        page.screenshot(path=f"{OUT}/d2_clock.png")
        ctx.close()
        browser.close()
    assert not errors, errors
    print("E2E OK")


if __name__ == "__main__":
    main()
