# -*- coding: utf-8 -*-
"""คำสั่ง "วิเคราะห์"/"ta", "คำอธิบายงบ"/"mda", ปุ่ม 📐/📅 และ callback ta:SYM ใน telegram_bot.py
ใช้ object ปลอมแทน Update/Message ของ Telegram — ไม่ต่อ Telegram ไม่แตะเน็ต"""
import asyncio
import datetime
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# telegram_bot เรียก logging.basicConfig ตอน import → เปิด bot_log.txt ของจริง
# ตั้ง handler ให้ root ก่อน = basicConfig ของบอทกลายเป็น no-op ไม่แตะไฟล์ log จริง
logging.basicConfig(level=logging.WARNING, handlers=[logging.NullHandler()])

import dashboard_feed  # noqa: E402
import scanner  # noqa: E402
import telegram_bot as tb  # noqa: E402
from telegram import InlineKeyboardMarkup  # noqa: E402

AOT = {"t": "AOT", "n": "Airports of Thailand Public Company Limited", "g": "TH", "c": 65.0,
       "sma20": 63.525, "sma50": 62.225, "sma150": 55.9483, "sma200": 53.5813, "e20v": 63.423,
       "e50v": 61.743, "h52": 65.0, "l52": 34.475, "sh30": 65.0, "sl30": 60.5, "vr50": 2.15,
       "vmax3": [51, 3.98], "rsi": 63.1, "macdv": [0.39916, 0.470523, -0.0713627], "atrc": 1.08152,
       "atrp": 1.66, "rs": 86, "r1m": 1.17, "r3m": 23.2, "ytd": 22.6, "r1y": 62.9, "st": 5,
       "bis": 2, "stw": 2, "scn": [1, 1, 0, 1]}


def meta(**over):
    m = {"asof": "2026-08-14", "core_asof": "2026-08-14", "indexName": "SET Index",
         "intraday": False, "bench": {"close": [1500.0] * 70}, "source": "local",
         "build_id": "18f8448e0b5402633ce2", "age_days": 1, "stale": False}
    m.update(over)
    return m


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []            # [(text, kwargs)]

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


class FakeChat:
    id = 4242


class FakeUpdate:
    def __init__(self, text):
        self.message = FakeMessage(text)
        self.effective_chat = FakeChat()


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


class FakeCallbackUpdate:
    def __init__(self, data):
        self.callback_query = FakeQuery(data)
        self.effective_chat = FakeChat()


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(tb, "_register_chat", lambda chat_id: None)      # ไม่เขียน chat_ids.json จริง
    monkeypatch.setattr(tb.stock_core, "next_earnings_date", lambda sym: None)

    def no_lookup(*a, **k):
        raise AssertionError("ต้องไม่ตกไปโหมดดูหุ้น (build_message)")
    monkeypatch.setattr(tb, "build_message", no_lookup)


def _buttons(markup):
    """[[(text, callback_data, url), …], …]"""
    assert isinstance(markup, InlineKeyboardMarkup)
    return [[(b.text, b.callback_data, b.url) for b in row] for row in markup.inline_keyboard]


# ── ปุ่ม ─────────────────────────────────────────────────────

def test_watch_buttons_give_three_buttons_per_stock_row():
    rows = _buttons(tb._watch_buttons([{"ticker": "AOT.BK"}, "PTT"]))
    assert rows == [
        [("➕ AOT", "watch:AOT", None), ("📐 AOT", "ta:AOT", None),
         ("📅 AOT", None, "https://earningsradar.pages.dev/company/AOT/")],
        [("➕ PTT", "watch:PTT", None), ("📐 PTT", "ta:PTT", None),
         ("📅 PTT", None, "https://earningsradar.pages.dev/company/PTT/")],
    ]


def test_watch_buttons_empty_and_limit():
    assert tb._watch_buttons([]) is None
    assert tb._watch_buttons(None) is None
    rows = _buttons(tb._watch_buttons([f"S{i}" for i in range(20)]))
    assert len(rows) == 15


def test_ta_rows_three_per_row():
    rows = tb._ta_rows(["AOT", "PTT", "CPALL", "BH"])
    got = [[(b.text, b.callback_data) for b in row] for row in rows]
    assert got == [[("📐 AOT", "ta:AOT"), ("📐 PTT", "ta:PTT"), ("📐 CPALL", "ta:CPALL")],
                   [("📐 BH", "ta:BH")]]
    assert tb._ta_rows([]) == []


