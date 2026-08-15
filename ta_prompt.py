# -*- coding: utf-8 -*-
"""ta_prompt.py — ข้อความ PHASE 0 สำหรับ Gem เทคนิค (Multi-Model TA prompt v1.1)

port แบบตัวต่อตัวจาก taPrompt() ใน Trading_Dashboard/market_dashboard.js (ปุ่ม 📐 บน dashboard)
ข้อความที่ได้ต้องเท่ากับปุ่ม 📐 ทุกบรรทัด ยกเว้น ข.8 (วันประกาศงบ) ที่รับจากคลังวันงบของบอท
เพราะ payload TH ของ dashboard ไม่มี field `ed`

รับ dict ล้วน ๆ ไม่แตะดิสก์ ไม่แตะเน็ต — เทสต์ได้ตรง ๆ (tests/test_ta_prompt.py)
และมีเทสต์กัน drift ที่รัน taPrompt() ตัวจริงใน Node มาเทียบ (tests/test_ta_prompt_matches_dashboard.py)
"""
from decimal import Decimal, ROUND_HALF_UP

# ── ค่าคงที่ที่ port มาจาก market_dashboard.js (เก็บแค่ชื่อ ไม่เอาสี) ──
STAGE_NAME = {5: "SpecialBull", 4: "ExtraBull", 3: "Bull", 2: "Accum", 1: "Recovery",
              6: "Warning", 7: "Distribution", 8: "Bear", 0: "None"}
MKT_LABEL = {"US": "US (NASDAQ/NYSE)", "TH": "SET (หุ้นไทย)", "HK": "HKEX (ฮ่องกง)", "ETF": "ETF สหรัฐฯ"}
SCAN_LABEL = ["VDU (volume dry-up)", "Pocket Pivot", "Buyable Gap-Up", "ใกล้ 52WH ≤5%"]
NO_DATA = "ไม่มีข้อมูล"


def trend_w(w):
    """TREND_W ใน JS: 2..5 → Bull · 1 → Recovery · 0/6 → Sideway · null → ไม่ทราบ · อื่น → Bear"""
    if w is None:
        return "ไม่ทราบ"
    if 2 <= w <= 5:
        return "Bull"
    if w == 1:
        return "Recovery"
    if w in (0, 6):
        return "Sideway"
    return "Bear"


