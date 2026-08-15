# -*- coding: utf-8 -*-
"""เทสต์ ta_prompt.build_ta_prompt — port ของ taPrompt() ใน Trading_Dashboard/market_dashboard.js

golden ด้านล่างคือข้อความที่ taPrompt() ตัวจริงสร้างจาก build 18f8448e0b5402633ce2 (2026-08-14)
รันผ่าน Node (ดู tests/test_ta_prompt_matches_dashboard.py สำหรับการเทียบสดกับ JS)
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ta_prompt  # noqa: E402

# ── ข้อมูลจริงของ AOT จาก stocks-TH.json build 18f8448e0b5402633ce2 (เฉพาะ field ที่ taPrompt ใช้) ──
AOT = {
    "t": "AOT", "n": "Airports of Thailand Public Company Limited", "g": "TH",
    "c": 65.0, "sma20": 63.525, "sma50": 62.225, "sma150": 55.9483, "sma200": 53.5813,
    "e20v": 63.423, "e50v": 61.743, "h52": 65.0, "l52": 34.475, "sh30": 65.0, "sl30": 60.5,
    "vr50": 2.15, "vmax3": [51, 3.98], "rsi": 63.1, "macdv": [0.39916, 0.470523, -0.0713627],
    "atrc": 1.08152, "atrp": 1.66, "rs": 86, "r1m": 1.17, "r3m": 23.2, "ytd": 22.6, "r1y": 62.9,
    "st": 5, "bis": 2, "stw": 2, "scn": [1, 1, 0, 1],
    # ไม่มี key "ed" — payload TH ไม่ใส่มาเลย
}


def _bench_close():
    """bench.close ยาว 65 แท่ง — idxRetBars ใช้แค่ตัวสุดท้ายกับตัวที่ย้อน 21/63 แท่ง
    (ค่าจริงจาก core.json: last 1609.06, ย้อน 21 = 1627.9, ย้อน 63 = 1483.56)"""
    close = [1500.0] * 65
    close[-1] = 1609.06
    close[-1 - 21] = 1627.9
    close[-1 - 63] = 1483.56
    return close


def market_th(**over):
    m = {"asof": "2026-08-14", "indexName": "SET Index", "intraday": False,
         "bench": {"close": _bench_close()}, "core_asof": "2026-08-14"}
    m.update(over)
    return m


GOLDEN_AOT = """วิเคราะห์เทคนิค AOT (Airports of Thailand Public Company Limited) ตาม prompt — แนบรูป Weekly + Daily มาด้วย

=== PHASE 0 (ตอบครบแล้ว ไม่ต้องถามซ้ำ) ===
ก.1 ตลาด: SET (หุ้นไทย)
ก.2 TF ที่แนบ: [Weekly + Daily — แก้บรรทัดนี้ถ้าแนบไม่ครบ]
ก.3 ระดับราคา (ข้อมูล 2026-08-14 · ปิดตลาดแล้ว):
  ราคาปัจจุบัน 65
  Swing High/Low 30 แท่ง: 65 / 60.5
  52W High/Low: 65 / 34.475
  SMA20 63.525 · SMA50 62.225 · SMA150 55.9483 · SMA200 53.5813
  EMA20 63.423 · EMA50 61.743
ก.4 Volume: วันล่าสุด 2.15× ของค่าเฉลี่ย 50 วัน · spike แรงสุดใน 3 เดือน 3.98× เมื่อ 51 แท่งก่อน
ข.5 RSI(14) 63.1 · MACD 0.399 / signal 0.471 / histogram -0.071
ข.6 ATR(14) 1.0815 (1.66% ของราคา)
ข.7 Relative Strength: 1 เดือน +2.3% (หุ้น 1.2% / SET Index -1.2%) · 3 เดือน +14.7% (หุ้น 23.2% / SET Index 8.5%) · RS Rating 86/99
ข.8 วันประกาศงบถัดไป: ไม่มีข้อมูล
ค.9 ขนาดพอร์ต: [กรอกเอง] · risk ต่อ trade: 1%
ค.10 สถานะ: [มีของอยู่แล้ว / กำลังหาจุดเข้า — เลือกอย่างใดอย่างหนึ่ง]

=== ข้อมูลเสริมจากระบบ screener (นอก prompt) ===
Stage MSI: 5 SpecialBull (อยู่มา 2 แท่ง) · Trend Weekly: Bull
สแกนที่ติด: VDU (volume dry-up), Pocket Pivot, ใกล้ 52WH ≤5%
ผลตอบแทน: 1 เดือน 1.2% · YTD 22.6% · 1 ปี 62.9%

