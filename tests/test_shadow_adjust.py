# -*- coding: utf-8 -*-
"""ไม้เงาต้อง replay ด้วยราคาซื้อขายจริง ไม่ใช่ราคา adjusted (improvement review 31 ส.ค. §1.7)

stop (pre_earn_low) ถูกเก็บเป็นราคาจริง ณ วันสแกน แต่ replay เดิมดึงผ่าน history()
ที่ auto_adjust=True → หุ้นขึ้น XD หลังงบ (ปกติมากใน SET) ราคาย้อนหลังถูก rescale
แต่ stop ไม่ → ⛔ ปลอม / R เพี้ยน แล้ว cache ถาวรใน shadow_log.csv
แก้: fetch_history รับ auto_adjust แล้ว shadow ส่ง False — สเกลเดียวกับ stop
และเหมือนที่ watch job เทียบราคาสดกับเส้น ⛔ (stats/backtest คงใช้ adjusted ซึ่งถูกแล้ว
สำหรับวัด drift return)
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shadow        # noqa: E402
import stock_core    # noqa: E402

_HIST = pd.DataFrame(
    {"Open": [61.0, 62.0], "High": [62.0, 63.0], "Low": [60.5, 61.5],
     "Close": [61.5, 62.5]},
    index=pd.to_datetime(["2026-08-28", "2026-08-31"]))


def test_fetch_history_passes_auto_adjust_to_yahoo(monkeypatch):
    calls = []

    class FakeTicker:
        def history(self, **kwargs):
            calls.append(kwargs)
            return _HIST

    monkeypatch.setattr(stock_core, "_yf_ticker", lambda t: FakeTicker())

    stock_core.fetch_history("AOT", period="1y", auto_adjust=False)
    assert calls and calls[-1]["auto_adjust"] is False

    calls.clear()
    stock_core.fetch_history("AOT")   # ค่า default ต้องคง adjusted (ผู้เรียกเดิม: stats/backtest)
    assert calls and calls[-1]["auto_adjust"] is True


def test_update_shadow_replays_with_unadjusted_prices(monkeypatch):
    with open(shadow.SCAN_LOG_PATH, "w", encoding="utf-8-sig", newline="") as f:
        f.write("scan_date,ticker,earn_date,pre_earn_low,score\n"
                "2026-08-27,AOT.BK,2026-08-26,60.0,9\n")
    calls = []

    def fake_fetch(ticker, period="1y", **kwargs):
        calls.append(kwargs)
        return ticker, _HIST

    monkeypatch.setattr(shadow.stock_core, "fetch_history", fake_fetch)
    shadow.update_shadow()
    assert calls and calls[0].get("auto_adjust") is False, \
        "replay ไม้เงาต้องขอราคาจริง (auto_adjust=False) ให้สเกลตรงกับ stop ที่เก็บไว้"
