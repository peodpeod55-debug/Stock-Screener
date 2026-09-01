"""ไม้เงา (shadow trades) — บอทเทรดกระดาษเองทุกตัวที่ติดสแกน แล้ววัดผลจริง

ทุกแถวใน scan_log.csv คือไม้จำลองหนึ่งไม้ (นับครั้งแรกต่อ ticker+วันงบ):
  เข้า     : ราคาเปิดวันทำการถัดจากวันสแกน (เห็นรายงาน 17:30 → ซื้อเช้าวันรุ่งขึ้น)
  ตัด      : ราคาปิดหลุด pre_earn_low (เส้น ⛔) → ออกที่ราคาปิดวันนั้น
  หมดเวลา : ถือครบ HOLD_DAYS วันทำการ → ออกที่ราคาปิด (ตรง horizon ของ backtest)

ตามปรัชญาระบบ "คำนวณย้อนหลังได้เสมอ": ไม่มี job เฝ้ารายวัน ไม่มี state ที่พัง
ตอนปิดบอทนาน — replay จากราคาย้อนหลังทั้งหมด ไม้ที่ปิดแล้ว cache ลง
shadow_log.csv (append-only) เรียกซ้ำจะยิง Yahoo เฉพาะไม้ที่ยังเปิด

ตอบคำถามที่ backtest ตอบไม่ได้: ระบบคะแนน "ตอนนี้" ยังทำงานไหม ด้วยข้อมูล
forward สดจาก pipeline จริง (ข่าว SET → สแกน → เข้า) — ไม่ใช้เงินสักบาท
รันเองก็ได้ (python shadow.py) หรือพิมพ์ "เงา" ในแชทบอท
"""
import os
import csv
import datetime
from collections import defaultdict

import stock_core

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCAN_LOG_PATH = os.path.join(_BASE_DIR, "scan_log.csv")
SHADOW_LOG_PATH = os.path.join(_BASE_DIR, "shadow_log.csv")

HOLD_DAYS = 20  # ถือสูงสุดกี่วันทำการ (เท่า horizon ยาวสุดของ stats/backtest)

_SHADOW_COLUMNS = ["scan_date", "ticker", "earn_date", "score",
                   "entry_date", "entry", "stop",
                   "exit_date", "exit", "reason", "ret_pct", "r"]


def _bands():
    """ช่วงคะแนนชุดเดียวกับ stats.py (import ตอนใช้ กันวงกลม stats↔shadow)"""
    import stats
    return stats.BANDS


# ── อ่านไม้จาก scan_log.csv ─────────────────────────────────────

