"""โมดูลกลางสำหรับดึงข้อมูลหุ้น (ใช้ร่วมกันระหว่าง CLI และ Telegram bot)"""
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")
warnings.filterwarnings("ignore", message=".*Timestamp.utcnow is deprecated.*")

import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

import os
import csv
import json
import time
import random
import datetime
import threading
from zoneinfo import ZoneInfo
import pandas as pd
import yfinance as yf

_BKK = ZoneInfo("Asia/Bangkok")
from curl_cffi import requests as curl_requests
from yfinance.exceptions import YFRateLimitError

# Use a local cache folder next to this script so a corrupt cache can be
# cleared by just deleting the .yf_cache directory.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".yf_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)
try:
    yf.set_tz_cache_location(_CACHE_DIR)
except Exception:
    pass

# shared browser-impersonating session (avoids Yahoo rate-limiter)
_SESSION = curl_requests.Session(impersonate="chrome")

# session/แคชใช้ร่วมกันทุก thread (บอทรันสแกน+lookup พร้อมกันได้)
# → ยิง Yahoo ทีละคำขอผ่าน lock เดียว กันทั้ง race และ rate limit
_FETCH_LOCK = threading.Lock()


def _yf_ticker(symbol: str):
    return yf.Ticker(symbol, session=_SESSION)


def _retry(fn, tries: int = 4, base_delay: float = 1.5):
    """Run fn() with exponential backoff on rate-limit errors."""
    for attempt in range(tries):
        try:
            return fn()
        except YFRateLimitError:
            if attempt == tries - 1:
                raise
            time.sleep(base_delay * (2 ** attempt) + random.uniform(0, 0.5))
        except Exception as e:
            msg = str(e).lower()
            if "rate" in msg or "429" in msg or "too many" in msg:
                if attempt == tries - 1:
                    raise
                time.sleep(base_delay * (2 ** attempt) + random.uniform(0, 0.5))
            else:
                raise


def _pct(base, value):
    """% change from base to value, or None if base is unusable."""
    if base is None or value is None or base == 0:
        return None
    return (value - base) / base * 100


# ── caches (ลดจำนวน request กัน rate limit) ─────────────────────

# ชื่อบริษัท/สกุลเงินแทบไม่เปลี่ยน → เก็บถาวรในไฟล์ เรียก stock.info
# แค่ครั้งแรกที่เจอ ticker นั้นเท่านั้น
_NAME_CACHE_PATH = os.path.join(_CACHE_DIR, "ticker_names.json")
try:
    with open(_NAME_CACHE_PATH, encoding="utf-8") as f:
        _NAME_CACHE = json.load(f)
except Exception:
    _NAME_CACHE = {}


