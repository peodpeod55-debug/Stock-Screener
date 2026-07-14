"""สถิติย้อนหลังจาก scan_log.csv — หุ้นที่ติดสแกนแต่ละช่วงคะแนน ไปต่อจริงแค่ไหน

ตอบคำถามสำคัญ: คะแนนสัญญาณหลังงบ (0-15 ตาม SCORE_MAX ใน stock_core) ทำนายผลได้จริงไหม
วัดผลตอบแทน 5 / 10 / 20 วันทำการหลังวันสแกน แยกตามช่วงคะแนน
ต่อท้ายด้วยผลไม้จริงจาก trades_log.csv (คำสั่ง ซื้อ/ขาย ในบอท)
เทียบตามช่วงคะแนนเดียวกัน — execution ตามระบบทันหรือเปล่า
รันเองก็ได้ (python stats.py) หรือพิมพ์ "สถิติ" ในแชทบอท
"""
import os
import csv
import datetime
from collections import defaultdict

import stock_core

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(_BASE_DIR, "scan_log.csv")

HORIZONS = (5, 10, 20)  # วัดผลกี่วันทำการหลังวันสแกน
BANDS = [
    (13, 15, "⭐⭐⭐ (13-15)"),
    (9, 12, "⭐⭐ (9-12)"),
    (5, 8, "⭐ (5-8)"),
    (0, 4, "ไม่มีดาว (0-4)"),
]