def _load_scan_entries():
    """แถวสแกนที่ใช้เป็นไม้เงาได้ — ครั้งแรกต่อ (ticker, วันงบ) เท่านั้น
    (หุ้นเดิมติดสแกนซ้ำหลายวันขณะยังเข้าเกณฑ์ = ไม้เดียว ไม่ใช่หลายไม้)"""
    entries, seen = [], set()
    try:
        with open(SCAN_LOG_PATH, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    e = {
                        "scan_date": datetime.date.fromisoformat(r["scan_date"]),
                        "ticker": r["ticker"],
                        "earn_date": r["earn_date"],
                        "stop": float(r["pre_earn_low"]),
                        "score": int(r["score"]),
                    }
                except (KeyError, ValueError, TypeError):
                    continue  # แถวเก่า/เสีย ไม่มีข้อมูลพอทำไม้เงา — ข้าม
                key = (e["ticker"], e["earn_date"])
                if key in seen:
                    continue
                seen.add(key)
                entries.append(e)
    except FileNotFoundError:
        pass
    return entries


# ── cache ไม้ที่ปิดแล้ว (shadow_log.csv) ────────────────────────

def load_closed():
    """ไม้เงาที่ปิดแล้วจาก cache — ไฟล์ไม่มี/แถวเสียคืนเท่าที่อ่านได้"""
    out = []
    try:
        with open(SHADOW_LOG_PATH, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    out.append({
                        "scan_date": r["scan_date"],
                        "ticker": r["ticker"],
                        "earn_date": r["earn_date"],
                        "score": int(r["score"]),
                        "entry_date": r["entry_date"],
                        "entry": float(r["entry"]) if r.get("entry") else None,
                        "stop": float(r["stop"]) if r.get("stop") else None,
                        "exit_date": r["exit_date"],
                        "exit": float(r["exit"]) if r.get("exit") else None,
                        "reason": r["reason"],
                        "ret_pct": float(r["ret_pct"]) if r.get("ret_pct") else None,
                        "r": float(r["r"]) if r.get("r") else None,
                    })
                except (KeyError, ValueError, TypeError):
                    continue
    except FileNotFoundError:
        pass
    return out


def _append_closed(rows):
    if not rows:
        return
    is_new = not os.path.exists(SHADOW_LOG_PATH)
    try:
        with open(SHADOW_LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(_SHADOW_COLUMNS)
            for t in rows:
                w.writerow([
                    t["scan_date"], t["ticker"], t["earn_date"], t["score"],
                    t["entry_date"],
                    f"{t['entry']:.2f}" if t["entry"] is not None else "",
                    f"{t['stop']:.2f}" if t["stop"] is not None else "",
                    t["exit_date"],
                    f"{t['exit']:.2f}" if t["exit"] is not None else "",
                    t["reason"],
                    f"{t['ret_pct']:.2f}" if t["ret_pct"] is not None else "",
                    f"{t['r']:.2f}" if t["r"] is not None else "",
                ])
    except Exception:
        pass  # เขียน cache ไม่ได้ (เช่นเปิดใน Excel) — รอบหน้าคำนวณใหม่ได้เสมอ


# ── replay ไม้หนึ่งไม้จากราคาย้อนหลัง ───────────────────────────

def _simulate(entry, hist):
    """คืน dict ไม้เงา: reason = stop/time (ปิดแล้ว), open (ยังถือ),
    waiting (ยังไม่ถึงวันเข้า), skip (เปิดมาก็หลุด ⛔ แล้ว — ไม่เข้าตามกติกา)"""
    dates = list(hist.index.date)
    opens, closes = hist["Open"], hist["Close"]
    start = next((i for i, dt in enumerate(dates) if dt > entry["scan_date"]), None)

    base = {**entry, "earn_date": entry["earn_date"],
            "scan_date": entry["scan_date"].isoformat()}
    if start is None:
        return {**base, "reason": "waiting", "entry_date": "", "entry": None,
                "exit_date": "", "exit": None, "ret_pct": None, "r": None}

    price_in = float(opens.iloc[start])
    stop = entry["stop"]
    base.update({"entry_date": dates[start].isoformat(), "entry": price_in})

    def result(reason, j=None, price_out=None):
        ret = ((price_out - price_in) / price_in * 100
               if price_out is not None else None)
        r = ((price_out - price_in) / (price_in - stop)
             if price_out is not None and price_in > stop else None)
        return {**base, "reason": reason,
                "exit_date": dates[j].isoformat() if j is not None else "",
                "exit": price_out, "ret_pct": ret,
                "r": round(r, 4) if r is not None else None,
                "days_held": (j - start + 1) if j is not None
                else len(dates) - start}

    # เปิดมาก็ต่ำกว่า/เท่ากับเส้น ⛔ แล้ว = สัญญาณเสียก่อนได้เข้า — ไม่นับไม้
    if price_in <= stop:
        return result("skip")

    for j in range(start, len(dates)):
        if float(closes.iloc[j]) < stop:
            return result("stop", j, float(closes.iloc[j]))
        if j - start + 1 >= HOLD_DAYS:
            return result("time", j, float(closes.iloc[j]))
    # ยังไม่จบ — รายงานสถานะปัจจุบันด้วยราคาปิดล่าสุด (ไม่เข้า cache)
    last = len(dates) - 1
    out = result("open", last, float(closes.iloc[last]))
    out["exit_date"] = ""
    return out


def update_shadow():
    """อัปเดตไม้เงาทั้งหมด: ไม้ใหม่/ยังเปิดยิง Yahoo ตัวละครั้ง ไม้ปิดแล้วอ่าน cache

    คืน {"closed": [...], "open": [...], "waiting": n, "failed": [tickers]}
    ไม้ที่เพิ่งปิดในรอบนี้ถูกต่อท้าย shadow_log.csv ให้เอง"""
    entries = _load_scan_entries()
    cached = load_closed()
    done = {(t["ticker"], t["earn_date"]) for t in cached}
    pending = [e for e in entries if (e["ticker"], e["earn_date"]) not in done]

    by_ticker = defaultdict(list)
    for e in pending:
        by_ticker[e["ticker"]].append(e)

    newly_closed, open_, waiting, failed = [], [], 0, []
    for ticker, items in by_ticker.items():
        try:
            # ราคาจริงไม่ adjust — stop ใน scan_log เป็นราคาจริง ณ วันสแกน และกติกา
            # ⛔ ของ watch job ก็เทียบราคาซื้อขายจริง (หุ้น SET ขึ้น XD หลังงบบ่อย —
            # ราคา adjusted จะถูก rescale หนี stop → ⛔ ปลอม/R เพี้ยน cache ถาวร)
            fetched = stock_core.fetch_history(ticker, period="1y", auto_adjust=False)
        except Exception:
            fetched = None
        if fetched is None:
            failed.append(ticker)
            continue
        _, hist = fetched
        for e in items:
            t = _simulate(e, hist)
            if t["reason"] in ("stop", "time", "skip"):
                newly_closed.append(t)
            elif t["reason"] == "open":
                open_.append(t)
            else:
                waiting += 1

    _append_closed(newly_closed)
    return {"closed": cached + newly_closed, "open": open_,
            "waiting": waiting, "failed": failed}


# ── รายงาน ──────────────────────────────────────────────────────

def _sym(ticker):
    return ticker.replace(".BK", "")


def summary_lines(closed, indent=""):
    """สรุปไม้เงาที่ปิดแล้ว แยกช่วงคะแนน (ใช้ทั้งรายงานเต็มและท้าย "สถิติ")
    ไม้ reason=skip ไม่นับ (ไม่ได้เข้าจริง)"""
    trades = [t for t in closed if t["reason"] in ("stop", "time")
              and t["ret_pct"] is not None]
    if not trades:
        return []
    pcts = [t["ret_pct"] for t in trades]
    wins = sum(1 for x in pcts if x > 0)
    lines = [f"{indent}ปิดแล้ว {len(trades)} ไม้: ชนะ {wins} "
             f"({wins / len(trades) * 100:.0f}%) เฉลี่ย {sum(pcts) / len(pcts):+.1f}%"]
    rs = [t["r"] for t in trades if t["r"] is not None]
    if rs:
        lines.append(f"{indent}R เฉลี่ย {sum(rs) / len(rs):+.2f}R  รวม {sum(rs):+.1f}R")
    for lo, hi, label in _bands():
        band = [t for t in trades if lo <= t["score"] <= hi]
        if not band:
            continue
        bp = [t["ret_pct"] for t in band]
        bw = sum(1 for x in bp if x > 0)
        brs = [t["r"] for t in band if t["r"] is not None]
        r_txt = f"  {sum(brs) / len(brs):+.2f}R" if brs else ""
        lines.append(f"{indent}{label}: ชนะ {bw}/{len(band)} "
                     f"เฉลี่ย {sum(bp) / len(bp):+.1f}%{r_txt}")
    return lines


def build_shadow_report(html: bool = True) -> str:
    """คำสั่ง "เงา": อัปเดตไม้เงาทั้งหมดแล้วรายงานเต็ม (ยิง Yahoo เฉพาะไม้ค้าง)"""
    b = (lambda t: f"<b>{t}</b>") if html else (lambda t: t)
    res = update_shadow()
    closed, open_ = res["closed"], res["open"]

    if not closed and not open_ and not res["waiting"]:
        return ("👻 ยังไม่มีไม้เงา — ไม้เงาเกิดจากหุ้นที่ติดสแกน (17:30 หรือพิมพ์ "
                "\"สแกน\") ระบบจะจำลองเข้าเช้าวันถัดไปแล้ววัดผลให้เองตามกติกา "
                "⛔ / ถือครบ 20 วัน\nปล่อยให้สแกนเก็บผลไปก่อน แล้วค่อยกลับมาดูครับ")

    lines = [f"👻 {b('ไม้เงา — ระบบเทรดกระดาษเองจากผลสแกน')}",
             f"(เข้าเปิดวันถัดจากวันสแกน • ตัดปิดหลุด ⛔ • ถือสูงสุด {HOLD_DAYS} วันทำการ)",
             ""]

    if open_:
        lines.append(b(f"📂 ยังถืออยู่ ({len(open_)})"))
        for t in sorted(open_, key=lambda x: x["entry_date"]):
            r_txt = f"  {t['r']:+.2f}R" if t["r"] is not None else ""
            lines.append(
                f"• {_sym(t['ticker'])} [{t['score']}] เข้า {t['entry']:.2f} "
                f"({t['entry_date'][5:]}) ตอนนี้ {t['exit']:.2f} "
                f"({t['ret_pct']:+.1f}%){r_txt} — วันที่ {t['days_held']}/{HOLD_DAYS}"
            )
        lines.append("")

    sm = summary_lines(closed)
    if sm:
        lines.append(b("📁 ผลรวมไม้ที่ปิดแล้ว (แยกช่วงคะแนน)"))
        lines += sm
        recent = sorted((t for t in closed if t["reason"] in ("stop", "time")),
                        key=lambda t: t["exit_date"])[-8:]
        if recent:
            lines.append("")
            lines.append(b(f"ไม้ปิดล่าสุด ({len(recent)})"))
            for t in reversed(recent):
                mark = "✅" if (t["ret_pct"] or 0) > 0 else "❌"
                why = "⛔" if t["reason"] == "stop" else "ครบเวลา"
                lines.append(
                    f"{mark} {_sym(t['ticker'])} [{t['score']}] "
                    f"{t['entry']:.2f}→{t['exit']:.2f} = {t['ret_pct']:+.1f}% ({why})"
                )
        lines.append("")

    skipped = sum(1 for t in closed if t["reason"] == "skip")
    notes = []
    if res["waiting"]:
        notes.append(f"รอเข้าเช้าวันทำการถัดไป {res['waiting']} ไม้")
    if skipped:
        notes.append(f"ไม่ได้เข้า {skipped} ไม้ (เปิดมาก็หลุด ⛔ แล้ว)")
    if res["failed"]:
        notes.append("ดึงราคาไม่ได้: "
                     + ", ".join(_sym(t) for t in res["failed"][:10]))
    if notes:
        lines.append("(" + " • ".join(notes) + ")")
    lines.append("ตัวเลขไม่รวมค่าธรรมเนียม — ไว้เทียบกับไม้จริงใน \"สถิติ\"")
    return "\n".join(lines)


if __name__ == "__main__":
    print("\nกำลังอัปเดตไม้เงาจาก scan_log.csv ...\n")
    print(build_shadow_report(html=False))
    print()
