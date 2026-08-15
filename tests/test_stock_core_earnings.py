# -*- coding: utf-8 -*-
"""stock_core.next_earnings_date — วันงบถัดไปจากคลังในเครื่อง ไม่ยิงเน็ต (manual ชนะ auto)"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stock_core  # noqa: E402

TODAY = datetime.date(2026, 8, 15)


@pytest.fixture
def store(monkeypatch):
    data = {
        "AOT": {"auto_dates": ["2026-05-14", "2026-08-13", "2026-11-12"], "fetched": 1.0},
        "PTT": {"auto_dates": ["2026-08-14", "2026-11-13"], "manual_dates": ["2026-11-10"]},
        "CPALL": {"manual_dates": ["2026-08-15"]},                     # วันนี้ — ไม่นับว่า "ถัดไป"
        "BAD": {"auto_dates": ["ไม่ใช่วันที่", "2026-09-01"]},
    }
    monkeypatch.setattr(stock_core, "_EARN_STORE", data)
    monkeypatch.setattr(stock_core, "today_bkk", lambda: TODAY)
    return data


def test_returns_next_future_auto_date(store):
    assert stock_core.next_earnings_date("AOT") == datetime.date(2026, 11, 12)


def test_manual_date_wins_over_auto(store):
    assert stock_core.next_earnings_date("PTT") == datetime.date(2026, 11, 10)


def test_today_is_not_next(store):
    assert stock_core.next_earnings_date("CPALL") is None


def test_unknown_symbol_returns_none(store):
    assert stock_core.next_earnings_date("ZZZZ") is None


def test_normalizes_bk_suffix_and_case(store):
    assert stock_core.next_earnings_date("aot.bk") == datetime.date(2026, 11, 12)


def test_ignores_malformed_dates(store):
    assert stock_core.next_earnings_date("BAD") == datetime.date(2026, 9, 1)


def test_today_bkk_is_a_date():
    assert isinstance(stock_core.today_bkk(), datetime.date)
