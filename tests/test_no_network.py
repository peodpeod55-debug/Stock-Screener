# -*- coding: utf-8 -*-
"""conftest.py ต้องกันเทสต์ยิงเน็ตจริงทุกทาง + กันเทสต์เขียนทับไฟล์ state ของจริง (แนวคิดจาก HK tests/conftest.py)

NetworkBlocked เป็น BaseException โดยตั้งใจ: โค้ดจริงจับ `except Exception` แล้ว retry/sleep (stock_core._retry)
หรือกลืนเป็น None — ถ้าเป็น Exception ธรรมดา เทสต์ที่ลืม stub จะ "ผ่านช้า ๆ" แทนที่จะล้มทันทีพร้อมบอกว่าลืมอะไร
"""
import os
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import NetworkBlocked  # noqa: E402
import stock_core  # noqa: E402
import telegram_bot as tb  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "http://127.0.0.1:9/"        # port 9 (discard) — ถ้าหลุดไปยิงจริงก็โดนปฏิเสธทันที ไม่ค้าง


def test_urllib_is_blocked():
    with pytest.raises(NetworkBlocked):
        urllib.request.urlopen(URL)


def test_curl_cffi_session_is_blocked():
    from curl_cffi import requests as curl_requests
    with pytest.raises(NetworkBlocked):
        curl_requests.Session().get(URL)


def test_yfinance_through_stock_core_is_blocked():
    with pytest.raises(NetworkBlocked):
        stock_core._yf_ticker("AOT.BK").history(period="5d")


def test_requests_is_blocked():
    import requests
    with pytest.raises(NetworkBlocked):
        requests.get(URL)


def test_httpx_is_blocked():
    import httpx
    with pytest.raises(NetworkBlocked):
        httpx.get(URL)


def test_playwright_is_blocked():
    import playwright.sync_api
    with pytest.raises(NetworkBlocked):
        playwright.sync_api.sync_playwright()


def test_chat_id_write_goes_to_tmp_not_project(tmp_path):
    real = os.path.join(PROJECT, "chat_ids.json")
    before = open(real, "rb").read() if os.path.exists(real) else None
    tb._register_chat(424242)
    after = open(real, "rb").read() if os.path.exists(real) else None
    assert before == after, "chat_ids.json ของจริงถูกแก้"
    assert os.path.dirname(tb._CHAT_IDS_PATH) == str(tmp_path)
    assert 424242 in tb._load_chat_ids()


def test_all_state_paths_point_outside_project():
    names = [(tb, n) for n in ("_CHAT_IDS_PATH", "_ALIVE_PATH", "_DIGEST_STATE_PATH",
                               "_WATCH_STATE_PATH", "_PORT_SETTINGS_PATH")]
    names += [(stock_core, n) for n in ("_WATCHLIST_PATH", "_TRADES_LOG_PATH", "_LOOKUP_LOG_PATH",
                                        "_NAME_CACHE_PATH", "_EARN_STORE_PATH")]
    inside = [n for mod, n in names if os.path.dirname(getattr(mod, n)).startswith(PROJECT)]
    assert inside == [], f"ยังชี้ไฟล์ของจริง: {inside}"