def test_earnings_url_quotes_symbol():
    assert tb._earnings_url("F&D") == "https://earningsradar.pages.dev/company/F%26D/"
    assert tb._earnings_url("AOT") == "https://earningsradar.pages.dev/company/AOT/"


# ── _ta_reply ─────────────────────────────────────────────────

def test_ta_reply_sends_pre_block_and_gem_button(monkeypatch):
    calls = {}

    def fake_load(sym, market="TH", **kw):
        calls["args"] = (sym, market, kw)
        return dict(AOT), meta()
    monkeypatch.setattr(dashboard_feed, "load_stock", fake_load)
    msg = FakeMessage()
    asyncio.run(tb._ta_reply(msg, "aot.bk"))

    assert calls["args"][0] == "AOT" and calls["args"][1] == "TH"
    kw = calls["args"][2]
    assert kw["site_dir"] == tb.DASHBOARD_SITE_DIR
    assert kw["url"] == tb.DASHBOARD_URL
    assert kw["max_age_days"] == scanner.LOCAL_MAX_AGE_DAYS

    assert len(msg.replies) == 1
    text, rkw = msg.replies[0]
    assert rkw["parse_mode"] == "HTML"
    assert text.startswith("📐 ข้อมูล PHASE 0 · <b>AOT</b> (ข้อมูล 2026-08-14 · ปิดตลาดแล้ว)")
    assert "⚠️ ข้อมูลเก่า" not in text
    assert "<pre>" in text and "</pre>" in text
    body = text.split("<pre>", 1)[1].split("</pre>", 1)[0]
    assert body.startswith("วิเคราะห์เทคนิค AOT (Airports of Thailand Public Company Limited) ตาม prompt")
    assert "ข.8 วันประกาศงบถัดไป: ไม่มีข้อมูล" in body
    assert "RS Rating 86/99" in body
    assert len(text) < tb.TG_LIMIT
    assert _buttons(rkw["reply_markup"]) == [[("📐 เปิด Gem เทคนิค", None, tb.GEM_TA_URL)]]


def test_ta_reply_escapes_html_inside_pre(monkeypatch):
    s = dict(AOT, n="A <b>&</b> Co")
    monkeypatch.setattr(dashboard_feed, "load_stock", lambda *a, **k: (s, meta()))
    msg = FakeMessage()
    asyncio.run(tb._ta_reply(msg, "AOT"))
    text = msg.replies[0][0]
    assert "A &lt;b&gt;&amp;&lt;/b&gt; Co" in text
    assert "<pre>" in text and text.count("<b>") == 1     # <b> เดียวคือชื่อหุ้นในหัวข้อความ


def test_ta_reply_uses_next_earnings_date_from_store(monkeypatch):
    monkeypatch.setattr(dashboard_feed, "load_stock", lambda *a, **k: (dict(AOT), meta()))
    monkeypatch.setattr(tb.stock_core, "next_earnings_date",
                        lambda sym: datetime.date(2026, 11, 12) if sym == "AOT" else None)
    msg = FakeMessage()
    asyncio.run(tb._ta_reply(msg, "AOT"))
    assert "ข.8 วันประกาศงบถัดไป: 2026-11-12" in msg.replies[0][0]


def test_ta_reply_marks_stale_data_and_intraday(monkeypatch):
    monkeypatch.setattr(dashboard_feed, "load_stock",
                        lambda *a, **k: (dict(AOT), meta(intraday=True, age_days=9, stale=True)))
    msg = FakeMessage()
    asyncio.run(tb._ta_reply(msg, "AOT"))
    text = msg.replies[0][0]
    assert "(ข้อมูล 2026-08-14 · ระหว่างวัน)" in text
    assert "⚠️ ข้อมูลเก่า 9 วัน" in text
    assert "<pre>" in text                                   # ยังส่งให้ ไม่ปฏิเสธ


def test_ta_reply_dashboard_unavailable_explains(monkeypatch):
    def boom(*a, **k):
        raise dashboard_feed.DashboardUnavailable("อ่าน payload ของ dashboard ไม่ได้ — ลองแล้ว: X · Y")
    monkeypatch.setattr(dashboard_feed, "load_stock", boom)
    msg = FakeMessage()
    asyncio.run(tb._ta_reply(msg, "AOT"))
    text = msg.replies[0][0]
    assert text.startswith("⚠️") and "X · Y" in text
    assert "<pre>" not in text and "reply_markup" not in msg.replies[0][1]


