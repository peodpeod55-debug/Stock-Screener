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
from zoneinfo import ZoneInfo

import dotenv
from yfinance.exceptions import YFRateLimitError

import stock_core
from stock_core import format_signed_pct, volume_flag, signal_score

_BKK = ZoneInfo("Asia/Bangkok")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_PATH = os.path.join(_BASE_DIR, "scan_universe.txt")
LOG_PATH = os.path.join(_BASE_DIR, "scan_log.csv")

MAX_DAYS_SINCE = 7      # สนใจเฉพาะหุ้นที่ออกงบภายในกี่วัน
MIN_CHANGE_PCT = 2.0    # วันตอบรับงบต้องปิดบวกอย่างน้อยกี่ %
MIN_VOL_RATIO = 1.5     # วอลุ่มวันตอบรับต้องกี่เท่าของค่าเฉลี่ย 20 วัน


def _env_value(key: str, default: str = "", env_path: str | None = None) -> str:
    """อ่านค่าจาก environment variable หรือไฟล์ .env ข้างๆ สคริปต์ ผ่าน python-dotenv
    (utf-8-sig ทนไฟล์ .env ที่มี BOM — HK/US ใช้ตัวเดียวกัน) — ไม่เรียก load_dotenv()
    เพราะห้ามแตะ os.environ (สคริปต์นี้รันเดี่ยวได้ จึงอ่าน .env เองไม่ต้องพึ่ง telegram_bot)"""
    val = os.environ.get(key, "").strip()
    if val:
        return val
    path = env_path or os.path.join(_BASE_DIR, ".env")
    val = dotenv.dotenv_values(path, encoding="utf-8-sig").get(key)
    if val is not None and val.strip():
        return val.strip()
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


# ── โหมดหลัก: สแกนจากรายชื่อ "บริษัทที่แจ้งงบแล้ว" (ข่าวเว็บ SET) ──
# แม่นกว่าการไล่หาราคาพุ่งทั้งตลาดแล้วค่อยถามว่าเกี่ยวกับงบไหม เพราะรู้ตัว
# ผู้แจ้งงบเป๊ะๆ จาก filings_log.csv + คลังวันงบ — ครอบคลุมทุกบริษัทที่แจ้งงบ
# (ไม่จำกัดแค่ตัวที่อยู่ใน cache ของ Trading_Dashboard) และไม่พึ่งความสด
# ของ parquet — parquet เหลือบทบาทเป็นตัวกรองหยาบเสริม (มีก็ใช้ ไม่มีก็ข้าม)

NEWS_FRESH_MAX_HOURS = 36  # ข้อมูลข่าวเก่ากว่านี้ = ระบบข่าวอาจล่ม → ถอยโหมดเดิม


def _parquet_prefilter(candidates):
    """คัดทิ้ง candidate ที่ parquet ยืนยันได้ว่าตั้งแต่วันแจ้งงบมา
    "ไม่มีวันไหนตอบรับถึงเกณฑ์เลย" — เช็คแบบ superset (มีสักวันที่ผ่านเกณฑ์
    ก็เก็บไว้ให้ Yahoo ตัดสินขั้นสุดท้าย) จึงไม่มีทางตัดตัวจริงทิ้ง

    candidates: {symbol: วันแจ้งงบ (date)} → คืน (kept_symbols, dropped_count)
    ไม่มี parquet / ข้อมูลไม่ครอบคลุมช่วงหลังแจ้งงบ = ตัดสินไม่ได้ → เก็บไว้
    """
    kept, dropped = [], 0
    if not DASHBOARD_TH_CACHE:
        return sorted(candidates), dropped
    import pandas as pd
    for sym, filed in sorted(candidates.items()):
        fp = os.path.join(DASHBOARD_TH_CACHE, f"{sym}.parquet")
        if not os.path.exists(fp):
            kept.append(sym)
            continue
        try:
            df = pd.read_parquet(fp).tail(120)
        except Exception:
            kept.append(sym)
            continue
        dates = [ts.date() for ts in df.index]
        # วันตอบรับจริงคือ 1 ใน 2 วันซื้อขายแรกตั้งแต่วันแจ้งงบ (ดู
        # stock_core._earnings_day_reaction) — ตัดได้เฉพาะเมื่อ parquet
        # ครอบทั้ง 2 วันนั้นแล้ว ข้อมูลยังมาไม่ครบ = ห้ามตัดสิน
        post = [i for i, d in enumerate(dates) if d >= filed]
        if len(post) < 2:
            kept.append(sym)
            continue
        start = post[0]
        if start < 21:  # ประวัติก่อนวันงบไม่พอคำนวณวอลุ่มเฉลี่ย 20 วัน
            kept.append(sym)
            continue
        closes, vols = df["close"], df["volume"]
        ok = False
        for i in range(start, len(df)):
            prev_close = closes.iloc[i - 1]
            if not prev_close:
                continue
            chg = (closes.iloc[i] - prev_close) / prev_close * 100
            avg_vol = float(vols.iloc[i - 20:i].mean())
            vr = (float(vols.iloc[i]) / avg_vol) if avg_vol else 0
            if chg >= MIN_CHANGE_PCT and vr >= MIN_VOL_RATIO:
                ok = True
                break
        if ok:
            kept.append(sym)
        else:
            dropped += 1
    return kept, dropped


