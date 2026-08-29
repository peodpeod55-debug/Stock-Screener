# -*- coding: utf-8 -*-
"""เมนูคำสั่ง "/" ใน Telegram (setMyCommands) + slash alias → dispatcher ข้อความเดิม (fork เทส HK)

เมนู "/" ของแอป Telegram รับได้เฉพาะคำสั่ง slash — คำสั่งพิมพ์ไทย/อังกฤษเดิมทั้งหมด
จึงได้ alias slash ชื่อเดียวกับ alias อังกฤษ ส่งเข้า _dispatch_text เดิม ไม่มี logic ซ้ำ
ลงทะเบียนตอน start (post_init) ล้มไม่ทำให้บอทตาย — ใช้ object ปลอม ไม่ต่อ Telegram ไม่แตะเน็ต"""
import asyncio
import logging
import os
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# telegram_bot เรียก logging.basicConfig ตอน import → เปิด bot_log.txt ของจริง
# ตั้ง handler ให้ root ก่อน = basicConfig ของบอทกลายเป็น no-op ไม่แตะไฟล์ log จริง
logging.basicConfig(level=logging.WARNING, handlers=[logging.NullHandler()])

import telegram_bot as tb  # noqa: E402
from telegram import BotCommand  # noqa: E402


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


def _ctx(args=None):
    return SimpleNamespace(bot=None, args=args or [])


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(tb, "_register_chat", lambda chat_id: None)      # ไม่เขียน chat_ids.json จริง


# ── เมนู ─────────────────────────────────────────────────────────

def test_menu_entries_follow_telegram_limits():
    names = [c for c, _ in tb.BOT_COMMANDS]
    assert len(names) == len(set(names))
    for name, desc in tb.BOT_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name
        assert 3 <= len(desc) <= 256, name


def test_menu_lists_every_feature():
    names = [c for c, _ in tb.BOT_COMMANDS]
    assert names[:2] == ["scan", "list"]
    assert {"scan", "news", "digest", "confirm", "list", "watch", "unwatch",
            "calendar", "earn", "stats", "shadow", "port", "size", "buy", "sell",
            "trades", "ta", "mda", "price", "help"} == set(names)


def test_every_alias_is_in_menu_and_help_has_direct_handler():
    menu = {c for c, _ in tb.BOT_COMMANDS}
    assert set(tb.SLASH_ALIASES) <= menu
    # ทุกรายการในเมนูต้องมีทางเข้า: alias ทั้งหมด ยกเว้น help (CommandHandler ตรงใน main)
    assert menu - set(tb.SLASH_ALIASES) == {"help"}


def test_register_commands_sends_menu_to_telegram():
    sent = []

    async def set_my_commands(cmds):
        sent.append(cmds)
    app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_my_commands))
    asyncio.run(tb.register_commands(app))
    assert sent == [[BotCommand(c, d) for c, d in tb.BOT_COMMANDS]]


def test_register_commands_failure_is_logged_not_raised(caplog):
    async def set_my_commands(cmds):
        raise RuntimeError("network down")
    app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_my_commands))
    with caplog.at_level(logging.WARNING, logger="bot"):
        asyncio.run(tb.register_commands(app))
    assert "network down" in caplog.text


# ── slash alias → _dispatch_text เดิม ─────────────────────────────

def _run_alias(monkeypatch, text, dispatched):
    async def fake_dispatch(update, context, t):
        dispatched.append(t)
    monkeypatch.setattr(tb, "_dispatch_text", fake_dispatch)
    upd = FakeUpdate(text)
    asyncio.run(tb.cmd_alias(upd, _ctx(text.split()[1:])))
    return upd


def test_alias_joins_command_word_with_args(monkeypatch):
    dispatched = []
    _run_alias(monkeypatch, "/watch AOT PTT", dispatched)
    _run_alias(monkeypatch, "/scan", dispatched)
    _run_alias(monkeypatch, "/earn AOT 13/11/2569", dispatched)
    assert dispatched == ["watch AOT PTT", "scan", "earn AOT 13/11/2569"]


def test_alias_with_bot_username_suffix(monkeypatch):
    dispatched = []
    _run_alias(monkeypatch, "/watch@SET_Screener_bot AOT", dispatched)
    assert dispatched == ["watch AOT"]


def test_price_alias_passes_bare_tickers(monkeypatch):
    dispatched = []
    _run_alias(monkeypatch, "/price AOT PTT", dispatched)
    assert dispatched == ["AOT PTT"]


def test_price_alias_without_ticker_shows_usage(monkeypatch):
    dispatched = []
    upd = _run_alias(monkeypatch, "/price", dispatched)
    assert dispatched == []
    assert "/price AOT" in upd.message.replies[0][0]


def test_unknown_slash_command_is_ignored(monkeypatch):
    dispatched = []
    upd = _run_alias(monkeypatch, "/nosuchcmd", dispatched)
    assert dispatched == [] and upd.message.replies == []


# ── ทุกคำสั่งในเมนูต้องถูก dispatcher จำได้ (ไม่หลุดไปโหมดดูหุ้นรายตัว) ──

def test_every_menu_command_routes_not_ticker_lookup(monkeypatch):
    def no_lookup(*a, **k):
        raise AssertionError("ต้องไม่ตกไปโหมดดูหุ้น (build_message)")
    monkeypatch.setattr(tb, "build_message", no_lookup)
    # ตัวหนัก (ยิงเน็ต/อ่านไฟล์จริง) แทนด้วยของปลอม — เทสต์เอาแค่ "จำคำสั่งได้"
    monkeypatch.setattr(tb, "run_best_scan", lambda: ([], 0, None, [], None, []))
    monkeypatch.setattr(tb.scanner, "format_report", lambda *a, **k: "รายงาน")
    monkeypatch.setattr(tb, "build_earnings_news_summary", lambda: "ข่าว")
    monkeypatch.setattr(tb, "build_morning_digest", lambda *a, **k: ("", []))
    monkeypatch.setattr(tb, "build_morning_confirm", lambda: ("", []))
    monkeypatch.setattr(tb.shadow, "build_shadow_report", lambda: "เงา")
    monkeypatch.setattr(tb.stats, "build_stats_report", lambda *a, **k: "สถิติ")
    monkeypatch.setattr(tb, "build_earnings_calendar", lambda chat_id: "ปฏิทิน")
    monkeypatch.setattr(tb, "handle_earnings_command", lambda tickers: "งบ")
    monkeypatch.setattr(tb, "build_trades_report", lambda chat_id: "ไม้")
    monkeypatch.setattr(tb, "build_watchlist_summary", lambda chat_id: ("ลิสต์", []))
    monkeypatch.setattr(tb.stock_core, "get_watchlist", lambda chat_id: [])
    monkeypatch.setattr(tb, "get_port_size", lambda chat_id: None)
    for cmd, word in tb.SLASH_ALIASES.items():
        if not word:                                    # price — เทสต์แยกด้านบน
            continue
        upd = FakeUpdate(f"/{cmd}")
        asyncio.run(tb.cmd_alias(upd, _ctx()))
        assert upd.message.replies, cmd                 # มีคำตอบ (ผล/usage) เสมอ