def test_ta_reply_unknown_symbol_says_not_in_dashboard(monkeypatch):
    monkeypatch.setattr(dashboard_feed, "load_stock", lambda *a, **k: (None, meta()))
    msg = FakeMessage()
    asyncio.run(tb._ta_reply(msg, "ZZZZ"))
    text = msg.replies[0][0]
    assert "ZZZZ" in text and "dashboard" in text and "2026-08-14" in text
    assert "<pre>" not in text


def test_ta_reply_rejects_malformed_symbol(monkeypatch):
    monkeypatch.setattr(dashboard_feed, "load_stock",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ต้องไม่ถูกเรียก")))
    msg = FakeMessage()
    asyncio.run(tb._ta_reply(msg, "???"))
    assert "วิเคราะห์ AOT" in msg.replies[0][0]


# ── handle_text: คำสั่งใหม่ 2 ตัว ─────────────────────────────

def test_handle_text_ta_command_calls_ta_reply(monkeypatch):
    seen = []

    async def fake_ta_reply(message, raw):
        seen.append((message, raw))
    monkeypatch.setattr(tb, "_ta_reply", fake_ta_reply)
    for text in ("วิเคราะห์ AOT", "ta aot", "TA AOT"):
        upd = FakeUpdate(text)
        asyncio.run(tb.handle_text(upd, None))
        assert seen[-1] == (upd.message, text.split()[1])
    assert len(seen) == 3


def test_handle_text_ta_without_symbol_shows_usage():
    for text in ("วิเคราะห์", "ta"):
        upd = FakeUpdate(text)
        asyncio.run(tb.handle_text(upd, None))
        assert "วิเคราะห์ AOT" in upd.message.replies[0][0]


def test_handle_text_mda_command_gives_earnings_radar_button():
    upd = FakeUpdate("mda ace.bk")
    asyncio.run(tb.handle_text(upd, None))
    text, kw = upd.message.replies[0]
    assert "ACE" in text and kw["parse_mode"] == "HTML"
    assert _buttons(kw["reply_markup"]) == [
        [("📅 เปิด Earnings Radar — ACE", None, "https://earningsradar.pages.dev/company/ACE/")]]


def test_handle_text_mda_without_symbol_shows_usage():
    for text in ("คำอธิบายงบ", "mda"):
        upd = FakeUpdate(text)
        asyncio.run(tb.handle_text(upd, None))
        assert "คำอธิบายงบ AOT" in upd.message.replies[0][0]


# ── on_button: callback ta:SYM ─────────────────────────────────

def test_on_button_ta_callback_answers_first_then_replies(monkeypatch):
    order = []

    async def fake_ta_reply(message, raw):
        order.append(("reply", message, raw))
    monkeypatch.setattr(tb, "_ta_reply", fake_ta_reply)
    upd = FakeCallbackUpdate("ta:AOT")
    q = upd.callback_query
    orig_answer = q.answer

    async def answer(text=None, **kw):
        order.append(("answer", text))
        await orig_answer(text, **kw)
    q.answer = answer
    asyncio.run(tb.on_button(upd, None))
    assert order[0][0] == "answer"
    assert order[1] == ("reply", q.message, "AOT")


def test_on_button_ta_callback_swallows_reply_errors(monkeypatch):
    async def bad(message, raw):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(tb, "_ta_reply", bad)
    upd = FakeCallbackUpdate("ta:AOT")
    asyncio.run(tb.on_button(upd, None))          # ต้องไม่ระเบิดออกมา (กันบอทตาย)
    assert upd.callback_query.answers


# ── help ─────────────────────────────────────────────────────

def test_help_mentions_new_commands_and_uses_reply_long(monkeypatch):
    sent = []

    async def fake_reply_long(message, text, reply_markup=None, **kw):
        sent.append(text)
    monkeypatch.setattr(tb, "_reply_long", fake_reply_long)
    upd = FakeUpdate("/help")
    asyncio.run(tb.cmd_help(upd, None))
    assert sent and "วิเคราะห์ AOT" in sent[0] and "คำอธิบายงบ AOT" in sent[0]
    assert "<code>ta AOT</code>" in sent[0] and "<code>mda AOT</code>" in sent[0]
    assert upd.message.replies == []               # ไม่ได้ส่งตรงผ่าน reply_text แล้ว