def _get_name_currency(stock, used_ticker):
    cached = _NAME_CACHE.get(used_ticker)
    if cached:
        return cached["name"], cached["currency"]
    try:
        info = _retry(lambda: stock.info) or {}
    except Exception:
        info = {}
    name = info.get("longName") or info.get("shortName") or used_ticker
    currency = info.get("currency", "THB")
    if info.get("longName") or info.get("shortName"):
        _NAME_CACHE[used_ticker] = {"name": name, "currency": currency}
        try:
            with open(_NAME_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(_NAME_CACHE, f, ensure_ascii=False)
        except Exception:
            pass
    return name, currency


# ผลลัพธ์ทั้งชุดจำไว้ชั่วคราว — ถามหุ้นตัวเดิมซ้ำจะตอบทันทีโดยไม่ยิง Yahoo
# ช่วงตลาดเปิดใช้ 60 วินาที (ให้ราคาตามทันตลาด) นอกเวลาใช้ 3 นาที
_RESULT_CACHE = {}  # normalized input -> (timestamp, data)


def _result_ttl_seconds():
    now = datetime.datetime.now(_BKK)
    if now.weekday() < 5 and datetime.time(10, 0) <= now.time() <= datetime.time(16, 45):
        return 60
    return 180


# ── วันประกาศงบ (จำอัตโนมัติจาก Yahoo + บันทึกเองได้) ──────────

_EARN_STORE_PATH = os.path.join(_CACHE_DIR, "earnings_dates.json")
try:
    with open(_EARN_STORE_PATH, encoding="utf-8") as f:
        _EARN_STORE = json.load(f)
except Exception:
    _EARN_STORE = {}

_EARN_REFRESH_SECONDS = 24 * 3600  # ดึงจาก Yahoo วันละครั้งพอ


def _save_earn_store():
    try:
        with open(_EARN_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(_EARN_STORE, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _base_symbol(ticker: str) -> str:
    return ticker.upper().strip().replace(".BK", "")


def set_manual_earnings_date(ticker_input: str, date_obj: datetime.date) -> str:
    """บันทึกวันประกาศงบด้วยมือ (เผื่อ Yahoo ไม่มีหรือวันเลื่อน)"""
    base = _base_symbol(ticker_input)
    entry = _EARN_STORE.setdefault(base, {})
    manual = set(entry.get("manual_dates", []))
    manual.add(date_obj.isoformat())
    entry["manual_dates"] = sorted(manual)
    _save_earn_store()
    _RESULT_CACHE.clear()  # ให้ผลลัพธ์รอบหน้าเห็นวันที่ใหม่ทันที
    return base


def _parse_iso_dates(strings):
    out = []
    for s in strings:
        try:
            out.append(datetime.date.fromisoformat(s))
        except ValueError:
            pass
    return sorted(out)


def _get_earnings_dates(stock, used_ticker):
    """รวมวันงบจาก Yahoo (refresh วันละครั้ง) + ที่บันทึกเอง

    Returns (all_dates, manual_dates) — วันที่บันทึกเองแยกออกมา
    เพราะให้ priority สูงกว่า (กรณีบริษัทเลื่อนวันประกาศ)
    """
    base = _base_symbol(used_ticker)
    entry = _EARN_STORE.setdefault(base, {})
    now = time.time()
    if now - entry.get("fetched", 0) > _EARN_REFRESH_SECONDS:
        try:
            ed = _retry(lambda: stock.get_earnings_dates(limit=8))
            if ed is not None and len(ed.index) > 0:
                entry["auto_dates"] = sorted({ts.date().isoformat() for ts in ed.index})
            entry["fetched"] = now
            _save_earn_store()
        except Exception:
            # ดึงวันงบไม่ได้ ไม่เป็นไร — อย่าให้กระทบการดูราคาหลัก
            entry["fetched"] = now
    manual = _parse_iso_dates(entry.get("manual_dates", []))
    all_dates = sorted(set(_parse_iso_dates(entry.get("auto_dates", []))) | set(manual))
    return all_dates, manual


def _earnings_day_reaction(hist, closes, last_earn):
    """ปฏิกิริยาราคา "วันที่ตลาดตอบรับงบ" (คำนวณย้อนหลังจากราคา 1 ปี)

    บริษัทไทยจำนวนมากประกาศงบหลังตลาดปิด → ปฏิกิริยาจริงไปเกิด
    วันซื้อขายถัดไป จึงดู 2 วันซื้อขายแรกตั้งแต่วันงบ แล้วเลือกวันที่
    วอลุ่มพุ่งกว่าเป็นวันตอบรับ
    """
    if last_earn is None:
        return None
    dates = list(hist.index.date)
    start = next((i for i, dt in enumerate(dates) if dt >= last_earn), None)
    if start is None or start == 0:
        return None

    vols = hist["Volume"]

    def day_metrics(i):
        prev_close = closes.iloc[i - 1]
        prior_vols = vols.iloc[max(0, i - 20):i]
        avg = float(prior_vols.mean()) if len(prior_vols) > 0 else None
        vr = (float(vols.iloc[i]) / avg) if avg else None
        return {
            "date": dates[i],
            "gap_pct": _pct(prev_close, hist["Open"].iloc[i]),
            "change_pct": _pct(prev_close, closes.iloc[i]),
            "vol_ratio": vr,
        }

    candidates = [day_metrics(i) for i in (start, start + 1) if i < len(dates)]
    if not candidates:
        return None
    return max(candidates, key=lambda m: m["vol_ratio"] or 0)


def _post_earnings_signals(hist, closes, price, last_earn):
    """สัญญาณติดตามหลังงบ ตามเกณฑ์:
    1) หลังงบทะลุไฮ 3 เดือน (วัดไฮจากช่วง *ก่อน* วันงบ)
    2) ยังทำไฮใหม่ต่อเนื่องอยู่ไหม (ไฮใหม่ล่าสุดกี่วันทำการก่อน)
    3) ไม่หลุด Low สุดก่อนวันงบ (ใช้ Low ต่ำสุด 5 วันทำการก่อนงบ)
    """
    if last_earn is None:
        return None
    dates = list(hist.index.date)
    start = next((i for i, dt in enumerate(dates) if dt >= last_earn), None)
    if start is None or start < 6:
        return None

    highs = hist["High"]
    lows = hist["Low"]

    # (1) ไฮ 3 เดือน (~63 วันทำการ) ก่อนวันงบ + ไฮ 5 วันก่อนงบ
    # (บันไดสองขั้น: ผ่านไฮ 5 วัน = เคลียร์แนวต้านระยะสั้น,
    #  ผ่านไฮ 3 เดือน = breakout เต็มตัว ได้คะแนนสูงกว่า)
    pre3m_high = float(highs.iloc[max(0, start - 63):start].max())
    pre5d_high = float(highs.iloc[start - 5:start].max())
    post_max_close = float(closes.iloc[start:].max())
    broke_pre3m_high = post_max_close > pre3m_high
    broke_pre5d_high = post_max_close > pre5d_high

    # (2) วันล่าสุดที่ราคาทำไฮใหม่หลังงบ (นับเป็นวันทำการย้อนจากวันล่าสุด)
    post_highs = highs.iloc[start:]
    running_max = post_highs.cummax()
    is_new_high = post_highs >= running_max  # วันที่ตั้งไฮใหม่ของช่วงหลังงบ
    last_new_high_pos = int(is_new_high.values.nonzero()[0][-1])
    days_since_new_high = len(post_highs) - 1 - last_new_high_pos

    # (3) Low สุด 5 วันทำการก่อนวันงบ = แนวรับสำคัญ
    pre_earn_low = float(lows.iloc[start - 5:start].min())
    above_pre_low = price > pre_earn_low

    return {
        "pre3m_high": pre3m_high,
        "broke_pre3m_high": broke_pre3m_high,
        "pre5d_high": pre5d_high,
        "broke_pre5d_high": broke_pre5d_high,
        "days_since_new_high": days_since_new_high,
        "pre_earn_low": pre_earn_low,
        "above_pre_low": above_pre_low,
        "pct_above_pre_low": _pct(pre_earn_low, price),
    }


SCORE_MAX = 12


def signal_score(d):
    """คะแนนสัญญาณหลังงบ 0-12 + ดาว

    น้ำหนักตามเกณฑ์ผู้ใช้: ทะลุไฮ 3 เดือนก่อนงบสำคัญสุด (3 คะแนน)
    ผ่านไฮ 5 วันก่อนงบเป็นบันไดขั้นแรก (1 คะแนน — หุ้นทะลุไฮ 3 เดือน
    จะได้ทั้งสองข้อรวม 4) ⭐⭐⭐ (10+) เป็นไปได้เฉพาะหุ้นที่ทะลุ
    ไฮ 3 เดือนเท่านั้น (ไม่ทะลุได้สูงสุด 9)
    """
    r = d.get("earn_reaction")
    s = d.get("post_signals")
    if r is None or s is None or r["change_pct"] is None:
        return None, ""
    pts = 0
    if r["change_pct"] >= 5:
        pts += 2
    elif r["change_pct"] >= 2.5:
        pts += 1
    if (r["vol_ratio"] or 0) >= 3:
        pts += 2
    elif (r["vol_ratio"] or 0) >= 2:
        pts += 1
    if s["broke_pre5d_high"]:
        pts += 1
    if s["broke_pre3m_high"]:
        pts += 3
    if s["days_since_new_high"] <= 1:
        pts += 3
    elif s["days_since_new_high"] <= 5:
        pts += 2
    if s["above_pre_low"]:
        pts += 1
    if pts >= 10:
        stars = "⭐⭐⭐"
    elif pts >= 7:
        stars = "⭐⭐"
    elif pts >= 4:
        stars = "⭐"
    else:
        stars = ""
    return pts, stars


# ── watchlist (หุ้นที่ติดตามหลังงบออก) ─────────────────────────

_WATCHLIST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "watchlist.json"
)


def get_watchlist():
    try:
        with open(_WATCHLIST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_watchlist(symbols):
    with open(_WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(symbols, f, ensure_ascii=False, indent=1)


def add_to_watchlist(ticker_input: str):
    base = _base_symbol(ticker_input)
    symbols = get_watchlist()
    if base not in symbols:
        symbols.append(base)
        _save_watchlist(symbols)
    return base, symbols


def remove_from_watchlist(ticker_input: str):
    base = _base_symbol(ticker_input)
    symbols = get_watchlist()
    if base in symbols:
        symbols.remove(base)
        _save_watchlist(symbols)
        return base, symbols
    return None, symbols


_RESULT_CACHE_MAX = 300


def _prune_result_cache():
    """กัน cache โตไม่จำกัดเมื่อบอทรันทิ้งไว้นานๆ"""
    if len(_RESULT_CACHE) < _RESULT_CACHE_MAX:
        return
    now = time.time()
    ttl = _result_ttl_seconds()
    for k in [k for k, (ts, _) in _RESULT_CACHE.items() if now - ts >= ttl]:
        _RESULT_CACHE.pop(k, None)
    while len(_RESULT_CACHE) >= _RESULT_CACHE_MAX:
        oldest = min(_RESULT_CACHE, key=lambda k: _RESULT_CACHE[k][0])
        _RESULT_CACHE.pop(oldest)


def get_stock_data(ticker_input: str, log: bool = False):
    """ดึงข้อมูลหุ้นหนึ่งตัว

    log=True → บันทึกผลลง lookup_log.csv ทุกครั้งที่ดึงข้อมูลสดจาก Yahoo
    (cache hit ไม่บันทึกซ้ำ)

    Returns dict of raw numbers, or None if ticker not found.
    Raises YFRateLimitError when Yahoo rate-limits us.
    """
    ticker = ticker_input.upper().strip()

    # ถามหุ้นตัวเดิมภายในช่วง TTL → ใช้ผลเดิมโดยไม่ยิง Yahoo ซ้ำ
    cached = _RESULT_CACHE.get(ticker)
    if cached and time.time() - cached[0] < _result_ttl_seconds():
        return cached[1]

    with _FETCH_LOCK:
        # เช็คซ้ำหลังได้ lock — thread อื่นอาจเพิ่งดึงตัวเดียวกันเสร็จ
        cached = _RESULT_CACHE.get(ticker)
        if cached and time.time() - cached[0] < _result_ttl_seconds():
            return cached[1]
        data = _fetch_stock_data(ticker)
    if data is not None and log:
        _log_lookup(data)
    return data


def fetch_history(ticker_input: str, period: str = "1y"):
    """ดึงเฉพาะราคาย้อนหลัง (ใช้คำนวณสถิติ) — คืน (used_ticker, DataFrame) หรือ None"""
    ticker = ticker_input.upper().strip()
    candidates = [ticker + ".BK", ticker] if "." not in ticker else [ticker]
    with _FETCH_LOCK:
        for t in candidates:
            s = _yf_ticker(t)
            h = _retry(lambda s=s: s.history(period=period))
            if h is not None and not h.empty:
                return t, h
    return None


def _fetch_stock_data(ticker: str):
    # รองรับหุ้นไทย (SET) อัตโนมัติ: ลอง .BK ก่อน แล้วค่อยลอง ticker ตรงๆ (US)
    candidates = [ticker + ".BK", ticker] if "." not in ticker else [ticker]

    stock = None
    used_ticker = None
    hist = None
    for t in candidates:
        s = _yf_ticker(t)
        # ดึง 1 ปีในครั้งเดียว: ได้ทั้ง 5 วัน / 3 เดือน / 52 สัปดาห์
        # จาก request เดียว (เมื่อก่อนต้องเรียก stock.info เพิ่มเพื่อเอา 52w)
        h = _retry(lambda s=s: s.history(period="1y"))
        if h is not None and not h.empty:
            stock, used_ticker, hist = s, t, h
            break
    if stock is None:
        return None

    name, currency = _get_name_currency(stock, used_ticker)

    closes = hist["Close"]
    price = closes.iloc[-1]
    prev_close = closes.iloc[-2] if len(closes) >= 2 else None
    open_today = hist["Open"].iloc[-1]

    # ── ปฏิกิริยาราคา (สำหรับดูการตอบสนองหลังประกาศงบ) ──
    day_change_pct = _pct(prev_close, price)
    gap_pct = _pct(prev_close, open_today)
    intraday_pct = _pct(open_today, price)  # จากราคาเปิดวันนี้ถึงล่าสุด
    chg_5d_pct = _pct(closes.iloc[-6], price) if len(closes) >= 6 else None
    chg_1m_pct = _pct(closes.iloc[-22], price) if len(closes) >= 22 else None

    # วอลุ่มวันล่าสุดเทียบค่าเฉลี่ย 20 วันก่อนหน้า (ไม่รวมวันล่าสุดเอง)
    vols = hist["Volume"]
    volume = float(vols.iloc[-1])
    prior = vols.iloc[:-1].tail(20)
    avg_vol_20 = float(prior.mean()) if len(prior) > 0 else None
    vol_ratio = (volume / avg_vol_20) if avg_vol_20 else None

    week = hist.tail(5)
    # หน้าต่าง 3 เดือนล่าสุดจากข้อมูล 1 ปี
    cutoff_3m = hist.index[-1] - pd.Timedelta(days=91)
    win3m = hist[hist.index >= cutoff_3m]

    # ── วันประกาศงบ + ราคาตั้งแต่งบออก ──
    earn_dates, manual_dates = _get_earnings_dates(stock, used_ticker)
    today = datetime.date.today()
    last_earn = max((dt for dt in earn_dates if dt <= today), default=None)
    # วันที่บันทึกเองชนะวันจาก Yahoo (กรณีบริษัทเลื่อนวันประกาศ)
    next_earn = min((dt for dt in manual_dates if dt > today), default=None) \
        or min((dt for dt in earn_dates if dt > today), default=None)
    since_earn_pct = None
    if last_earn is not None:
        # ฐานเทียบ = ราคาปิดวันซื้อขายสุดท้ายก่อนวันงบออก
        mask = [d < last_earn for d in hist.index.date]
        prior_closes = closes[mask]
        if len(prior_closes) > 0:
            since_earn_pct = _pct(prior_closes.iloc[-1], price)

    data = {
        "ticker": used_ticker,
        "name": name,
        "currency": currency,
        "last_date": hist.index[-1].strftime("%d/%m/%Y"),
        "price": price,
        "prev_close": prev_close,
        "open_today": open_today,
        "day_change_pct": day_change_pct,
        "gap_pct": gap_pct,
        "intraday_pct": intraday_pct,
        "chg_5d_pct": chg_5d_pct,
        "chg_1m_pct": chg_1m_pct,
        "volume": volume,
        "avg_vol_20": avg_vol_20,
        "vol_ratio": vol_ratio,
        "week_high": week["High"].max(),
        "week_low": week["Low"].min(),
        "hi3m": win3m["High"].max(),
        "lo3m": win3m["Low"].min(),
        # คำนวณจากข้อมูลราคา 1 ปีโดยตรง (สดกว่าค่าจาก stock.info)
        "hi52": hist["High"].max(),
        "lo52": hist["Low"].min(),
        "last_earnings": last_earn,
        "next_earnings": next_earn,
        "days_since_earnings": (today - last_earn).days if last_earn else None,
        "days_to_earnings": (next_earn - today).days if next_earn else None,
        "since_earnings_pct": since_earn_pct,
        "earn_reaction": _earnings_day_reaction(hist, closes, last_earn),
        "post_signals": _post_earnings_signals(hist, closes, price, last_earn),
        "fetched_time": datetime.datetime.now(_BKK).strftime("%H:%M"),
    }
    _prune_result_cache()
    _RESULT_CACHE[ticker] = (time.time(), data)
    return data


# ── บันทึกทุกการกดดูลงไฟล์ (ไว้วิเคราะห์ย้อนหลัง) ───────────────

_LOOKUP_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "lookup_log.csv"
)


def _log_lookup(d):
    """ต่อท้าย lookup_log.csv — กดดูตัวเดิมซ้ำระหว่างวันจะได้ time series
    ของปฏิกิริยาราคาเก็บไว้"""
    is_new = not os.path.exists(_LOOKUP_LOG_PATH)
    try:
        r = d.get("earn_reaction") or {}
        score, _ = signal_score(d)
        with open(_LOOKUP_LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow([
                    "datetime", "ticker", "price", "day_change_pct", "gap_pct",
                    "vol_ratio", "earn_date", "reaction_pct",
                    "since_earn_pct", "score",
                ])
            w.writerow([
                datetime.datetime.now(_BKK).strftime("%Y-%m-%d %H:%M:%S"),
                d["ticker"],
                f"{d['price']:.2f}",
                f"{d['day_change_pct']:.2f}" if d["day_change_pct"] is not None else "",
                f"{d['gap_pct']:.2f}" if d["gap_pct"] is not None else "",
                f"{d['vol_ratio']:.2f}" if d["vol_ratio"] is not None else "",
                d["last_earnings"].isoformat() if d["last_earnings"] else "",
                f"{r['change_pct']:.2f}" if r.get("change_pct") is not None else "",
                f"{d['since_earnings_pct']:.2f}" if d["since_earnings_pct"] is not None else "",
                score if score is not None else "",
            ])
    except Exception:
        pass  # เขียน log ไม่ได้ (เช่นเปิดใน Excel ค้าง) ไม่ต้องล้มการดูหุ้น


# ── format helpers ──────────────────────────────────────────────

def format_pct(current, target):
    """% ห่างจากราคาปัจจุบันไปยังระดับ target"""
    if not current or current == 0 or target is None:
        return "N/A"
    pct = (target - current) / current * 100
    arrow = "▲" if pct >= 0 else "▼"
    return f"{arrow}{abs(pct):.1f}%"


def format_signed_pct(pct):
    """แสดง % เปลี่ยนแปลงแบบมีเครื่องหมาย เช่น +3.2% / -1.5%"""
    if pct is None:
        return "N/A"
    return f"{pct:+.2f}%"


def format_volume(v):
    if v is None:
        return "N/A"
    if v >= 1e9:
        return f"{v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:.0f}"


def volume_flag(vol_ratio):
    """สัญลักษณ์เตือนวอลุ่มผิดปกติ (สำคัญช่วงงบออก)"""
    if vol_ratio is None:
        return ""
    if vol_ratio >= 3:
        return " 🔥🔥"
    if vol_ratio >= 2:
        return " 🔥"
    if vol_ratio >= 1.5:
        return " ⚡"
    return ""