def _load_rows():
    rows = []
    try:
        with open(LOG_PATH, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    scan_date = datetime.date.fromisoformat(r["scan_date"])
                    score = int(r["score"]) if r.get("score") else None
                except (ValueError, KeyError, TypeError):
                    continue
                if score is None:
                    continue
                rows.append({"date": scan_date, "ticker": r["ticker"], "score": score})
    except FileNotFoundError:
        pass
    # สแกนหลายรอบวันเดียวกันเจอหุ้นตัวเดิม → นับครั้งเดียว
    seen, out = set(), []
    for r in rows:
        key = (r["date"], r["ticker"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _forward_returns(rows):
    """คำนวณ % เปลี่ยนแปลงหลังวันสแกนของทุกแถว (ดึงราคาตัวละครั้ง)"""
    by_ticker = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(r)

    results = []
    for ticker, items in by_ticker.items():
        try:
            fetched = stock_core.fetch_history(ticker, period="2y")
        except Exception:
            fetched = None
        if fetched is None:
            continue
        _, hist = fetched
        closes = hist["Close"]
        dates = list(hist.index.date)
        for r in items:
            # ราคาฐาน = ปิดของวันซื้อขายสุดท้ายที่ไม่เกินวันสแกน
            idx = next(
                (i for i in range(len(dates) - 1, -1, -1) if dates[i] <= r["date"]),
                None,
            )
            if idx is None:
                continue
            base = float(closes.iloc[idx])
            if not base:
                continue
            rets = {}
            for h in HORIZONS:
                j = idx + h
                if j < len(closes):
                    rets[h] = (float(closes.iloc[j]) - base) / base * 100
            if rets:
                results.append({**r, "returns": rets})
    return results


def _trades_section(b, chat_id=None):
    """สรุปผลไม้จริงจาก trades_log.csv — เทียบกับผลระบบตามช่วงคะแนนเดียวกัน
    (ไม่มีไฟล์/ไม่เคยบันทึก = คืนลิสต์ว่าง ไม่แสดงหัวข้อ)"""
    closed, open_ = stock_core.pair_trades(stock_core.load_trades(chat_id))
    if not closed and not open_:
        return []

    lines = ["", b("💼 ผลไม้จริง (บันทึกด้วย ซื้อ/ขาย)")
             + f" — ปิดแล้ว {len(closed)} ไม้"
             + (f" เปิดอยู่ {len(open_)}" if open_ else "")]
    if not closed:
        lines.append("  ยังไม่มีไม้ที่ปิดครบวงจร — ขายเมื่อไหร่ตัวเลขจะขึ้นที่นี่")
        return lines

    def pct(p):
        return (p["sell"]["price"] - p["buy"]["price"]) / p["buy"]["price"] * 100

    pcts = [pct(p) for p in closed]
    wins = sum(1 for x in pcts if x > 0)
    lines.append(f"  ชนะ {wins}/{len(closed)} ({wins / len(closed) * 100:.0f}%)"
                 f"  เฉลี่ย {sum(pcts) / len(pcts):+.1f}%")
    rs = [(p["sell"]["price"] - p["buy"]["price"])
          / (p["buy"]["price"] - p["buy"]["stop"])
          for p in closed
          if p["buy"]["stop"] is not None and p["buy"]["price"] > p["buy"]["stop"]]
    if rs:
        lines.append(f"  R-multiple เฉลี่ย {sum(rs) / len(rs):+.2f}R"
                     f"  รวม {sum(rs):+.1f}R ({len(rs)} ไม้ที่รู้เส้น ⛔)")

    # แยกตามคะแนนระบบ ณ ตอนเข้า — วางเทียบกับตารางสถิติสแกนข้างบนตรงๆ
    band_lines = []
    for lo, hi, label in BANDS:
        band = [p for p in closed
                if p["buy"]["score"] is not None and lo <= p["buy"]["score"] <= hi]
        if not band:
            continue
        bp = [pct(p) for p in band]
        bw = sum(1 for x in bp if x > 0)
        band_lines.append(f"  {label}: ชนะ {bw}/{len(band)}"
                          f"  เฉลี่ย {sum(bp) / len(bp):+.1f}%")
    if band_lines:
        lines.append("  แยกตามคะแนน ณ ตอนเข้า:")
        lines += band_lines
    no_score = sum(1 for p in closed if p["buy"]["score"] is None)
    if no_score:
        lines.append(f"  (อีก {no_score} ไม้ไม่มีคะแนนบันทึกไว้ตอนเข้า)")
    return lines


def _shadow_section(b):
    """สรุปไม้เงาจาก cache (shadow_log.csv) — ไม่ยิงเครือข่าย ให้ "สถิติ" ตอบเร็ว
    (ไม้ที่ยังเปิด/เพิ่งครบกำหนดต้องพิมพ์ "เงา" ให้ระบบอัปเดตก่อน)"""
    import shadow
    lines = shadow.summary_lines(shadow.load_closed(), indent="  ")
    if not lines:
        return []
    return ["", b("👻 ไม้เงา (ระบบเทรดกระดาษเองจากผลสแกน)"), *lines,
            "  อัปเดต + ดูไม้ที่ยังเปิด: พิมพ์ \"เงา\""]


def build_stats_report(html: bool = True, chat_id=None) -> str:
    """chat_id: จำกัดส่วนไม้จริงเฉพาะ chat นั้น (None = ทุก chat — โหมด CLI)"""
    b = (lambda t: f"<b>{t}</b>") if html else (lambda t: t)
    rows = _load_rows()
    lines = []
    if not rows:
        lines.append(
            "📈 ยังไม่มีข้อมูลสะสมใน scan_log.csv พอให้วิเคราะห์\n"
            "ปล่อยให้ระบบสแกนเก็บผลไปสักระยะ (ผลสแกนทุกรอบถูกบันทึกอัตโนมัติ)\n"
            "แล้วค่อยพิมพ์ \"สถิติ\" อีกครั้งครับ"
        )
    else:
        results = _forward_returns(rows)
        if not results:
            lines.append(
                "⚠️ ยังคำนวณไม่ได้ — รายการในบันทึกอาจใหม่เกินไป "
                "(ยังไม่ครบ 5 วันทำการ) หรือดึงราคาย้อนหลังไม่สำเร็จ"
            )
        else:
            lines += [
                f"📈 {b('สถิติหุ้นติดสแกน')} — {len(results)} รายการจาก scan_log.csv",
                "(วัดจากราคาปิดวันสแกน → อีก 5/10/20 วันทำการ)",
                "",
            ]
            for lo, hi, label in BANDS:
                band = [r for r in results if lo <= r["score"] <= hi]
                if not band:
                    continue
                lines.append(b(f"คะแนน {label}") + f" — {len(band)} รายการ")
                for h in HORIZONS:
                    rets = [r["returns"][h] for r in band if h in r["returns"]]
                    if not rets:
                        continue
                    wins = sum(1 for x in rets if x > 0)
                    avg = sum(rets) / len(rets)
                    best = max(rets)
                    worst = min(rets)
                    lines.append(
                        f"  +{h} วัน : ชนะ {wins}/{len(rets)} ({wins / len(rets) * 100:.0f}%)"
                        f"  เฉลี่ย {avg:+.1f}%  (ดีสุด {best:+.1f} / แย่สุด {worst:+.1f})"
                    )
                lines.append("")

            pending = len(rows) - len(results)
            if pending > 0:
                lines.append(f"(อีก {pending} รายการยังวัดไม่ได้ — ใหม่เกินไปหรือดึงราคาไม่สำเร็จ)")
            lines.append("หมายเหตุ: ตัวเลขไม่รวมค่าธรรมเนียม และวัดแบบถือครบกำหนดเท่านั้น")

    lines += _shadow_section(b)
    lines += _trades_section(b, chat_id)
    return "\n".join(lines)


if __name__ == "__main__":
    print("\nกำลังคำนวณสถิติจาก scan_log.csv ...\n")
    print(build_stats_report(html=False))
    print()
