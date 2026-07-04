"""Backtest ระบบคะแนนสัญญาณหลังงบ กับข้อมูลย้อนหลัง ~2 ปี

ไม่ต้องรอเก็บข้อมูลจริงหลายเดือน — ใช้ราคาย้อนหลัง + วันงบเก่าจาก Yahoo
จำลองว่าถ้าระบบรันช่วงงบที่ผ่านมา หุ้นที่เข้าเกณฑ์สแกน (ตอบรับ ≥2%
วอลุ่ม ≥1.5 เท่า) แต่ละช่วงคะแนนไปต่อจริงแค่ไหน

วัดสองจุดตัดสินใจตาม workflow:
  จุด A "เย็นวันตอบรับ"    — คะแนน ณ วันแรก (เพดาน 11 เพราะความ
                             ต่อเนื่องยังไม่ถูกพิสูจน์) ถือ 5/10/20 วัน
  จุด B "เช็คซ้ำหลัง 10 วัน" — คะแนนเต็มสเกล 15 (ความต่อเนื่องนับแล้ว)
                             เข้าราคาปิดวันนั้น ถือต่อ 10/20 วัน

รัน:  python backtest.py            (universe 94 ตัว ใช้เวลา ~5-10 นาที)
ผลถูกบันทึกลง backtest_results.csv ไว้เปิดดูใน Excel
ข้อจำกัด: วันงบจาก Yahoo ไม่ครบทุกตัว (หุ้นเล็กมักขาด) — ตัวอย่างจึง
เอียงไปทางหุ้นใหญ่/กลาง และผลไม่รวมค่าธรรมเนียม
"""
import os
import csv
import time
import datetime

import scanner
import stock_core
from stock_core import _post_earnings_signals, signal_score

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_BASE_DIR, "backtest_results.csv")

ENTRY_MIN_CHANGE = scanner.MIN_CHANGE_PCT   # เกณฑ์เดียวกับสแกนจริง
ENTRY_MIN_VOL = scanner.MIN_VOL_RATIO
RECHECK_DAYS = 10                            # จุด B: กี่วันทำการหลังวันเข้า
HORIZONS_A = (5, 10, 20)
HORIZONS_B = (10, 20)

BANDS_A = [(9, 11, "9-11 (สูงสุดของวันแรก)"), (5, 8, "5-8"), (0, 4, "0-4")]
BANDS_B = [(13, 15, "⭐⭐⭐ 13-15"), (9, 12, "⭐⭐ 9-12"),
           (5, 8, "⭐ 5-8"), (0, 4, "0-4")]


def _get_past_earnings(used_ticker):
    """วันงบย้อนหลังจาก Yahoo — กรองวันซ้อนกัน (<30 วัน) ออก"""
    try:
        ed = stock_core._retry(
            lambda: stock_core._yf_ticker(used_ticker).get_earnings_dates(limit=12))
    except Exception:
        return []
    if ed is None or len(ed.index) == 0:
        return []
    out = []
    for d in sorted({ts.date() for ts in ed.index}):
        if out and (d - out[-1]).days < 30:
            continue
        out.append(d)
    return out


def _day_metrics(closes, vols, i):
    prev = closes.iloc[i - 1]
    chg = stock_core._pct(prev, closes.iloc[i])
    prior = vols.iloc[max(0, i - 20):i]
    avg = float(prior.mean()) if len(prior) > 0 else None
    vr = (float(vols.iloc[i]) / avg) if avg else None
    return chg, vr


def _score_asof(hist, earn_date, t):
    """คะแนน ณ สิ้นวันที่ index t — ใช้เฉพาะข้อมูลถึงวันนั้น (point-in-time)"""
    sub = hist.iloc[:t + 1]
    closes = sub["Close"]
    price = float(closes.iloc[-1])
    r = stock_core._earnings_day_reaction(sub, closes, earn_date)
    s = _post_earnings_signals(sub, closes, price, earn_date)
    score, _ = signal_score({"earn_reaction": r, "post_signals": s})
    return score


