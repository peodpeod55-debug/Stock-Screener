# -*- coding: utf-8 -*-
"""คำสั่ง "สถานะ"/"สรุปงบ" ต้อง build นอก event loop + latest_scan อ่านเฉพาะท้ายไฟล์
(improvement review 31 ส.ค. §4 ข้อ 3 — job path ใช้ to_thread ถูกอยู่แล้ว แต่ path
คำสั่งมือ build sync บน event loop: ไฟล์ใหญ่ขึ้นทุกวัน = บอทค้างทั้งตัวระหว่างอ่าน)
"""
import asyncio
import datetime
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_status          # noqa: E402
import telegram_bot as tb  # noqa: E402


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _dispatch(monkeypatch, text):
    monkeypatch.setattr(tb, "_register_chat", lambda chat_id: None)
    upd = SimpleNamespace(message=FakeMessage(text),
                          effective_chat=SimpleNamespace(id=4242))
    asyncio.run(tb._dispatch_text(upd, SimpleNamespace(bot=None, args=[]), text))
    return upd.message.replies


def test_status_command_builds_off_event_loop(monkeypatch):
    seen = {}

    def fake_build(cid):
        seen["thread"] = threading.current_thread()
        return "รายงานสถานะทดสอบ"

    monkeypatch.setattr(tb, "build_status_report", fake_build)
    replies = _dispatch(monkeypatch, "สถานะ")
    assert replies and "รายงานสถานะทดสอบ" in replies[0]
    assert seen["thread"] is not threading.main_thread(), \
        "build_status_report อ่านไฟล์ (scan_log/bot_log) — ต้องวิ่งใน to_thread ไม่บล็อกบอท"


def test_digest_command_builds_off_event_loop(monkeypatch):
    seen = {}

    def fake_build(since, now, window_label=None):
        seen["thread"] = threading.current_thread()
        return "สรุปงบทดสอบ", []

    monkeypatch.setattr(tb, "build_morning_digest", fake_build)
    replies = _dispatch(monkeypatch, "สรุปงบ")
    assert replies and "สรุปงบทดสอบ" in replies[0]
    assert seen["thread"] is not threading.main_thread(), \
        "build_morning_digest อ่าน filings/results CSV — ต้องวิ่งใน to_thread เหมือน job path"


def test_latest_scan_reads_only_file_tail(tmp_path):
    p = tmp_path / "scan_log.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        f.write("scan_date,ticker,score\n")
        for i in range(300):
            f.write(f"2026-08-27,OLD{i}.BK,7\n")
        f.write("2026-08-28,X1.BK,9\n2026-08-28,X2,8\n")
    out = bot_status.latest_scan(str(p), tail_bytes=200)
    assert out == {"date": "2026-08-28", "tickers": ["X1", "X2"]}, \
        "อ่านท้ายไฟล์พอ (แถวแรกที่โดนตัดกลางบรรทัดต้องถูกทิ้ง ไม่พังทั้งฟังก์ชัน)"


def test_latest_scan_default_still_correct_on_small_file(tmp_path):
    p = tmp_path / "scan_log.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        f.write("scan_date,ticker,score\n2026-08-28,AOT.BK,9\n")
    assert bot_status.latest_scan(str(p)) == {"date": "2026-08-28", "tickers": ["AOT"]}
