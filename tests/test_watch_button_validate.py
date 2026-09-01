# -*- coding: utf-8 -*-
"""ปุ่ม ➕ (callback watch:) ต้อง validate เหมือนคำสั่ง "ติดตาม" (improvement review 31 ส.ค. §4 ข้อ 1)

เดิม callback เพิ่มเข้าลิสต์ทันทีไม่เช็คอะไร → REIT/หุ้นเล็กจากสรุปงบที่ Yahoo
ไม่มีข้อมูล ค้างในลิสต์ให้ watch job ไล่ดึงทุก 15 นาทีโดยไม่มีวันสำเร็จ
ใหม่: ใช้ validate กลางร่วมกับคำสั่ง — no-data = ปฏิเสธ · Yahoo ล่ม = รับไว้ก่อน (เหมือนเดิม)
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_core          # noqa: E402
import telegram_bot as tb  # noqa: E402

CHAT = 111


class FakeMsg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeQ:
    def __init__(self, data):
        self.data = data
        self.message = FakeMsg()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text or "")


def _press(data):
    q = FakeQ(data)
    upd = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=CHAT))
    asyncio.run(tb.on_button(upd, SimpleNamespace(bot=None)))
    return q


def test_watch_button_rejects_ticker_without_data(monkeypatch):
    monkeypatch.setattr(tb.stock_core, "get_stock_data", lambda sym, *a, **k: None)
    q = _press("watch:WXYZ")
    assert "WXYZ" not in stock_core.get_watchlist(CHAT), \
        "หุ้น no-data ต้องไม่เข้าลิสต์ (เดิมค้างให้ watch job ดึงล้มทุก 15 นาที)"
    feedback = " ".join(q.answers + q.message.replies)
    assert "ไม่พบข้อมูล" in feedback


def test_watch_button_accepts_when_yahoo_is_down(monkeypatch):
    def boom(sym, *a, **k):
        raise RuntimeError("Yahoo 429")

    monkeypatch.setattr(tb.stock_core, "get_stock_data", boom)
    q = _press("watch:AOT")
    assert "AOT" in stock_core.get_watchlist(CHAT), \
        "Yahoo ล่มชั่วคราวต้องไม่ขวางการเก็บเข้าลิสต์ (กติกาเดียวกับคำสั่ง)"
    assert any("เพิ่ม" in t for t in q.answers + q.message.replies)


def test_watch_button_adds_valid_ticker(monkeypatch):
    monkeypatch.setattr(tb.stock_core, "get_stock_data",
                        lambda sym, *a, **k: {"symbol": sym})
    q = _press("watch:PTT")
    assert "PTT" in stock_core.get_watchlist(CHAT)
    assert any("เพิ่ม" in t for t in q.answers + q.message.replies)