def run_backtest(symbols=None, verbose=True):
    symbols = symbols or scanner.load_universe()
    events = []
    today = datetime.date.today()
    no_dates = 0
    for n, sym in enumerate(symbols, 1):
        if verbose:
            print(f"\r  ดึงข้อมูล {n}/{len(symbols)}  {sym:<10}", end="", flush=True)
        try:
            fetched = stock_core.fetch_history(sym, period="2y")
        except Exception:
            fetched = None
        if not fetched:
            continue
        used, hist = fetched
        earns = _get_past_earnings(used)
        time.sleep(0.2)
        if not earns:
            no_dates += 1
            continue
        dates = list(hist.index.date)
        closes, vols = hist["Close"], hist["Volume"]

        for earn in earns:
            if earn > today:
                continue
            start = next((j for j, dt in enumerate(dates) if dt >= earn), None)
            # ต้องมีประวัติก่อนงบพอ (ไฮ 3 เดือน = 63 วัน + กันชน)
            if start is None or start < 70:
                continue
            # วันตอบรับที่เข้าเกณฑ์สแกน = วันงบหรือวันถัดไปที่ทั้งราคา
            # และวอลุ่มผ่านเกณฑ์ (เลียนแบบจังหวะที่สแกนเย็นจะเจอจริง)
            r_idx = None
            for t in (start, start + 1):
                if t >= len(dates):
                    continue
                chg, vr = _day_metrics(closes, vols, t)
                if chg is not None and chg >= ENTRY_MIN_CHANGE \
                        and (vr or 0) >= ENTRY_MIN_VOL:
                    r_idx = t
                    break
            if r_idx is None:
                continue

            score_a = _score_asof(hist, earn, r_idx)
            if score_a is None:
                continue
            entry = float(closes.iloc[r_idx])
            ev = {"symbol": sym, "earn_date": earn.isoformat(),
                  "entry_date": dates[r_idx].isoformat(),
                  "entry": entry, "score_A": score_a}
            for h in HORIZONS_A:
                j = r_idx + h
                ev[f"retA_{h}d"] = (stock_core._pct(entry, float(closes.iloc[j]))
                                    if j < len(dates) else None)

            t2 = r_idx + RECHECK_DAYS
            if t2 < len(dates):
                score_b = _score_asof(hist, earn, t2)
                entry2 = float(closes.iloc[t2])
                ev["score_B"] = score_b
                for h in HORIZONS_B:
                    j = t2 + h
                    ev[f"retB_{h}d"] = (stock_core._pct(entry2, float(closes.iloc[j]))
                                        if j < len(dates) else None)
            events.append(ev)
    if verbose:
        print(f"\r  ดึงข้อมูลครบ {len(symbols)} ตัว "
              f"(ไม่มีวันงบจาก Yahoo: {no_dates} ตัว)          ")
    return events


def _band_lines(events, score_key, ret_prefix, horizons, bands, b):
    lines = []
    for lo, hi, label in bands:
        rows = [e for e in events
                if e.get(score_key) is not None and lo <= e[score_key] <= hi]
        if not rows:
            continue
        lines.append(b(f"คะแนน {label}") + f" — {len(rows)} รายการ")
        for h in horizons:
            rets = [e[f"{ret_prefix}_{h}d"] for e in rows
                    if e.get(f"{ret_prefix}_{h}d") is not None]
            if not rets:
                continue
            wins = sum(1 for x in rets if x > 0)
            avg = sum(rets) / len(rets)
            lines.append(
                f"  +{h} วัน : ชนะ {wins}/{len(rets)} "
                f"({wins / len(rets) * 100:.0f}%)  เฉลี่ย {avg:+.1f}%")
        lines.append("")
    return lines


def build_report(events, html=False) -> str:
    b = (lambda t: f"<b>{t}</b>") if html else (lambda t: t)
    if not events:
        return "ไม่พบเหตุการณ์ที่เข้าเกณฑ์ — Yahoo อาจไม่มีวันงบย้อนหลังพอ"
    lines = [
        b("Backtest ระบบคะแนนกับงบย้อนหลัง ~2 ปี"),
        f"หุ้นที่เข้าเกณฑ์สแกน (ตอบรับ ≥{ENTRY_MIN_CHANGE:.0f}% "
        f"วอลุ่ม ≥{ENTRY_MIN_VOL}x): {len(events)} เหตุการณ์",
        "",
        b(f"— จุด A: เข้า ณ ปิดวันตอบรับ (คะแนนวันแรก เพดาน 11) —"),
        "",
    ]
    lines += _band_lines(events, "score_A", "retA", HORIZONS_A, BANDS_A, b)
    lines += [
        b(f"— จุด B: เช็คซ้ำหลัง {RECHECK_DAYS} วันทำการ "
          f"(คะแนนเต็มสเกล รวมความต่อเนื่อง) —"),
        "",
    ]
    lines += _band_lines(events, "score_B", "retB", HORIZONS_B, BANDS_B, b)
    lines.append("หมายเหตุ: ไม่รวมค่าธรรมเนียม/สลิปเพจ • วันงบจาก Yahoo "
                 "(หุ้นเล็กบางตัวขาด) • อดีตไม่การันตีอนาคต")
    return "\n".join(lines)


def _save_csv(events):
    if not events:
        return
    cols = ["symbol", "earn_date", "entry_date", "entry", "score_A"]
    cols += [f"retA_{h}d" for h in HORIZONS_A]
    cols += ["score_B"] + [f"retB_{h}d" for h in HORIZONS_B]
    try:
        with open(OUT_PATH, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for e in events:
                w.writerow([
                    (f"{e[c]:.2f}" if isinstance(e.get(c), float) else e.get(c, ""))
                    for c in cols
                ])
    except Exception:
        pass


if __name__ == "__main__":
    print("\nกำลัง backtest (universe ~94 ตัว ใช้เวลา ~5-10 นาที)...\n")
    events = run_backtest()
    print()
    print(build_report(events))
    _save_csv(events)
    print(f"\nรายละเอียดรายเหตุการณ์อยู่ใน {os.path.basename(OUT_PATH)}\n")
