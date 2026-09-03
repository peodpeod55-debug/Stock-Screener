# -*- coding: utf-8 -*-
"""ดึงข่าว SET ล้มติดกัน: เว้นระยะก่อนลองใหม่ + ธง "กำลังล่ม" ที่อยู่ข้ามรีสตาร์ท

incident 1 ก.ย. 2026: news_monitor_job ล้ม 16 รอบติด 17:05-19:35 — บอทเปิด Chromium
ใหม่ทุก 10 นาทีโดยไม่เว้นระยะเลย และ ✅ "กลับมาปกติ" ไม่เคยถูกส่ง เพราะตัวนับอยู่ใน
memory ล้วน พอรีบูตเครื่องเช้าวันถัดไป รอบ 08:49 ที่สำเร็จจึงเงียบสนิท
ใหม่: ล้ม ≥3 รอบ → เว้น 20/40/60 นาทีตามความหนัก · ธง news_outage ลง digest_state
(ไฟล์) ด้วย → รีสตาร์ทแล้วรอบที่สำเร็จยังรู้ว่าต้องแจ้ง ✅ · จำสาเหตุล่าสุดไว้ให้ "สถานะ"
"""
import asyncio
import datetime
import logging
import os
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import set_news  # noqa: E402
import telegram_bot as tb  # noqa: E402

BKK = ZoneInfo("Asia/Bangkok")
THU_1700 = datetime.datetime(2026, 8, 27, 17, 0, tzinfo=BKK)   # พฤหัส ต้นช่วง poll เย็น


def _plus(when, minutes):
    return when + datetime.timedelta(minutes=minutes)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text=None, **kwargs):
        self.sent.append((chat_id, text))


def _reset_memory(monkeypatch):
    """ตัวนับ/ธงใน memory กลับสู่สภาพ "บอทเพิ่งเปิด" (monkeypatch คืนค่าเดิมตอนจบเทสต์)"""
    for name, val in (("_news_fail_count", 0), ("_news_fail_alerted", False),
                      ("_news_next_try", None), ("_news_last_error", None),
                      ("_news_recovery_sent", False)):
        monkeypatch.setattr(tb, name, val)


@pytest.fixture
def env(monkeypatch):
    """บอทปลอม + ตัวนับ/ธงใน memory ตั้งต้นใหม่ทุกเทสต์ (monkeypatch คืนค่าเดิมให้เอง)"""
    bot = FakeBot()
    monkeypatch.setattr(tb, "_load_chat_ids", lambda: [111])
    monkeypatch.setattr(tb, "_is_market_holiday", lambda d: False)
    monkeypatch.setattr(tb.set_news, "news_data_age_hours", lambda: 1.0)
    _reset_memory(monkeypatch)
    tb._save_digest_state({})
    return SimpleNamespace(bot=bot, ctx=SimpleNamespace(bot=bot))


def _freeze(monkeypatch, when):
    """ให้ datetime.datetime.now(tz) ในบอทคืนเวลาที่กำหนด (แบบเดียวกับ test_job_retry)"""
    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(tb, "datetime", SimpleNamespace(
        datetime=FakeDT, date=datetime.date, time=datetime.time, timedelta=datetime.timedelta))


def _poll(env, monkeypatch, when, result, calls=None):
    """รัน news_monitor_job ณ เวลา when — result เป็น exception = ดึงล้ม, เป็น list = สำเร็จ"""
    _freeze(monkeypatch, when)

    def fake(max_age_hours, days_back):
        if calls is not None:
            calls.append(when)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(tb.set_news, "check_new_earnings_news", fake)
    asyncio.run(tb.news_monitor_job(env.ctx))


def _fail_until(env, monkeypatch, n, err=None, calls=None):
    """ล้ม n รอบติดกัน รอบละ 10 นาที เริ่ม 17:00 — คืนเวลาของรอบสุดท้าย"""
    err = err or RuntimeError("เว็บล่ม")
    when = THU_1700
    for i in range(n):
        when = _plus(THU_1700, i * 10)
        _poll(env, monkeypatch, when, err, calls)
    return when