RATE_LIMIT_PAUSE_S = 90  # โดน Yahoo จำกัดคำขอกลางสแกน: พักกี่วินาทีก่อนลองต่อ


def _fetch_all(symbols):
    """ดึงข้อมูลทีละตัว คืน ({sym: data หรือ None}, รายชื่อที่ยังไม่ได้ตรวจ)

    เดิมโดน rate limit กลางคันแล้วตัวที่เหลือถูกตีเป็น "ไม่มีข้อมูลราคา"
    ทั้งหมดแบบเงียบๆ (หลุดจาก scan_log และไม้เงาของวันนั้นถาวร)
    → แก้เป็น: พักครั้งเดียว (RATE_LIMIT_PAUSE_S) แล้วไล่ต่อจากตัวเดิม
    โดนซ้ำอีกถือว่าโควตาหมดจริง คืนตัวที่เหลือให้ผู้เรียกรายงานแยก"""
    out, rate_limited = {}, []
    paused = False
    i = 0
    while i < len(symbols):
        sym = symbols[i]
        try:
            d = stock_core.get_stock_data(sym)
        except YFRateLimitError:
            if not paused:
                paused = True
                time.sleep(RATE_LIMIT_PAUSE_S)
                continue  # ลองตัวเดิมซ้ำหลังพัก
            rate_limited = list(symbols[i:])
            break
        except Exception:
            d = None
        time.sleep(0.2)  # เว้นจังหวะกัน rate limit
        out[sym] = d
        i += 1
    return out, rate_limited


def run_filings_scan():
    """สแกนจากรายชื่อบริษัทที่แจ้งงบใน MAX_DAYS_SINCE วันล่าสุด

    แหล่ง candidate: filings_log.csv (ข่าวเว็บ SET) + คลังวันงบในเครื่อง
    (รวมที่ผู้ใช้บันทึกเองผ่าน "งบ XXX วันที่") แล้วยืนยันทุกตัวด้วยข้อมูลสด
    จาก Yahoo ตามเกณฑ์เดิมเป๊ะ — ผลบันทึกลง scan_log.csv schema เดิม

    คืน (hits, candidate_count, no_data_syms, dropped_count, rate_limited)
    หรือ None ถ้าข้อมูลข่าวไม่สด (poll ล้มนาน/ไม่เคยดึง) — ผู้เรียกถอยไป
    โหมด parquet/รายชื่อหลักแทน (fail open: เว็บ SET ล่มต้องยังสแกนได้)
    """
    import set_news  # import ในฟังก์ชัน: set_news เอง import scanner (กันวนซ้ำ)
    age = set_news.news_data_age_hours()
    if age is None or age > NEWS_FRESH_MAX_HOURS:
        return None

    now = datetime.datetime.now(_BKK)
    since = now - datetime.timedelta(days=MAX_DAYS_SINCE)
    candidates = {}  # {symbol: วันแจ้งงบ}
    for sym, d in stock_core.symbols_with_recent_earnings(MAX_DAYS_SINCE).items():
        candidates[sym] = d
    for sym, info in set_news.load_filings_since(since).items():
        candidates[sym] = info["datetime"].date()  # ข่าวจริงชนะวันจากคลัง

    kept, dropped = _parquet_prefilter(candidates)
    data, rate_limited = _fetch_all(kept)
    hits = []
    no_data = []  # แจ้งงบแล้วแต่ Yahoo ไม่มีข้อมูลราคา (หุ้นเล็ก/กอง REIT บางตัว)
    for sym, d in data.items():
        if d is None:
            no_data.append(sym)
            continue
        if not d["ticker"].endswith(".BK"):
            continue  # คลังวันงบมีหุ้น US ที่เคยเปิดดูปนอยู่ — สแกนนี้เอาเฉพาะ SET
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
    return hits, len(candidates), no_data, dropped, rate_limited


