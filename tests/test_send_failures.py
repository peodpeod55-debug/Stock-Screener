# -*- coding: utf-8 -*-
"""ส่งล้มทุก chat ต้องไม่ถูกนับว่า "ทำแล้ว" (improvement review 31 ส.ค. §1.4)

เดิม: exception ตอนส่งถูกกลืนใน loop ต่อ chat → core จบปกติ → _run_guarded จด _done
→ catch-up ข้ามทั้งวัน + "สถานะ" โชว์ "✅ ทำแล้ว ไม่มีอะไรส่ง" ทั้งที่รายงานหายเงียบ
ใหม่: มีของจะส่ง + ส่งไม่ถึงสักแชท → raise ให้ _run_guarded นับครั้ง/แจ้ง/ให้ catch-up ลองใหม่
(ส่งถึงบางแชท = log พอ — retry จะสแปมแชทที่ได้ไปแล้ว)
"""
import asyncio
import datetime
import os
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_bot as tb  # noqa: E402

BKK = ZoneInfo("Asia/Bangkok")
THU_0900 = datetime.datetime(2026, 8, 27, 9, 0, tzinfo=BKK)   # พฤหัส วันทำการ
THU_1800 = datetime.datetime(2026, 8, 27, 18, 0, tzinfo=BKK)  # หลังสแกน 17:30


class FlakyBot:
    """ส่งสำเร็จเฉพาะ chat ที่ไม่อยู่ใน fail_chats — ที่เหลือโยนเหมือน Telegram ล่ม"""

    def __init__(self, fail_chats=()):
        self.fail_chats = set(fail_chats)
        self.sent = []

    async def send_message(self, chat_id, text=None, **kwargs):
        if chat_id in self.fail_chats:
            raise RuntimeError("Telegram timeout")
        self.sent.append((chat_id, text))


def _run(coro):
    return asyncio.run(coro)


def _freeze(monkeypatch, when):
    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(tb, "datetime", SimpleNamespace(
        datetime=FakeDT, date=datetime.date, time=datetime.time, timedelta=datetime.timedelta))


THU = THU_0900.date().isoformat()   # วันตามเวลาที่ freeze — _job_key ใช้ตัวนี้ ไม่ใช่วันจริง


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(tb, "_load_chat_ids", lambda: [111, 222])
    tb._fail_counts.clear()
    tb._done.clear()
    yield SimpleNamespace()
    tb._fail_counts.clear()
    tb._done.clear()


def _ctx(fail_chats=()):
    return SimpleNamespace(bot=FlakyBot(fail_chats))


def _assert_counted_as_failed(job_name, state_key):
    assert tb._fail_counts.get((job_name, THU)) == 1, \
        f"{job_name}: ส่งล้มทุกแชทต้องนับเป็นล้ม 1 ครั้ง"
    assert (job_name, THU) not in tb._done, \
        f"{job_name}: ต้องไม่ถูกจด _done (ไม่งั้น catch-up ไม่ลองใหม่)"
    assert tb._load_digest_state().get(state_key) != THU, \
        f"{job_name}: key รายวันต้องไม่ถูกเขียน"


def test_digest_all_sends_fail_counts_as_failure(env, monkeypatch):
    _freeze(monkeypatch, THU_0900)
    monkeypatch.setattr(tb, "build_morning_digest",
                        lambda since, now, **k: ("สรุปงบทดสอบ", []))
    _run(tb.morning_digest_job(_ctx(fail_chats=[111, 222])))
    _assert_counted_as_failed("digest", "last_sent")


def test_digest_partial_send_still_counts_as_sent(env, monkeypatch):
    _freeze(monkeypatch, THU_0900)
    monkeypatch.setattr(tb, "build_morning_digest",
                        lambda since, now, **k: ("สรุปงบทดสอบ", []))
    ctx = _ctx(fail_chats=[111])
    _run(tb.morning_digest_job(ctx))
    assert tb._load_digest_state().get("last_sent") == THU
    assert ("digest", THU) not in tb._fail_counts
    assert [cid for cid, _ in ctx.bot.sent] == [222]


def test_confirm_all_sends_fail_counts_as_failure(env, monkeypatch):
    _freeze(monkeypatch, THU_0900)
    monkeypatch.setattr(tb, "build_morning_confirm",
                        lambda now: ("ยืนยันทดสอบ", []))
    _run(tb.morning_confirm_job(_ctx(fail_chats=[111, 222])))
    _assert_counted_as_failed("confirm", "confirm_last_sent")


def test_calendar_all_sends_fail_counts_as_failure(env, monkeypatch):
    _freeze(monkeypatch, THU_0900)
    monkeypatch.setattr(tb, "build_earnings_calendar", lambda cid: "ปฏิทินทดสอบ")
    _run(tb.calendar_job(_ctx(fail_chats=[111, 222])))
    assert tb._fail_counts.get(("calendar", THU)) == 1
    assert ("calendar", THU) not in tb._done
    anchor = tb._last_sunday(THU_0900.date()).isoformat()
    assert tb._load_digest_state().get("calendar_last_sent") != anchor


def test_scan_all_sends_fail_counts_as_failure(env, monkeypatch):
    _freeze(monkeypatch, THU_1800)
    monkeypatch.setattr(tb, "run_best_scan", lambda: ([], 0, "", [], None, []))
    monkeypatch.setattr(tb.shadow, "update_shadow", lambda: None)
    _run(tb.daily_scan_job(_ctx(fail_chats=[111, 222])))
    _assert_counted_as_failed("scan", "scan_last_run")
    assert tb._scan_in_flight is False


def test_reminder_all_sends_fail_counts_as_failure(env, monkeypatch):
    """ของเดิม raise เฉพาะ build ล้ม — build สำเร็จแต่ส่งไม่ถึงใครยังหลุดเป็น "ทำแล้ว" """
    _freeze(monkeypatch, THU_0900)
    monkeypatch.setattr(tb, "build_earnings_reminder", lambda cid: "เตือนวันงบทดสอบ")
    _run(tb.earnings_reminder_job(_ctx(fail_chats=[111, 222])))
    _assert_counted_as_failed("reminder", "remind_last_sent")


def test_openpos_all_sends_fail_counts_as_failure(env, monkeypatch):
    _freeze(monkeypatch, THU_1800)
    monkeypatch.setattr(tb, "build_open_positions_report", lambda cid: "ไม้เปิดทดสอบ")
    _run(tb.open_positions_job(_ctx(fail_chats=[111, 222])))
    _assert_counted_as_failed("openpos", "openpos_last_sent")
