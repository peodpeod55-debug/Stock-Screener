# -*- coding: utf-8 -*-
"""กันเทสต์ยิงเน็ตจริง + กันเทสต์เขียนทับไฟล์ state ของจริง (port แนวคิดจาก HK tests/conftest.py)

- NetworkBlocked เป็น BaseException โดยตั้งใจ: โค้ดจริงจับ `except Exception` แล้ว retry+sleep
  (stock_core._retry, scanner) หรือกลืนเป็น None — เทสต์ที่ลืม stub จะล้มทันทีพร้อมบอกว่าลืมอะไร
  แทนที่จะ "ผ่านช้า ๆ" หรือได้ None เงียบ ๆ
- ไฟล์ state ทุกตัว (chat_ids / watchlist / digest_state / watch_state / port_settings /
  trades_log / lookup_log / .yf_cache / bot_log / scan_log / news_seen) ถูกชี้ไป tmp_path ของ pytest — เทสต์เขียนได้เต็มที่ ของจริงไม่โดน
"""
import logging
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# telegram_bot เรียก logging.basicConfig ตอน import → เปิด bot_log.txt ของจริง
# ตั้ง handler ให้ root ก่อน = basicConfig ของบอทกลายเป็น no-op ไม่แตะไฟล์ log จริง
logging.basicConfig(level=logging.WARNING, handlers=[logging.NullHandler()])


class NetworkBlocked(BaseException):
    """เทสยิงเน็ตจริง — ดู docstring ของโมดูล"""


def _blocked(what):
    def fn(*args, **kwargs):
        raise NetworkBlocked(f"เทสยิงเน็ตจริง: {what} — ส่ง stub / monkeypatch แทน")
    return fn


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """บล็อกทุกรอยต่อที่โค้ดไทยใช้จริง (curl_cffi เป็น libcurl ไม่ผ่าน socket ของ Python จึง patch ที่ Session)"""
    import urllib.request
    import httpx
    import requests
    import playwright.sync_api
    from curl_cffi import requests as curl_requests

    monkeypatch.setattr(urllib.request, "urlopen", _blocked("urllib.request.urlopen (dashboard_feed)"))
    monkeypatch.setattr(curl_requests.Session, "request", _blocked("curl_cffi Session.request (yfinance / stock_core)"))
    monkeypatch.setattr(requests.Session, "request", _blocked("requests"))
    monkeypatch.setattr(httpx.Client, "send", _blocked("httpx.Client.send (Telegram)"))
    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked("httpx.AsyncClient.send (Telegram)"))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", _blocked("playwright (set_news)"))


@pytest.fixture(autouse=True)
def _state_in_tmp(monkeypatch, tmp_path):
    """ชี้ path ของไฟล์ state ทุกตัวไป tmp — โค้ดอ่านค่าคงที่ตอนเรียก จึง monkeypatch ที่ module attribute พอ"""
    import stock_core
    import telegram_bot as tb

    import scanner
    import set_news

    for name in ("_CHAT_IDS_PATH", "_ALIVE_PATH", "_DIGEST_STATE_PATH", "_WATCH_STATE_PATH", "_PORT_SETTINGS_PATH",
                 "_HOLIDAYS_PATH", "_LOG_PATH"):
        monkeypatch.setattr(tb, name, str(tmp_path / os.path.basename(getattr(tb, name))))
    for name in ("_WATCHLIST_PATH", "_TRADES_LOG_PATH", "_LOOKUP_LOG_PATH", "_NAME_CACHE_PATH", "_EARN_STORE_PATH"):
        monkeypatch.setattr(stock_core, name, str(tmp_path / os.path.basename(getattr(stock_core, name))))
    monkeypatch.setattr(stock_core, "_CACHE_DIR", str(tmp_path))
    # คำสั่ง "สถานะ" อ่าน scan_log.csv + mtime ของ news_seen.json — ชี้ tmp เช่นกัน (deep review: เติม redirect ก่อนแตะโมดูลพวกนี้)
    monkeypatch.setattr(scanner, "LOG_PATH", str(tmp_path / "scan_log.csv"))
    monkeypatch.setattr(set_news, "_SEEN_PATH", str(tmp_path / "news_seen.json"))