# ── ตารางเว้นระยะ: ยิ่งล้มนาน ยิ่งเว้นห่าง (แต่ไม่หยุดถาวร) ──────


@pytest.mark.parametrize("fails,minutes", [
    (0, 0), (1, 0), (2, 0),          # ยังไม่ถึงเกณฑ์เตือน = ยิงตามรอบปกติทุก 10 นาที
    (3, 20), (4, 20),
    (5, 40), (6, 40),
    (7, 60), (12, 60),               # เพดาน — ยังลองต่อเรื่อย ๆ ไม่เลิกลอง
])
def test_backoff_table(fails, minutes):
    assert tb._news_backoff_minutes(fails) == minutes


def test_backoff_starts_exactly_at_the_alert_threshold():
    assert tb._news_backoff_minutes(tb._NEWS_FAIL_ALERT_AFTER - 1) == 0
    assert tb._news_backoff_minutes(tb._NEWS_FAIL_ALERT_AFTER) == 20


# ── job: เว้นระยะจริงหลังล้มติดกัน ──────────────────────────────


def test_first_failures_do_not_delay_the_next_poll(env, monkeypatch):
    _fail_until(env, monkeypatch, 2)
    assert tb._news_fail_count == 2 and tb._news_next_try is None


def test_third_failure_sets_a_20_minute_backoff(env, monkeypatch):
    last = _fail_until(env, monkeypatch, 3)
    assert tb._news_fail_count == 3
    assert tb._news_next_try == _plus(last, 20)


def test_poll_is_skipped_during_backoff_then_resumes(env, monkeypatch):
    calls = []
    _fail_until(env, monkeypatch, 3, calls=calls)
    assert len(calls) == 3
    _poll(env, monkeypatch, _plus(THU_1700, 30), [], calls)   # 17:30 — ยังไม่ถึง 17:40
    assert len(calls) == 3
    _poll(env, monkeypatch, _plus(THU_1700, 45), [], calls)   # 17:45 — พ้น backoff
    assert len(calls) == 4


def test_backoff_lengthens_as_failures_pile_up(env, monkeypatch):
    monkeypatch.setattr(tb, "_news_fail_count", 4)
    _poll(env, monkeypatch, THU_1700, RuntimeError("เว็บล่ม"))
    assert tb._news_fail_count == 5
    assert tb._news_next_try == _plus(THU_1700, 40)


def test_success_clears_the_backoff(env, monkeypatch):
    _fail_until(env, monkeypatch, 3)
    _poll(env, monkeypatch, _plus(THU_1700, 45), [])
    assert tb._news_fail_count == 0 and tb._news_next_try is None


def test_backoff_logs_when_the_next_attempt_is(env, monkeypatch, caplog):
    _fail_until(env, monkeypatch, 3)
    with caplog.at_level(logging.INFO, logger="bot"):
        _poll(env, monkeypatch, _plus(THU_1700, 30), [])
    msgs = [r.getMessage() for r in caplog.records if r.name == "bot"]
    assert any("backoff" in m and "17:40" in m for m in msgs), msgs


def test_startup_catchup_ignores_the_backoff(env, monkeypatch):
    """เก็บตกตอนเปิดบอทเป็น one-shot คนละทางกับ poll — รีสตาร์ทแล้วต้องได้ลองทันที"""
    monkeypatch.setattr(tb, "_news_next_try", _plus(THU_1700, 600))
    monkeypatch.setattr(tb, "_write_alive", lambda: None)
    monkeypatch.setattr(tb.set_news, "news_data_age_hours", lambda: 30.0)
    calls = []

    def fake(max_age_hours, days_back):
        calls.append(days_back)
        return []

    monkeypatch.setattr(tb.set_news, "check_new_earnings_news", fake)
    _freeze(monkeypatch, THU_1700)
    asyncio.run(tb.startup_catchup_job(env.ctx))
    assert calls == [2]


# ── ธง news_outage ในไฟล์: ✅ "กลับมาปกติ" ต้องรอดข้ามรีสตาร์ท ──


def _outage():
    return tb._load_digest_state().get("news_outage")


def _texts(env, mark):
    return [t for _, t in env.bot.sent if t.startswith(mark)]


