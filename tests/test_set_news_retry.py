# -*- coding: utf-8 -*-
"""set_news: รู้สาเหตุที่ล้ม ล้มเร็ว แล้วลองใหม่หนึ่งครั้ง (incident 1 ก.ย. 2026)

เดิม: predicate ของ expect_response รับเฉพาะ HTTP 200 → API ตอบ 401/403/429/5xx
หรือได้หน้ากันบอทของ Incapsula มา ก็เผาเวลารอจนหมด timeout (90 วิ) เท่ากันหมด
และใน bot_log.txt หน้าตาเหมือนกันเป๊ะ — ล้ม 16 รอบติดโดยไม่รู้ว่าเป็นอาการไหน
ใหม่: ดัก response ไว้ก่อน (สถานะ != 200 → SetNewsBlocked ทันทีที่ goto จบ),
เช็คหน้ากันบอทจาก HTML (SetNewsChallenged) แล้วลองใหม่หนึ่งครั้งด้วย context ใหม่
"""
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import set_news  # noqa: E402

NEWS = [{"id": "1", "symbol": "AOT", "headline": "แจ้งผลประกอบการ", "url": ""}]


class _Recorder:
    """แทน _fetch_once ในเทสต์ — จดทุกครั้งที่ถูกเรียก แล้วคืน/โยนตามคิวที่สั่งไว้"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, days_back, timeout_s):
        self.calls.append((days_back, timeout_s))
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


# ── คลาส exception: ผู้เรียกที่ except Exception ต้องยังจับได้เหมือนเดิม ──


def test_error_classes_stay_plain_exceptions():
    assert issubclass(set_news.SetNewsError, Exception)
    assert issubclass(set_news.SetNewsBlocked, set_news.SetNewsError)
    assert issubclass(set_news.SetNewsChallenged, set_news.SetNewsError)
    assert set_news.SetNewsBlocked(403).status == 403


def test_module_constants_replace_the_old_90s_wait():
    assert set_news.RESPONSE_TIMEOUT_S == 45
    assert set_news.FETCH_RETRIES == 1


# ── _is_challenge_html: หน้ากันบอทหน้าตาแบบไหน ──────────────────


@pytest.mark.parametrize("html", [
    "<html><body>Incapsula incident ID: 1234-5678901234</body></html>",
    "<h2>Request unsuccessful.</h2><p>ลองใหม่ภายหลัง</p>",
    "<html><head><title>Access denied</title></head></html>",
    "<TITLE>ACCESS DENIED</TITLE>",                     # ตัวพิมพ์ใหญ่ก็ต้องจับได้
])
def test_challenge_page_is_detected(html):
    assert set_news._is_challenge_html(html) is True


@pytest.mark.parametrize("html", [
    "<html><title>ข่าววันนี้ | SET</title><div>บริษัทแจ้งงบ</div></html>",
    # เว็บ SET วิ่งผ่าน Incapsula ตลอด — Imperva แทรก script นี้ในหน้า "ปกติ" ด้วย
    # ถ้านับเป็นสัญญาณบล็อก = ทุกรอบที่สำเร็จกลายเป็น Challenged แล้วข่าวหยุดเดินทั้งระบบ
    '<html><script src="/_Incapsula_Resource?SWJIYLWA=719d34d3"></script>'
    "<div>บริษัทแจ้งงบ</div></html>",
    "",
])
def test_normal_page_is_not_a_challenge(html):
    assert set_news._is_challenge_html(html) is False


# ── ตัวห่อ retry: ลองใหม่หนึ่งครั้งเฉพาะอาการที่ "เปิดใหม่แล้วอาจผ่าน" ──


@pytest.mark.parametrize("err", [
    set_news.SetNewsBlocked(403),
    set_news.SetNewsChallenged("เจอหน้ากันบอท"),
    set_news.PlaywrightTimeout("Timeout 45000ms exceeded"),
])
def test_retries_once_then_returns_result(err):
    fetch = _Recorder(err, NEWS)
    assert set_news.fetch_company_news(days_back=2, _fetch=fetch) == NEWS
    assert len(fetch.calls) == 2
    assert [c[0] for c in fetch.calls] == [2, 2]        # ส่ง days_back เดิมทั้งสองครั้ง


def test_unexpected_error_propagates_without_retry():
    fetch = _Recorder(ValueError("โครงสร้างเว็บเปลี่ยน"), NEWS)
    with pytest.raises(ValueError):
        set_news.fetch_company_news(_fetch=fetch)
    assert len(fetch.calls) == 1


def test_both_attempts_fail_raises_the_last_error():
    first = set_news.SetNewsBlocked(429)
    last = set_news.SetNewsChallenged("เจอหน้ากันบอท")
    fetch = _Recorder(first, last)
    with pytest.raises(set_news.SetNewsChallenged) as ei:
        set_news.fetch_company_news(_fetch=fetch)
    assert ei.value is last
    assert len(fetch.calls) == set_news.FETCH_RETRIES + 1 == 2


def test_success_uses_exactly_one_attempt():
    fetch = _Recorder(NEWS)
    assert set_news.fetch_company_news(_fetch=fetch) == NEWS
    assert len(fetch.calls) == 1


def test_goto_timeout_defaults_to_the_response_timeout():
    # เดิม goto ใช้ 90 วิ พอมี retry = ค้างได้ 3 นาทีต่อรอบ (คำสั่ง "ข่าวงบ" ก็โดน ทั้งที่ไม่มี backoff)
    fetch = _Recorder(NEWS)
    set_news.fetch_company_news(_fetch=fetch)
    assert fetch.calls == [(1, set_news.RESPONSE_TIMEOUT_S)]


def test_every_attempt_is_logged_with_reason_and_attempt_number(caplog):
    fetch = _Recorder(set_news.SetNewsBlocked(403), NEWS)
    with caplog.at_level(logging.INFO, logger="bot.set_news"):
        set_news.fetch_company_news(_fetch=fetch)
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(warns) == 1 and "403" in warns[0] and "ครั้งที่ 1" in warns[0]
    assert len(infos) == 1 and "ครั้งที่ 2" in infos[0] and "1 ข่าว" in infos[0]


# ── _timeout_reason: รอไม่ทัน แต่ถ้ามีใบที่ถูกปฏิเสธ นั่นคือสาเหตุจริง ──


def test_timeout_reason_prefers_a_recorded_rejection():
    t = set_news.PlaywrightTimeout("Timeout 45000ms exceeded")
    assert set_news._timeout_reason([503, 403], t).status == 503   # ใบแรกคือสาเหตุ
    assert set_news._timeout_reason([], t) is t                    # ไม่มีใบไหนถูกปฏิเสธ = ช้าจริง


# ── _fetch_once: ลำดับจริงในหน้าเว็บ (ฉีดหน้าปลอม — เทสต์ไม่ต้องมี Playwright) ──

_API_URL = ("https://www.set.or.th/api/cms/v1/news/set"
            "?sourceId=company&fromDate=01/09/2026&toDate=02/09/2026")
_PAYLOAD = {"newsGroups": [{"newsInfoList": [
    {"id": "77", "symbol": "aot", "headline": "แจ้งผลประกอบการ",
     "datetime": "2026-09-02T17:30:00", "url": "/news/77"},
]}]}


class FakeResponse:
    def __init__(self, status, payload=None, url=_API_URL):
        self.url, self.status, self._payload = url, status, payload

    def json(self):
        return self._payload


class FakeInfo:
    def __init__(self, page):
        self._page = page

    @property
    def value(self):
        if self._page.matched is None:
            raise set_news.PlaywrightTimeout("Timeout 45000ms exceeded")
        return self._page.matched


class FakeExpect:
    """เลียน page.expect_response — ออกจากบล็อกพร้อม exception = ยกเลิกการรอทันที
    (ของจริง `EventContextManager.__exit__` ก็ `_cancel()` แบบนี้) · ออกปกติ = คำตอบ
    ที่มาหลัง domcontentloaded ค่อยทยอยเข้ามา"""

    def __init__(self, page):
        self._page = page

    def __enter__(self):
        return FakeInfo(self._page)

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            self._page.deliver(self._page.after_goto)
        return False


class FakePage:
    """ผิวของ Playwright เท่าที่ _fetch_once ใช้ · during_goto = คำตอบระหว่างหน้าโหลด,
    after_goto = คำตอบที่มาหลัง domcontentloaded (SPA ของ SET ยิง XHR ทีหลัง = ท่าจริง)"""

    def __init__(self, *, during_goto=(), after_goto=(), html="<html>ข่าววันนี้</html>",
                 content_error=None):
        self.during_goto, self.after_goto = list(during_goto), list(after_goto)
        self.html, self.content_error = html, content_error
        self.handlers, self.log, self.matched, self.pred = [], [], None, None

    def on(self, event, fn):
        self.log.append("on")
        self.handlers.append(fn)

    def expect_response(self, pred, timeout=None):
        self.log.append("expect")
        self.pred = pred
        return FakeExpect(self)

    def goto(self, url, **kw):
        self.log.append("goto")
        self.deliver(self.during_goto)

    def deliver(self, responses):
        for r in responses:
            for fn in self.handlers:
                fn(r)
            if self.matched is None and self.pred is not None and self.pred(r):
                self.matched = r

    def content(self):
        if self.content_error:
            raise self.content_error
        return self.html


class FakeBrowserStack:
    """เล่นบทเดียวทั้ง playwright / browser / context — _fetch_once สนใจแค่หน้ากับ close()"""

    def __init__(self, page):
        self.page, self.closed, self.routes = page, False, []
        self.chromium = SimpleNamespace(launch=lambda **kw: self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def new_context(self, **kw):
        return self

    def route(self, matcher, handler):
        self.routes.append(matcher)

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


def _fake_stack(monkeypatch, page):
    import playwright.sync_api as pw
    stack = FakeBrowserStack(page)
    monkeypatch.setattr(pw, "sync_playwright", lambda: stack)
    return stack


def test_fetch_once_parses_news_and_closes_the_browser(monkeypatch):
    page = FakePage(after_goto=[FakeResponse(200, _PAYLOAD)])
    stack = _fake_stack(monkeypatch, page)
    out = set_news._fetch_once(1, 45)
    assert [(r["symbol"], r["id"]) for r in out] == [("AOT", "77")]
    assert page.log == ["on", "expect", "goto"]      # ดักคำตอบก่อนเปิดหน้าเสมอ
    assert stack.closed is True


def test_fetch_once_fails_fast_when_blocked_while_the_page_loads(monkeypatch):
    page = FakePage(during_goto=[FakeResponse(403)])
    _fake_stack(monkeypatch, page)
    with pytest.raises(set_news.SetNewsBlocked) as ei:
        set_news._fetch_once(1, 45)
    assert ei.value.status == 403


def test_fetch_once_reports_a_block_that_arrives_after_the_page_loaded(monkeypatch):
    """ท่าจริงของ incident: XHR มาหลัง domcontentloaded — 403 ที่มาระหว่างรอ
    ต้องรายงานว่า Blocked ไม่ใช่ Timeout (ไม่งั้น "สถานะ" กับ log ขัดกันเอง)"""
    page = FakePage(after_goto=[FakeResponse(429)])
    _fake_stack(monkeypatch, page)
    with pytest.raises(set_news.SetNewsBlocked) as ei:
        set_news._fetch_once(1, 45)
    assert ei.value.status == 429


def test_fetch_once_ignores_a_redirect_hop(monkeypatch):
    """3xx = ต่อทางระหว่างทาง ไม่ใช่การบล็อก — ถ้านับด้วย รอบที่ปกติดีจะล้มฟรี"""
    page = FakePage(during_goto=[FakeResponse(302)],
                    after_goto=[FakeResponse(200, _PAYLOAD)])
    _fake_stack(monkeypatch, page)
    assert len(set_news._fetch_once(1, 45)) == 1


def test_fetch_once_does_not_blame_a_redirect_for_a_timeout(monkeypatch):
    page = FakePage(during_goto=[FakeResponse(302)])
    _fake_stack(monkeypatch, page)
    with pytest.raises(set_news.PlaywrightTimeout):     # ไม่ใช่ "Blocked HTTP 302"
        set_news._fetch_once(1, 45)


def test_fetch_once_still_reports_timeout_when_nothing_was_rejected(monkeypatch):
    page = FakePage()
    _fake_stack(monkeypatch, page)
    with pytest.raises(set_news.PlaywrightTimeout):
        set_news._fetch_once(1, 45)


def test_fetch_once_raises_on_a_real_challenge_page(monkeypatch):
    page = FakePage(html="<html><body>Incapsula incident ID: 9-9</body></html>",
                    after_goto=[FakeResponse(200, _PAYLOAD)])
    _fake_stack(monkeypatch, page)
    with pytest.raises(set_news.SetNewsChallenged):
        set_news._fetch_once(1, 45)


def test_fetch_once_treats_incapsula_script_as_normal_and_logs_it(monkeypatch, caplog):
    page = FakePage(html='<html><script src="/_Incapsula_Resource?x=1"></script>ข่าว</html>',
                    after_goto=[FakeResponse(200, _PAYLOAD)])
    _fake_stack(monkeypatch, page)
    with caplog.at_level(logging.INFO, logger="bot.set_news"):
        assert len(set_news._fetch_once(1, 45)) == 1
    assert any("_Incapsula_Resource" in r.getMessage() for r in caplog.records)


def test_fetch_once_survives_a_content_error_and_keeps_waiting(monkeypatch):
    page = FakePage(content_error=RuntimeError("Execution context was destroyed"),
                    after_goto=[FakeResponse(200, _PAYLOAD)])
    _fake_stack(monkeypatch, page)
    assert len(set_news._fetch_once(1, 45)) == 1