def run_full_scan():
    """สแกนทั้งตลาดจากข้อมูล dashboard + ยืนยันด้วยข้อมูลสด

    คืน (hits, scanned_count, last_date, unknowns, rate_limited)
    หรือ None ถ้าข้อมูล dashboard เก่าเกิน LOCAL_MAX_AGE_DAYS วัน
    (ให้ผู้เรียกถอยไปใช้ run_scan แทน)
    """
    last_date = local_data_last_date()
    if last_date is None:
        return None
    age = (datetime.date.today() - last_date).days
    if age > LOCAL_MAX_AGE_DAYS:
        return None

    candidates = coarse_candidates()
    data, rate_limited = _fetch_all(candidates)
    hits = []
    unknowns = []  # ราคา/วอลุ่มพุ่ง แต่ Yahoo ไม่มีข้อมูลวันงบ (หุ้นเล็กบางตัว)
    for sym, d in data.items():
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
    return hits, len(_local_parquet_files()), last_date, unknowns, rate_limited


def run_scan():
    """คืน (hits, scanned_count, rate_limited) — hits เรียงตามคะแนนสัญญาณ"""
    symbols = load_universe()
    data, rate_limited = _fetch_all(symbols)
    hits = []
    for sym, d in data.items():
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
    return hits, len(symbols), rate_limited


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
                  unknowns=None, unknowns_title=None, unknowns_hint=None,
                  rate_limited=None):
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
        # ค่า default = ข้อความของโหมด parquet (เจอราคาพุ่งแต่ไม่รู้วันงบ)
        # โหมดข่าวแจ้งงบส่งหัวข้อ/คำแนะนำของตัวเองมาแทน (unknowns_hint=""
        # คือไม่ต้องมีบรรทัดแนะนำ)
        title = unknowns_title or "⚠️ ราคา/วอลุ่มพุ่งใน 7 วันนี้ แต่ไม่มีข้อมูลวันงบ:"
        hint = unknowns_hint
        if hint is None:
            hint = ("ถ้าตัวไหนเพิ่งออกงบจริง บันทึกวันเอง: งบ ชื่อหุ้น 13/11/2569"
                    if not html else
                    "ถ้าตัวไหนเพิ่งออกงบจริง บันทึกวันเอง: <code>งบ ชื่อหุ้น 13/11/2569</code>")
        lines += ["", title, shown]
        if hint:
            lines.append(hint)

    if rate_limited:
        shown = ", ".join(rate_limited[:12])
        if len(rate_limited) > 12:
            shown += f" ...(+{len(rate_limited) - 12})"
        lines += ["",
                  f"⏳ โดน Yahoo จำกัดคำขอ — ยังไม่ได้ตรวจ {len(rate_limited)} ตัว:",
                  shown,
                  "รอสัก 10-15 นาทีแล้วพิมพ์ สแกน อีกครั้ง" if not html
                  else "รอสัก 10-15 นาทีแล้วพิมพ์ <code>สแกน</code> อีกครั้ง"]

    if hits:
        lines.append("")
        lines.append("เก็บเข้าลิสต์: พิมพ์ ติดตาม <ชื่อหุ้น>" if not html
                     else "เก็บเข้าลิสต์: <code>ติดตาม ชื่อหุ้น</code>")
    return "\n".join(lines)


# หัวข้อ ⚠️ ของโหมดข่าวแจ้งงบ (telegram_bot ใช้ด้วย — ให้ข้อความตรงกัน)
FILINGS_UNKNOWNS_TITLE = "⚠️ แจ้งงบแล้วแต่ดึงข้อมูลราคาจาก Yahoo ไม่ได้:"


def filings_source_note(dropped):
    # จำนวน candidate แสดงในบรรทัด "(ตรวจ N ตัว...)" ของ format_report อยู่แล้ว
    note = "โหมดข่าวแจ้งงบ • คัดเฉพาะบริษัทที่แจ้งงบจริง (ข่าวเว็บ SET + คลังวันงบ)"
    if dropped:
        note += f" — กรองหยาบด้วยข้อมูล Trading_Dashboard ออก {dropped} ตัว"
    return note


if __name__ == "__main__":
    print("\nกำลังสแกน... (ประมาณ 1-3 นาที)\n")
    res = run_filings_scan()
    if res is not None:
        hits, n, no_data, dropped, rate_limited = res
        print(format_report(hits, n, html=False,
                            source_note=filings_source_note(dropped),
                            unknowns=no_data,
                            unknowns_title=FILINGS_UNKNOWNS_TITLE,
                            unknowns_hint="", rate_limited=rate_limited))
    else:
        full = run_full_scan()
        if full is not None:
            hits, n, last_date, unknowns, rate_limited = full
            note = f"โหมดทั้งตลาด • ข้อมูล Trading_Dashboard ถึง {last_date:%d/%m/%Y}"
        else:
            hits, n, rate_limited = run_scan()
            note, unknowns = "โหมดรายชื่อหลัก (ข้อมูล Trading_Dashboard ไม่สด)", None
        print(format_report(hits, n, html=False, source_note=note,
                            unknowns=unknowns, rate_limited=rate_limited))
    print(f"\nบันทึกผลลง {os.path.basename(LOG_PATH)} แล้ว\n")