def f(x, d=2):
    """เท่ากับ Number(x).toLocaleString('en-US', {maximumFractionDigits: d}) ของ JS:
    คอมมาคั่นหลักพัน · ทศนิยมไม่เกิน d ตำแหน่ง · ตัดศูนย์ท้าย · ปัดครึ่งขึ้น (halfExpand)
    — ใช้ Decimal(str(x)) เพราะ round() ของ Python ปัดครึ่งไปเลขคู่ และ str() ให้ทศนิยมสั้นสุด
    แบบเดียวกับที่ ICU ใช้"""
    if x is None:
        return NO_DATA
    q = Decimal(str(x)).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP)
    s = format(q, ",f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _js_num(x):
    """`${x}` ของ JS สำหรับตัวเลขดิบ (ไม่ผ่าน f) — จำนวนเต็มไม่มี .0"""
    if x is None:
        return NO_DATA
    try:
        if float(x).is_integer():
            return str(int(x))
    except (TypeError, ValueError):
        pass
    return str(x)


def idx_ret_bars(market, bars):
    """ผลตอบแทนดัชนีอ้างอิงย้อนหลัง n แท่ง (%) จาก market['bench']['close'] — ต้อง len > bars"""
    bench = (market or {}).get("bench") or {}
    close = bench.get("close") if isinstance(bench, dict) else None
    if not close or len(close) <= bars:
        return None
    base = close[-1 - bars]
    if not base:
        return None
    return (close[-1] / base - 1) * 100


def build_ta_prompt(stock, market, next_earn):
    """(stock dict จาก stocks-<MK>.json, market meta จาก core.json.markets[MK] + core_asof,
    next_earn: datetime.date | None) → ข้อความ PHASE 0"""
    s = stock
    mk = market or {}
    g = s.get("g") or "TH"
    idx_name = mk.get("indexName") or "ดัชนีอ้างอิง"
    asof = mk.get("asof") or mk.get("core_asof") or "ไม่ทราบวันที่"

    def alpha(r, bars):
        bi = idx_ret_bars(mk, bars)
        if r is None or bi is None:
            return NO_DATA
        a = r - bi
        return f"{'+' if a >= 0 else ''}{f(a, 1)}% (หุ้น {f(r, 1)}% / {idx_name} {f(bi, 1)}%)"

    md = s.get("macdv") or [None, None, None]
    md = list(md) + [None] * (3 - len(md))
    scn = s.get("scn") or []
    scans = [lbl for i, lbl in enumerate(SCAN_LABEL) if i < len(scn) and scn[i]]
    st = s.get("st")
    stage = f"{st} {STAGE_NAME.get(st, '?')}" if st is not None else NO_DATA
    vmax3 = s.get("vmax3")
    if vmax3 is not None:                       # JS เช็ค truthiness — [] ก็เข้าสาขานี้ (ห้ามใช้ `if vmax3`)
        v_bars = _js_num(vmax3[0]) if len(vmax3) > 0 else NO_DATA
        v_mult = f(vmax3[1], 2) if len(vmax3) > 1 else NO_DATA
        vspike = f" · spike แรงสุดใน 3 เดือน {v_mult}× เมื่อ {v_bars} แท่งก่อน"
    else:
        vspike = " · ไม่มีข้อมูล volume spike"
    name = f" ({s['n']})" if s.get("n") else ""
    rs = s.get("rs")
    bis = s.get("bis")
    earn = next_earn.isoformat() if next_earn else NO_DATA

    return (
        f"วิเคราะห์เทคนิค {s.get('t')}{name} ตาม prompt — แนบรูป Weekly + Daily มาด้วย\n"
        "\n"
        "=== PHASE 0 (ตอบครบแล้ว ไม่ต้องถามซ้ำ) ===\n"
        f"ก.1 ตลาด: {MKT_LABEL.get(g, g)}\n"
        "ก.2 TF ที่แนบ: [Weekly + Daily — แก้บรรทัดนี้ถ้าแนบไม่ครบ]\n"
        f"ก.3 ระดับราคา (ข้อมูล {asof} · {'ระหว่างวัน แท่งยังไม่ปิด' if mk.get('intraday') else 'ปิดตลาดแล้ว'}):\n"
        f"  ราคาปัจจุบัน {f(s.get('c'), 4)}\n"
        f"  Swing High/Low 30 แท่ง: {f(s.get('sh30'), 4)} / {f(s.get('sl30'), 4)}\n"
        f"  52W High/Low: {f(s.get('h52'), 4)} / {f(s.get('l52'), 4)}\n"
        f"  SMA20 {f(s.get('sma20'), 4)} · SMA50 {f(s.get('sma50'), 4)} · SMA150 {f(s.get('sma150'), 4)} · SMA200 {f(s.get('sma200'), 4)}\n"
        f"  EMA20 {f(s.get('e20v'), 4)} · EMA50 {f(s.get('e50v'), 4)}\n"
        f"ก.4 Volume: วันล่าสุด {f(s.get('vr50'), 2)}× ของค่าเฉลี่ย 50 วัน{vspike}\n"
        f"ข.5 RSI(14) {f(s.get('rsi'), 1)} · MACD {f(md[0], 3)} / signal {f(md[1], 3)} / histogram {f(md[2], 3)}\n"
        f"ข.6 ATR(14) {f(s.get('atrc'), 4)} ({f(s.get('atrp'), 2)}% ของราคา)\n"
        f"ข.7 Relative Strength: 1 เดือน {alpha(s.get('r1m'), 21)} · 3 เดือน {alpha(s.get('r3m'), 63)} · RS Rating {NO_DATA if rs is None else _js_num(rs)}/99\n"
        f"ข.8 วันประกาศงบถัดไป: {earn}\n"
        "ค.9 ขนาดพอร์ต: [กรอกเอง] · risk ต่อ trade: 1%\n"
        "ค.10 สถานะ: [มีของอยู่แล้ว / กำลังหาจุดเข้า — เลือกอย่างใดอย่างหนึ่ง]\n"
        "\n"
        "=== ข้อมูลเสริมจากระบบ screener (นอก prompt) ===\n"
        f"Stage MSI: {stage} (อยู่มา {'—' if bis is None else _js_num(bis)} แท่ง) · Trend Weekly: {trend_w(s.get('stw'))}\n"
        f"สแกนที่ติด: {', '.join(scans) if scans else 'ไม่ติดสแกนใด'}\n"
        f"ผลตอบแทน: 1 เดือน {f(s.get('r1m'), 1)}% · YTD {f(s.get('ytd'), 1)}% · 1 ปี {f(s.get('r1y'), 1)}%\n"
        "\n"
        f"หมายเหตุ: ตัวเลขทั้งหมดมาจากระบบ screener ณ {asof} ไม่ได้อ่านจากภาพ — ถ้ารูปกราฟสดกว่านี้ให้ยึดรูปและบอกความต่าง"
    )