หมายเหตุ: ตัวเลขทั้งหมดมาจากระบบ screener ณ 2026-08-14 ไม่ได้อ่านจากภาพ — ถ้ารูปกราฟสดกว่านี้ให้ยึดรูปและบอกความต่าง"""


def test_golden_aot_matches_dashboard_button():
    assert ta_prompt.build_ta_prompt(AOT, market_th(), None) == GOLDEN_AOT


def test_next_earn_date_fills_line_b8():
    out = ta_prompt.build_ta_prompt(AOT, market_th(), datetime.date(2026, 11, 13))
    assert "ข.8 วันประกาศงบถัดไป: 2026-11-13" in out


def test_all_fields_none_shows_no_data_everywhere():
    empty = {k: None for k in AOT}
    empty["t"] = "XXX"
    empty["g"] = "TH"
    out = ta_prompt.build_ta_prompt(empty, {"asof": None, "core_asof": None, "bench": None}, None)
    for bad in ("None", "nan", "undefined", "()"):
        assert bad not in out
    assert "วิเคราะห์เทคนิค XXX ตาม prompt" in out          # ชื่อว่าง → ไม่มีวงเล็บ
    assert "ราคาปัจจุบัน ไม่มีข้อมูล" in out
    assert "· ไม่มีข้อมูล volume spike" in out
    assert "RS Rating ไม่มีข้อมูล/99" in out
    assert "Stage MSI: ไม่มีข้อมูล (อยู่มา — แท่ง) · Trend Weekly: ไม่ทราบ" in out
    assert "สแกนที่ติด: ไม่ติดสแกนใด" in out
    assert "(ข้อมูล ไม่ทราบวันที่ · ปิดตลาดแล้ว)" in out
    assert "1 เดือน ไม่มีข้อมูล · 3 เดือน ไม่มีข้อมูล" in out


def test_zero_rs_and_bis_are_shown_as_zero_not_default():
    s = dict(AOT, rs=0, bis=0)
    out = ta_prompt.build_ta_prompt(s, market_th(), None)
    assert "RS Rating 0/99" in out
    assert "(อยู่มา 0 แท่ง)" in out


def test_empty_vmax3_list_does_not_crash_and_is_treated_as_present():
    # JS: [] เป็น truthy → เข้าสาขา spike (ค่าเป็น undefined → ไม่มีข้อมูล) — Python ต้องไม่ crash
    s = dict(AOT, vmax3=[])
    out = ta_prompt.build_ta_prompt(s, market_th(), None)
    assert "spike แรงสุดใน 3 เดือน ไม่มีข้อมูล× เมื่อ ไม่มีข้อมูล แท่งก่อน" in out


def test_empty_company_name_has_no_parentheses():
    s = dict(AOT, n="")
    out = ta_prompt.build_ta_prompt(s, market_th(), None)
    assert out.startswith("วิเคราะห์เทคนิค AOT ตาม prompt —")


def test_intraday_flag_changes_header():
    out = ta_prompt.build_ta_prompt(AOT, market_th(intraday=True), None)
    assert "(ข้อมูล 2026-08-14 · ระหว่างวัน แท่งยังไม่ปิด)" in out


def test_missing_bench_gives_no_data_for_relative_strength():
    out = ta_prompt.build_ta_prompt(AOT, market_th(bench=None), None)
    assert "ข.7 Relative Strength: 1 เดือน ไม่มีข้อมูล · 3 เดือน ไม่มีข้อมูล · RS Rating 86/99" in out


def test_short_bench_gives_no_data():
    m = market_th(bench={"close": [1500.0] * 30})      # พอสำหรับ 21 แท่ง ไม่พอ 63
    out = ta_prompt.build_ta_prompt(AOT, m, None)
    assert "3 เดือน ไม่มีข้อมูล" in out
    assert "1 เดือน ไม่มีข้อมูล" not in out


def test_asof_falls_back_to_core_asof_then_unknown():
    out = ta_prompt.build_ta_prompt(AOT, market_th(asof=None, core_asof="2026-01-02"), None)
    assert "(ข้อมูล 2026-01-02 ·" in out
    out = ta_prompt.build_ta_prompt(AOT, market_th(asof=None, core_asof=None), None)
    assert "(ข้อมูล ไม่ทราบวันที่ ·" in out


def test_unknown_market_label_falls_back_to_code():
    s = dict(AOT, g="XX")
    out = ta_prompt.build_ta_prompt(s, market_th(), None)
    assert "ก.1 ตลาด: XX" in out


# ── formatter: เท่ากับ Number(x).toLocaleString('en-US',{maximumFractionDigits:d}) ──

def test_formatter_matches_js_tolocalestring():
    f = ta_prompt.f
    assert f(None) == "ไม่มีข้อมูล"
    assert f(65.0, 4) == "65"                 # ตัดศูนย์ท้าย
    assert f(1.08152, 4) == "1.0815"          # ไม่เกิน d ตำแหน่ง
    assert f(-0.0713627, 3) == "-0.071"
    assert f(1234.5, 4) == "1,234.5"          # คอมมาคั่นหลักพัน
    assert f(2.345, 2) == "2.35"              # halfExpand ไม่ใช่ banker's rounding
    assert f(2.5, 0) == "3"
    assert f(0, 2) == "0"
    assert f(1234567.891, 2) == "1,234,567.89"


def test_message_stays_under_telegram_limit_with_extreme_values():
    s = dict(AOT, n="X" * 200, c=123456789.1234, h52=987654321.9876, l52=0.0001,
             r1m=-99999.9, r3m=99999.9, ytd=-12345.6, r1y=123456.7)
    out = ta_prompt.build_ta_prompt(s, market_th(), datetime.date(2026, 12, 31))
    assert len(out) < 3000        # เผื่อหัวข้อความ Telegram อีก ~300 ตัวอักษร ยังต่ำกว่า 3,900
