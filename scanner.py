"""สแกนหุ้นที่เพิ่งออกงบแล้วตอบรับดี (รันเองหรือให้บอทเรียกอัตโนมัติหลังปิดตลาด)

เกณฑ์คัดเข้า:  ออกงบภายใน MAX_DAYS_SINCE วัน + วันตอบรับงบปิดบวก ≥ MIN_CHANGE_PCT
              + วอลุ่มวันตอบรับ ≥ MIN_VOL_RATIO เท่าของค่าเฉลี่ย
จัดอันดับด้วยคะแนนสัญญาณ (เกินไฮ 3 เดือนก่อนงบ + ทำไฮต่อเนื่อง ให้น้ำหนักสูงสุด)
ผลทุกครั้งถูกบันทึกต่อท้าย scan_log.csv ไว้วิเคราะห์ย้อนหลัง
"""
import os
import csv
import time
import datetime

import stock_core
from stock_core import format_signed_pct, volume_flag, signal_score

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_PATH = os.path.join(_BASE_DIR, "scan_universe.txt")
LOG_PATH = os.path.join(_BASE_DIR, "scan_log.csv")

MAX_DAYS_SINCE = 7      # สนใจเฉพาะหุ้นที่ออกงบภายในกี่วัน
MIN_CHANGE_PCT = 2.0    # วันตอบรับงบต้องปิดบวกอย่างน้อยกี่ %
MIN_VOL_RATIO = 1.5     # วอลุ่มวันตอบรับต้องกี่เท่าของค่าเฉลี่ย 20 วัน


