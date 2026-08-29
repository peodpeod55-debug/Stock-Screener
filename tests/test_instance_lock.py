"""กันบอทรันซ้อน (instance_lock.py) — จอง TCP port บน localhost เป็น lock ระดับเครื่อง (port จากบอท US)

บอทไทยไม่เคยมี lock: รัน "เริ่ม Bot.bat" ซ้อน หรือรันสำเนา Stock-lab กับ Desktop พร้อมกัน
= สอง instance แย่ง getUpdates → telegram.error.Conflict (US เจอจริง 65 ครั้ง 2026-08-20)
ใช้ socket แทน lockfile: process ตาย (crash/kill) แล้ว OS คืน port เอง ไม่มี stale lock ค้าง
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.WARNING, handlers=[logging.NullHandler()])  # กัน basicConfig ของบอทเปิด bot_log.txt จริง

import instance_lock  # noqa: E402
import telegram_bot as tb  # noqa: E402

TEST_PORT = 48953               # คนละ port กับ LOCK_PORT จริง — กันชนบอทที่รันอยู่


def test_lock_port_does_not_collide_with_sibling_bots():
    assert instance_lock.LOCK_PORT == 48952      # US 48962 · HK 48972


def test_acquire_returns_lock():
    lock = instance_lock.acquire(port=TEST_PORT)
    try:
        assert lock is not None
    finally:
        lock.close()


def test_second_acquire_fails_while_held():
    first = instance_lock.acquire(port=TEST_PORT)
    try:
        assert instance_lock.acquire(port=TEST_PORT) is None
    finally:
        first.close()


def test_reacquire_after_release():
    first = instance_lock.acquire(port=TEST_PORT)
    first.close()
    second = instance_lock.acquire(port=TEST_PORT)
    try:
        assert second is not None
    finally:
        second.close()


def test_main_exits_code_3_without_building_app_when_lock_held(monkeypatch):
    """lock ถูกถือ → ออก code 3 ก่อนแตะ Telegram (เริ่ม Bot.bat จะได้ไม่วน restart)"""
    monkeypatch.setattr(tb, "BOT_TOKEN", "123:ABC")
    monkeypatch.setattr(tb.instance_lock, "acquire", lambda port=None: None)

    def must_not_build(*args, **kwargs):
        raise AssertionError("ต้องไม่สร้าง Application เมื่อ lock ถูกถือ")

    monkeypatch.setattr(tb, "ApplicationBuilder", must_not_build)
    with pytest.raises(SystemExit) as exc:
        tb.main()
    assert exc.value.code == tb.EXIT_ALREADY_RUNNING == 3