def test_alert_broadcasts_once_and_persists_the_outage_flag(env, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="bot"):
        _fail_until(env, monkeypatch, 3, set_news.SetNewsBlocked(403))
    alerts = _texts(env, "⚠️")
    assert len(alerts) == 1 and "3 รอบ" in alerts[0]
    assert "Blocked HTTP 403" in alerts[0]        # บอกอาการจริง ไม่ให้เดาเอง
    flag = _outage()
    assert flag["fails"] == 3 and flag["reason"] == "Blocked HTTP 403"
    assert flag["since"] == _plus(THU_1700, 20).isoformat()   # รอบที่สามคือรอบที่แจ้ง
    assert any("แจ้งผู้ใช้แล้ว" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_alert_warns_when_the_flag_cannot_be_written(env, monkeypatch, caplog):
    """จดธงไม่ลง = รีสตาร์ทแล้ว ✅ หาย — ต้องไม่เงียบ แต่ ⚠️ ยังต้องถึงผู้ใช้เหมือนเดิม"""
    monkeypatch.setattr(tb, "_save_digest_state", lambda state: False)
    with caplog.at_level(logging.WARNING, logger="bot"):
        _fail_until(env, monkeypatch, 3, set_news.SetNewsBlocked(403))
    assert len(_texts(env, "⚠️")) == 1
    assert any("จดธง news_outage ไม่สำเร็จ" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_more_failures_update_the_flag_without_a_second_alert(env, monkeypatch):
    _fail_until(env, monkeypatch, 3, set_news.SetNewsBlocked(403))
    since = _outage()["since"]
    _poll(env, monkeypatch, _plus(THU_1700, 60), set_news.SetNewsChallenged("กันบอท"))
    assert len(_texts(env, "⚠️")) == 1                        # ไม่แจ้งซ้ำ
    flag = _outage()
    assert flag["fails"] == 4 and flag["reason"] == "Challenged (WAF)"
    assert flag["since"] == since                             # ยังเป็นช่วงล่มเดิม


def test_recovery_survives_a_restart_and_clears_the_flag(env, monkeypatch, caplog):
    _fail_until(env, monkeypatch, 3, set_news.SetNewsBlocked(403))
    since = _outage()["since"]
    _reset_memory(monkeypatch)          # จำลองรีสตาร์ท: ตัวนับหายหมด เหลือแต่ธงในไฟล์
    env.bot.sent.clear()
    with caplog.at_level(logging.INFO, logger="bot"):
        _poll(env, monkeypatch, _plus(THU_1700, 900), [])     # ศุกร์ 08:00 หลังรีบูต
    assert len(_texts(env, "✅")) == 1
    assert _outage() is None
    assert any("กลับมาปกติ" in r.getMessage() and since in r.getMessage()
               for r in caplog.records if r.levelno == logging.INFO)


def test_recovery_is_announced_only_once(env, monkeypatch):
    _fail_until(env, monkeypatch, 3, set_news.SetNewsBlocked(403))
    _poll(env, monkeypatch, _plus(THU_1700, 60), [])
    _poll(env, monkeypatch, _plus(THU_1700, 70), [])
    assert len(_texts(env, "✅")) == 1


def test_healthy_polls_never_write_the_flag(env, monkeypatch):
    _poll(env, monkeypatch, THU_1700, [])
    assert _outage() is None and _texts(env, "✅") == []


def test_recovery_clears_the_flag_before_broadcasting(env, monkeypatch):
    """ห้ามถือ digest_state ข้าม await — ระหว่างส่ง job อื่นเซฟ key ของตัวเอง
    ถ้าเซฟทีหลังด้วยสำเนาเก่า key นั้นหาย (เช่น scan_last_run = สแกน 17:30 ซ้ำ)"""
    _fail_until(env, monkeypatch, 3, set_news.SetNewsBlocked(403))

    async def racing_broadcast(bot, chat_ids, text):
        other = tb._load_digest_state()          # job อื่นทำงานตรงจังหวะ await พอดี
        other["scan_last_run"] = "2026-08-27"
        tb._save_digest_state(other)
        env.bot.sent.append((0, text))

    monkeypatch.setattr(tb, "_broadcast", racing_broadcast)
    _poll(env, monkeypatch, _plus(THU_1700, 60), [])
    state = tb._load_digest_state()
    assert state.get("scan_last_run") == "2026-08-27"    # ของ job อื่นต้องไม่ถูกกลืน
    assert "news_outage" not in state                    # และธงถูกล้างจริง


def test_recovery_sent_once_even_when_the_flag_cannot_be_cleared(env, monkeypatch, caplog):
    """ดิสก์เขียนไม่ได้ = ธงค้างบนดิสก์ตลอด — ห้ามยิง ✅ ซ้ำทุก 10 นาที"""
    _fail_until(env, monkeypatch, 3, set_news.SetNewsBlocked(403))
    monkeypatch.setattr(tb, "_save_digest_state", lambda state: False)
    env.bot.sent.clear()
    with caplog.at_level(logging.WARNING, logger="bot"):
        for i in range(3):
            _poll(env, monkeypatch, _plus(THU_1700, 60 + i * 10), [])
    assert len(_texts(env, "✅")) == 1
    assert any("news_outage" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_a_later_outage_can_announce_recovery_again(env, monkeypatch):
    _fail_until(env, monkeypatch, 3, set_news.SetNewsBlocked(403))
    _poll(env, monkeypatch, _plus(THU_1700, 60), [])              # ล่มรอบแรกจบ → ✅
    env.bot.sent.clear()
    for i in range(3):                                            # ล่มรอบใหม่ในโปรเซสเดิม
        _poll(env, monkeypatch, _plus(THU_1700, 70 + i * 10), RuntimeError("ล่มอีก"))
    _poll(env, monkeypatch, _plus(THU_1700, 200), [])
    assert len(_texts(env, "⚠️")) == 1 and len(_texts(env, "✅")) == 1


def test_failure_after_restart_resumes_the_count_from_the_flag(env, monkeypatch):
    """รีสตาร์ทกลางช่วงล่ม: ตัวนับ memory เริ่มที่ 1 แต่ธงบอกว่าล้มมา 16 รอบแล้ว
    ถ้าปล่อยให้ 1 ชนะ → fails เดินถอยหลัง และ backoff หล่นเป็น 0 อีกสามรอบ"""
    _fail_until(env, monkeypatch, 3, set_news.SetNewsBlocked(403))
    state = tb._load_digest_state()
    state["news_outage"]["fails"] = 16
    tb._save_digest_state(state)
    _reset_memory(monkeypatch)
    _poll(env, monkeypatch, _plus(THU_1700, 900), set_news.SetNewsBlocked(403))
    assert tb._news_fail_count == 16
    assert tb._load_digest_state()["news_outage"]["fails"] == 16
    assert tb._news_next_try == _plus(THU_1700, 960)     # เพดาน 60 นาทีทันที ไม่ใช่ 0
    _poll(env, monkeypatch, _plus(THU_1700, 1000), set_news.SetNewsBlocked(403))
    assert tb._news_fail_count == 17                     # แล้วเดินหน้าต่อตามปกติ


# ── สาเหตุล่าสุด: คำสั่ง "สถานะ" ต้องบอกได้ว่าล้มเพราะอะไร ──────


@pytest.mark.parametrize("err,reason", [
    (set_news.SetNewsBlocked(429), "Blocked HTTP 429"),
    (set_news.SetNewsChallenged("กันบอท"), "Challenged (WAF)"),
    (set_news.PlaywrightTimeout("Timeout"), f"Timeout {set_news.RESPONSE_TIMEOUT_S}s"),
    (RuntimeError("เว็บเปลี่ยนโครงสร้าง"), "RuntimeError"),
])
def test_failure_reason_is_remembered(env, monkeypatch, err, reason):
    _poll(env, monkeypatch, THU_1700, err)
    when, got = tb._news_last_error
    assert got == reason and when == THU_1700


def test_success_clears_the_last_error(env, monkeypatch):
    _poll(env, monkeypatch, THU_1700, RuntimeError("เว็บล่ม"))
    assert tb._news_last_error is not None
    _poll(env, monkeypatch, _plus(THU_1700, 10), [])
    assert tb._news_last_error is None