def _env_value(key: str, default: str = "") -> str:
    """อ่านค่าจาก environment variable หรือไฟล์ .env ข้างๆ สคริปต์
    (สคริปต์นี้รันเดี่ยวได้ จึงอ่าน .env เองไม่ต้องพึ่ง telegram_bot)"""
    val = os.environ.get(key, "").strip()
    if val:
        return val
    env_path = os.path.join(_BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    # strip quote ครอบ เผื่อผู้ใช้ใส่ path แบบ "..." หรือ '...'
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


# ── โหมดสแกนทั้งตลาด: ใช้ข้อมูล parquet จากโปรเจค Trading_Dashboard ──
# คัดหยาบจากไฟล์ในเครื่อง (~883 ตัว ไม่ยิง Yahoo เลย) แล้วค่อยยืนยัน
# เฉพาะตัวที่เข้าเกณฑ์ด้วยข้อมูลสด
# ตั้ง path เอง (ต่อเครื่อง) ผ่าน DASHBOARD_TH_CACHE ใน .env — ถ้าเว้นว่าง
# ระบบจะข้ามโหมดทั้งตลาด แล้วใช้โหมดรายชื่อหลัก (scan_universe.txt) แทน
DASHBOARD_TH_CACHE = _env_value("DASHBOARD_TH_CACHE")
LOCAL_MAX_AGE_DAYS = 5           # ข้อมูล dashboard เก่ากว่านี้ → ใช้สแกนปกติแทน
MIN_AVG_VALUE_THB = 5_000_000    # กรองหุ้นสภาพคล่องต่ำ (มูลค่าซื้อขายเฉลี่ย/วัน)


def load_universe():
    symbols = []
    try:
        with open(UNIVERSE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    symbols.append(line.upper())
    except FileNotFoundError:
        pass
    return symbols


def _local_parquet_files():
    import glob
    if not DASHBOARD_TH_CACHE:
        return []  # ไม่ได้ตั้ง path → ไม่มีข้อมูลทั้งตลาด (กันไป glob ผิดโฟลเดอร์)
    files = glob.glob(os.path.join(DASHBOARD_TH_CACHE, "*.parquet"))
    # ข้ามไฟล์ดัชนี เช่น ^SET.BK
    return [f for f in files if not os.path.basename(f).startswith("^")]


def local_data_last_date():
    """วันที่ล่าสุดของข้อมูลใน dashboard (ดูจากหุ้นใหญ่ที่เทรดทุกวัน)"""
    import pandas as pd
    if not DASHBOARD_TH_CACHE:
        return None
    for sym in ("AOT", "PTT", "KBANK"):
        fp = os.path.join(DASHBOARD_TH_CACHE, f"{sym}.parquet")
        if os.path.exists(fp):
            try:
                return pd.read_parquet(fp).index.max().date()
            except Exception:
                continue
    return None


def coarse_candidates(window_days=None):
    """คัดหยาบจาก parquet ทั้งตลาด: หาหุ้นที่ภายใน window_days วันทำการ
    ล่าสุดมีวันที่ปิดบวก ≥ MIN_CHANGE_PCT พร้อมวอลุ่ม ≥ MIN_VOL_RATIO เท่า
    (ยังไม่รู้ว่าเกี่ยวกับงบไหม — ขั้นยืนยันค่อยเช็ควันงบจาก Yahoo)
    """
    import pandas as pd
    window_days = window_days or MAX_DAYS_SINCE
    out = []
    for fp in _local_parquet_files():
        sym = os.path.splitext(os.path.basename(fp))[0]
        try:
            df = pd.read_parquet(fp).tail(40)
        except Exception:
            continue
        if len(df) < 25:
            continue
        closes, vols = df["close"], df["volume"]
        # กรองสภาพคล่อง: มูลค่าซื้อขายเฉลี่ย 20 วันล่าสุด
        avg_value = float((closes * vols).tail(20).mean())
        if avg_value < MIN_AVG_VALUE_THB:
            continue
        n = min(window_days, len(df) - 21)
        for i in range(len(df) - n, len(df)):
            prev_close = closes.iloc[i - 1]
            if not prev_close:
                continue
            chg = (closes.iloc[i] - prev_close) / prev_close * 100
            avg_vol = float(vols.iloc[i - 20:i].mean())
            vr = (float(vols.iloc[i]) / avg_vol) if avg_vol else 0
            if chg >= MIN_CHANGE_PCT and vr >= MIN_VOL_RATIO:
                out.append(sym)
                break
    return out


def run_full_scan():
    """สแกนทั้งตลาดจากข้อมูล dashboard + ยืนยันด้วยข้อมูลสด

    คืน (hits, scanned_count, last_date) หรือ None ถ้าข้อมูล dashboard
    เก่าเกิน LOCAL_MAX_AGE_DAYS วัน (ให้ผู้เรียกถอยไปใช้ run_scan แทน)
    """
    last_date = local_data_last_date()
    if last_date is None:
        return None
    age = (datetime.date.today() - last_date).days
    if age > LOCAL_MAX_AGE_DAYS:
        return None

    candidates = coarse_candidates()
    hits = []
    unknowns = []  # ราคา/วอลุ่มพุ่ง แต่ Yahoo ไม่มีข้อมูลวันงบ (หุ้นเล็กบางตัว)
    for sym in candidates:
        try:
            d = stock_core.get_stock_data(sym)
        except Exception:
            d = None
        time.sleep(0.2)
        if d is None:
            continue
        days = d["days_since_earnings"]
        # ไม่มีข้อมูลวันงบเลย หรือวันงบล่าสุดที่รู้เก่าผิดปกติ (>120 วัน
        # ทั้งที่ บ.จดทะเบียนออกงบทุก ~90 วัน = Yahoo เลิกอัปเดตหุ้นตัวนี้)
        # และไม่มีกำหนดงบรอบใหม่ใกล้ๆ → ถือว่าไม่รู้วันงบจริง
        no_data = d["last_earnings"] is None and d["next_earnings"] is None
        stale = (days is not None and days > 120
                 and (d["days_to_earnings"] is None or d["days_to_earnings"] > 7))
        if no_data or stale:
            unknowns.append(sym)
            continue
        r = d["earn_reaction"]
        if days is None or days > MAX_DAYS_SINCE:
            continue
        if r is None or r["change_pct"] is None:
            continue
        if r["change_pct"] < MIN_CHANGE_PCT or (r["vol_ratio"] or 0) < MIN_VOL_RATIO:
            continue
        hits.append(d)

    hits.sort(
        key=lambda d: (signal_score(d)[0] or 0, d["since_earnings_pct"] or 0),
        reverse=True,
    )
    _append_log(hits)
    return hits, len(_local_parquet_files()), last_date, unknowns


def run_scan():
    """คืน (hits, scanned_count) — hits เรียงตามคะแนนสัญญาณ"""
    symbols = load_universe()
    hits = []
    for sym in symbols:
        try:
            d = stock_core.get_stock_data(sym)
        except Exception:
            d = None
        time.sleep(0.2)  # เว้นจังหวะกัน rate limit
        if d is None:
            continue
        days = d["days_since_earnings"]
        r = d["earn_reaction"]
        if days is None or days > MAX_DAYS_SINCE:
            continue
        if r is None or r["change_pct"] is None:
            continue
        if r["change_pct"] < MIN_CHANGE_PCT or (r["vol_ratio"] or 0) < MIN_VOL_RATIO:
            continue
        hits.append(d)

    hits.sort(
        key=lambda d: (signal_score(d)[0] or 0, d["since_earnings_pct"] or 0),
        reverse=True,
    )
    _append_log(hits)
    return hits, len(symbols)


def _append_log(hits):
    """บันทึกผลสแกนลง CSV (เปิดใน Excel ได้) เพื่อเก็บข้อมูลไว้วิเคราะห์ต่อ"""
    is_new = not os.path.exists(LOG_PATH)
    try:
        with open(LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow([
                    "scan_date", "ticker", "earn_date", "reaction_pct",
                    "reaction_vol_ratio", "since_earn_pct", "price",
                    "broke_5d_high", "broke_3m_high", "days_since_new_high",
                    "pre_earn_low", "above_pre_low", "score",
                ])
            today = datetime.date.today().isoformat()
            for d in hits:
                r = d["earn_reaction"]
                s = d["post_signals"] or {}
                score, _ = signal_score(d)
                w.writerow([
                    today,
                    d["ticker"],
                    d["last_earnings"].isoformat() if d["last_earnings"] else "",
                    f"{r['change_pct']:.2f}",
                    f"{r['vol_ratio']:.2f}" if r["vol_ratio"] else "",
                    f"{d['since_earnings_pct']:.2f}" if d["since_earnings_pct"] is not None else "",
                    f"{d['price']:.2f}",
                    int(bool(s.get("broke_pre5d_high"))),
                    int(bool(s.get("broke_pre3m_high"))),
                    s.get("days_since_new_high", ""),
                    f"{s['pre_earn_low']:.2f}" if s.get("pre_earn_low") else "",
                    int(bool(s.get("above_pre_low"))),
                    score if score is not None else "",
                ])
    except Exception:
        pass  # เขียน log ไม่ได้ (เช่นไฟล์เปิดอยู่ใน Excel) ไม่ต้องล้มทั้งสแกน


def format_report(hits, scanned_count, html=True, limit=15, source_note=None,
                  unknowns=None):
    b = (lambda t: f"<b>{t}</b>") if html else (lambda t: t)
    today = datetime.date.today().strftime("%d/%m/%Y")
    lines = [
        f"🔍 {b('สแกนหุ้นตอบรับงบดี')} — {today}",
        f"(ตรวจ {scanned_count} ตัว, ออกงบใน {MAX_DAYS_SINCE} วัน, "
        f"วันตอบรับ ≥ +{MIN_CHANGE_PCT:.0f}% วอลุ่ม ≥ {MIN_VOL_RATIO}x)",
    ]
    if source_note:
        lines.append(source_note)
    lines.append("")
    if not hits:
        lines.append("วันนี้ไม่พบหุ้นเข้าเกณฑ์ครับ")

    for i, d in enumerate(hits[:limit], 1):
        base = d["ticker"].replace(".BK", "")
        r = d["earn_reaction"]
        s = d["post_signals"] or {}
        score, stars = signal_score(d)
        flags = []
        if s.get("broke_pre3m_high"):
            flags.append("ทะลุไฮ 3 ด. ✅")
        elif s.get("broke_pre5d_high"):
            flags.append("ผ่านไฮ 5 วัน ✅")
        dsh = s.get("days_since_new_high")
        if dsh is not None and dsh <= 5:
            flags.append("ไฮใหม่วันนี้ 🔥" if dsh == 0 else f"ไฮใหม่ {dsh} วันก่อน")
        streak = s.get("weekly_hh_streak") or 0
        if streak >= 2:
            flags.append(f"ไฮยกขึ้น {streak} สัปดาห์ติด 🔥")
        if s.get("above_pre_low") is False:
            flags.append("⛔ หลุด Low ก่อนงบ")
        vol_txt = f" วอล {r['vol_ratio']:.1f}x{volume_flag(r['vol_ratio'])}" if r["vol_ratio"] else ""
        lines.append(
            f"{i}. {b(base)} {stars} ({score}/{stock_core.SCORE_MAX})  "
            f"วันงบ {b(format_signed_pct(r['change_pct']))}{vol_txt}"
        )
        detail = f"    ตั้งแต่งบ {format_signed_pct(d['since_earnings_pct'])}"
        if flags:
            detail += " • " + " • ".join(flags)
        lines.append(detail)

    if len(hits) > limit:
        lines.append(f"...และอีก {len(hits) - limit} ตัว (ดูครบใน scan_log.csv)")

    if unknowns:
        shown = ", ".join(unknowns[:12])
        if len(unknowns) > 12:
            shown += f" ...(+{len(unknowns) - 12})"
        lines += [
            "",
            "⚠️ ราคา/วอลุ่มพุ่งใน 7 วันนี้ แต่ไม่มีข้อมูลวันงบ:",
            shown,
            ("ถ้าตัวไหนเพิ่งออกงบจริง บันทึกวันเอง: งบ ชื่อหุ้น 13/11/2569"
             if not html else
             "ถ้าตัวไหนเพิ่งออกงบจริง บันทึกวันเอง: <code>งบ ชื่อหุ้น 13/11/2569</code>"),
        ]

    if hits:
        lines.append("")
        lines.append("เก็บเข้าลิสต์: พิมพ์ ติดตาม <ชื่อหุ้น>" if not html
                     else "เก็บเข้าลิสต์: <code>ติดตาม ชื่อหุ้น</code>")
    return "\n".join(lines)


if __name__ == "__main__":
    print("\nกำลังสแกน... (ประมาณ 1-3 นาที)\n")
    full = run_full_scan()
    if full is not None:
        hits, n, last_date, unknowns = full
        note = f"โหมดทั้งตลาด • ข้อมูล Trading_Dashboard ถึง {last_date:%d/%m/%Y}"
    else:
        hits, n = run_scan()
        note, unknowns = "โหมดรายชื่อหลัก (ข้อมูล Trading_Dashboard ไม่สด)", None
    print(format_report(hits, n, html=False, source_note=note, unknowns=unknowns))
    print(f"\nบันทึกผลลง {os.path.basename(LOG_PATH)} แล้ว\n")
