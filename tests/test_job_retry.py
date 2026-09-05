# -*- coding: utf-8 -*-
"""job รายวันล้มต้องไม่เงียบ + catch-up ทุก 30 นาที (_run_guarded / catchup_job — แนวคิดจากบอท HK/US)

เดิม: build ล้ม → log.exception แล้ว return เงียบ · ไม่มีใครลองใหม่จนกว่าจะ restart · ผู้ใช้ไม่รู้ว่าทำไมวันนี้ไม่มีรายงาน
ใหม่: exception หลุดถึง _run_guarded → นับครั้ง + broadcast "จะลองใหม่ ~30 นาที (n/3)" → catchup_job เรียก
startup_*_job เดิมซ้ำ (มีเงื่อนไขเวลา + key รายวันครบอยู่แล้ว) เฉพาะตัวที่ยังไม่เคยทำวันนี้หรือเคยล้ม ·
ครบ 3 ครั้ง → แจ้งว่าหยุด แล้วไม่เรียก core อีกในวันนั้น
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

import telegram_bot as tb  # noqa: E402

BKK = ZoneInfo("Asia/Bangkok")
THU_0900 = datetime.datetime(2026, 8, 27, 9, 0, tzinfo=BKK)     # พฤหัส วันทำการ
THU_1800 = datetime.datetime(2026, 8, 27, 18, 0, tzinfo=BKK)    # หลังสแกน 17:30
STARTUP_JOBS = ["startup_heartbeat_job", "startup_digest_job", "startup_confirm_job",
                "startup_reminder_job", "startup_openpos_job", "startup_calendar_job",
                "startup_scan_job"]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text=None, **kwargs):
        self.sent.append((chat_id, text))


@pytest.fixture
def env(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(tb, "_load_chat_ids", lambda: [111, 222])
    tb._fail_counts.clear()
    tb._done.clear()
    return SimpleNamespace(bot=bot, ctx=SimpleNamespace(bot=bot))


def _run(coro):
    return asyncio.run(coro)


def _freeze(monkeypatch, when):
    """ให้ datetime.datetime.now(tz) ในบอทคืนเวลาที่กำหนด (core มีเงื่อนไขวันทำการ/เวลา)"""
    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(tb, "datetime", SimpleNamespace(
        datetime=FakeDT, date=datetime.date, time=datetime.time, timedelta=datetime.timedelta))


def _today():
    return datetime.datetime.now(BKK).date().isoformat()


def test_failure_notifies_every_chat_and_counts(env, caplog):
    async def core():
        raise RuntimeError("Yahoo down")

    with caplog.at_level(logging.INFO, logger="bot"):
        _run(tb._run_guarded(env.ctx, "digest", "สรุปงบเช้า", core))
    assert [cid for cid, _ in env.bot.sent] == [111, 222]
    text = env.bot.sent[0][1]
    assert "สรุปงบเช้า" in text and "Yahoo down" in text and "ลองใหม่" in text and "1/3" in text
    assert tb._fail_counts[("digest", _today())] == 1
    # ล้ม → ต้องไม่มีบรรทัด "... สำเร็จ" (มีแต่ตอนจบไม่มี exception เท่านั้น)
    assert not any(r.getMessage().endswith("สำเร็จ") for r in caplog.records if r.name == "bot")


def test_gives_up_after_max_tries_then_skips_core(env):
    calls = []

    async def core():
        calls.append(1)
        raise RuntimeError("x")

    for _ in range(tb.JOB_MAX_TRIES):
        _run(tb._run_guarded(env.ctx, "digest", "สรุปงบเช้า", core))
    assert "หยุดลอง" in env.bot.sent[-1][1]
    n_msgs = len(env.bot.sent)
    _run(tb._run_guarded(env.ctx, "digest", "สรุปงบเช้า", core))
    assert len(calls) == tb.JOB_MAX_TRIES        # ครั้งที่ 4 ไม่เรียก core
    assert len(env.bot.sent) == n_msgs           # และไม่สแปมซ้ำ


def test_success_logs_one_line_and_marks_done_but_stays_silent_on_telegram(env, caplog):
    """สำเร็จ = เงียบฝั่ง Telegram (ไม่มีอะไรผิดปกติให้แจ้ง) แต่ต้อง log INFO 1 บรรทัด
    ให้ watchdog ภายนอก/คนอ่าน bot_log.txt เห็นว่า job นี้จบจริง"""
    async def core():
        pass

    with caplog.at_level(logging.INFO, logger="bot"):
        _run(tb._run_guarded(env.ctx, "digest", "สรุปงบเช้า", core))
    assert env.bot.sent == []
    assert tb._done[("digest", _today())] is True
    records = [r for r in caplog.records if r.name == "bot"]
    assert len(records) == 1
    assert records[0].getMessage() == "สรุปงบเช้า สำเร็จ"


def test_success_after_failure_clears_fail_count(env):
    state = {"fail": True}

    async def core():
        if state["fail"]:
            raise RuntimeError("ครั้งแรกล้ม")

    _run(tb._run_guarded(env.ctx, "digest", "สรุปงบเช้า", core))
    state["fail"] = False
    _run(tb._run_guarded(env.ctx, "digest", "สรุปงบเช้า", core))
    assert ("digest", _today()) not in tb._fail_counts
    assert tb._done[("digest", _today())] is True


def test_digest_build_error_reaches_user_and_keeps_day_open(env, monkeypatch):
    """พฤติกรรมใหม่: build_morning_digest โยน → ไม่เงียบอีกต่อไป และ last_sent ไม่ถูกเขียน"""
    _freeze(monkeypatch, THU_0900)

    def boom(*args, **kwargs):
        raise RuntimeError("parquet เสีย")

    monkeypatch.setattr(tb, "build_morning_digest", boom)
    _run(tb.morning_digest_job(env.ctx))
    assert any("parquet เสีย" in t for _, t in env.bot.sent)
    assert tb._load_digest_state().get("last_sent") != THU_0900.date().isoformat()
    assert tb._digest_in_flight is False        # flag ถูกปลดแม้ล้ม


def test_scan_error_reaches_user_and_keeps_day_open(env, monkeypatch):
    _freeze(monkeypatch, THU_1800)

    def boom():
        raise RuntimeError("Yahoo 429")

    monkeypatch.setattr(tb, "run_best_scan", boom)
    _run(tb.daily_scan_job(env.ctx))
    assert any("Yahoo 429" in t and "สแกน" in t for _, t in env.bot.sent)
    assert tb._load_digest_state().get("scan_last_run") != THU_1800.date().isoformat()
    assert tb._scan_in_flight is False


def _fake_startup_jobs(monkeypatch, calls):
    for name in STARTUP_JOBS + ["startup_catchup_job"]:
        async def fake(ctx, _n=name):
            calls.append(_n)
        monkeypatch.setattr(tb, name, fake)


def test_catchup_runs_daily_startup_jobs_in_order_not_news(env, monkeypatch):
    calls = []
    _fake_startup_jobs(monkeypatch, calls)
    _run(tb.catchup_job(env.ctx))
    assert calls == STARTUP_JOBS                 # เก็บตกข่าว (startup_catchup_job) ไม่อยู่ในรอบ 30 นาที


def test_catchup_skips_done_today_but_retries_failed(env, monkeypatch, caplog):
    calls = []
    _fake_startup_jobs(monkeypatch, calls)
    tb._done[("digest", _today())] = True                       # ทำไปแล้ววันนี้ → ข้าม
    tb._done[("scan", _today())] = True
    tb._fail_counts[("scan", _today())] = 1                     # เคยล้ม → ลองใหม่
    with caplog.at_level(logging.INFO, logger="bot"):
        _run(tb.catchup_job(env.ctx))
    assert "startup_digest_job" not in calls
    assert "startup_scan_job" in calls
    assert "startup_heartbeat_job" in calls                     # ไม่มี build ไม่เข้า _done — เรียกเสมอ (ถูกกันด้วย key)
    msgs = [r.getMessage() for r in caplog.records if r.name == "bot"]
    assert "catch-up: retrying failed job scan" in msgs         # เคยล้ม (_fail_counts มี key) → log ว่ากำลังลองใหม่
    assert "catch-up: retrying failed job digest" not in msgs   # _done แล้ว ไม่ควรมีบรรทัดนี้
    # heartbeat ไม่เคยล้ม แค่ยังไม่ถึงเวลา (ไม่มี build ไม่เข้า _done เรียกทุกรอบ) — ไม่ log
    # (ไม่งั้นสแปม ~55 บรรทัด/วัน ก่อน scan/openpos ถึงเวลา — heartbeat/reminder/digest/confirm/calendar เอง)
    assert not any(m.startswith("catch-up:") for m in msgs if "scan" not in m)


def test_main_registers_catchup_every_30_min(monkeypatch):
    monkeypatch.setattr(tb, "BOT_TOKEN", "123:ABC")
    monkeypatch.setattr(tb.instance_lock, "acquire", lambda port=None: object())
    repeating = []
    jq = SimpleNamespace(run_daily=lambda *a, **k: None, run_once=lambda *a, **k: None,
                         run_repeating=lambda cb, **k: repeating.append((cb, k)))
    app = SimpleNamespace(add_handler=lambda *a, **k: None, add_error_handler=lambda *a, **k: None,
                          job_queue=jq, run_polling=lambda **k: None)

    class Builder:
        def token(self, *_):
            return self

        def post_init(self, *_):
            return self

        def build(self):
            return app

    monkeypatch.setattr(tb, "ApplicationBuilder", Builder)
    tb.main()
    entry = [k for cb, k in repeating if cb is tb.catchup_job]
    assert len(entry) == 1
    assert entry[0]["interval"] == tb.CATCHUP_INTERVAL == 30 * 60
    assert entry[0]["first"] >= 450              # หลัง startup burst (startup_scan_job วินาที 450) จบก่อน
