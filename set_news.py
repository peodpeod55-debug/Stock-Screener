"""ดึงข่าวบริษัทจากเว็บ SET — ตรวจว่าบริษัทไหน "แจ้งผลประกอบการแล้ว"

เว็บ SET มีระบบกันบอท (Incapsula) จึงต้องเปิดเบราว์เซอร์จริงผ่าน Playwright
(ดึงครั้งละ ~15-30 วินาที) แล้วเรียก API ภายในของหน้าเว็บจาก context เดียวกัน

หน้าที่หลัก:
  1. หาข่าวประเภท "งบออกแล้ว" — F45 (สรุปผลการดำเนินงาน) / งบการเงิน / MD&A
  2. บันทึกวันงบเข้าระบบอัตโนมัติ (stock_core.set_manual_earnings_date)
     → คะแนนสัญญาณ/สแกน/แจ้งเตือนใช้วันงบที่แม่นกว่า Yahoo ทันที
  3. คืนรายการข่าวใหม่ให้บอทส่งแจ้งเตือน

รันเองได้: python set_news.py   (ดูรายชื่อบริษัทที่แจ้งงบใน 1-2 วันล่าสุด)
"""
import os
import re
import csv
import json
import datetime
import threading
from zoneinfo import ZoneInfo

import scanner
import stock_core

_BKK = ZoneInfo("Asia/Bangkok")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SEEN_PATH = os.path.join(_BASE_DIR, "news_seen.json")

NEWS_PAGE = "https://www.set.or.th/th/market/news-and-alert/news?newsType=company"
_API_PATH = "/api/cms/v1/news/set"
PER_PAGE = 3000  # วันพีคฤดูงบ บริษัทแจ้งข่าวรวมกันหลายร้อยรายการ

# เปิดเบราว์เซอร์ทีละตัวพอ (job อัตโนมัติ + คำสั่งมือถือเรียกพร้อมกันได้)
_FETCH_LOCK = threading.Lock()


# ── ดึงข่าวผ่าน Playwright ──────────────────────────────────────

def fetch_company_news(days_back: int = 1, timeout_s: int = 90):
    """คืน list ข่าวบริษัท [{id, datetime, symbol, headline, url}]
    ตั้งแต่ days_back วันก่อนจนถึงวันนี้ (เรียงใหม่ → เก่า)

    เปิดหน้าเว็บจริงก่อนเพื่อผ่านระบบกันบอท แล้วเรียก API จากในหน้า
    ล้มเหลว → โยน exception ให้ผู้เรียกจัดการ (อย่าให้บอทหลักล้ม)
    """
    import urllib.parse as up
    from playwright.sync_api import sync_playwright

    today = datetime.datetime.now(_BKK).date()
    frm = (today - datetime.timedelta(days=days_back)).strftime("%d/%m/%Y")
    to = today.strftime("%d/%m/%Y")

    # API ของ SET ต้องมี header พิเศษที่ SPA แนบมา (ยิง fetch เองโดน 401)
    # → ดักคำขอของหน้าเว็บเองแล้วสลับช่วงวันที่/จำนวนต่อหน้าเป็นของเรา
    # คำขอเลยออกไปพร้อม header ครบ แล้วเก็บ JSON ที่ตอบกลับ
    def _rewrite(route):
        req_url = route.request.url
        if _API_PATH not in req_url:
            route.continue_()
            return
        parts = up.urlsplit(req_url)
        q = dict(up.parse_qsl(parts.query))
        q["fromDate"] = frm
        q["toDate"] = to
        q["perPage"] = str(PER_PAGE)
        route.continue_(url=parts._replace(query=up.urlencode(q)).geturl())

    with _FETCH_LOCK:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    locale="th-TH",
                    timezone_id="Asia/Bangkok",
                    viewport={"width": 1366, "height": 900},
                )
                # ใช้ฟังก์ชัน match แทน glob เพราะ query มี "/" ในวันที่
                # (glob ของ Playwright: "*" เดี่ยวไม่ match "/")
                ctx.route(lambda url: _API_PATH in url, _rewrite)
                page = ctx.new_page()
                # หน้า newsType=company จะเรียก API พร้อม sourceId=company
                # เอง — รอเก็บคำตอบของคำขอนั้น (ที่ถูกสลับพารามิเตอร์แล้ว)
                with page.expect_response(
                    lambda r: _API_PATH in r.url
                    and "sourceId=company" in r.url
                    and r.status == 200,
                    timeout=timeout_s * 1000,
                ) as resp_info:
                    page.goto(NEWS_PAGE, wait_until="domcontentloaded",
                              timeout=timeout_s * 1000)
                data = resp_info.value.json()
            finally:
                browser.close()

    items = []
    for grp in (data or {}).get("newsGroups") or []:
        items += grp.get("newsInfoList") or []
    paginate = (data or {}).get("paginateNews") or {}
    items += paginate.get("newsInfoList") or []

    out, seen_ids = [], set()
    for it in items:
        nid = str(it.get("id") or "")
        sym = (it.get("symbol") or "").strip().upper()
        headline = (it.get("headline") or "").strip()
        if not nid or not sym or not headline or nid in seen_ids:
            continue
        seen_ids.add(nid)
        try:
            dt = datetime.datetime.fromisoformat(it["datetime"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append({
            "id": nid,
            "datetime": dt,
            "symbol": sym,
            "headline": headline,
            "url": it.get("url") or "",
        })
    out.sort(key=lambda x: x["datetime"], reverse=True)
    return out


# ── ดึงเนื้อหาข่าวรายตัว (ใช้อ่านแบบ F45) ───────────────────────

F45_DETAIL_MAX = 15  # อ่านรายละเอียด F45 สูงสุดกี่ฉบับต่อรอบ (กันวันพีคใช้เวลานานเกิน)


def fetch_news_details(urls, timeout_s: int = 90):
    """เปิดหน้ารายละเอียดข่าวทีละ URL ในเบราว์เซอร์เดียว คืน {url: ข้อความในหน้า}

    เข้าหน้า detail ตรงๆ จะได้ "ไม่มีข้อมูล" — ต้องแวะหน้า list ก่อน
    ให้ session ผ่านระบบกันบอท แล้วค่อยไล่เปิดทีละหน้า
    URL ไหนเปิดไม่สำเร็จจะไม่อยู่ในผลลัพธ์ (ผู้เรียกต้องเช็คเอง)
    """
    from playwright.sync_api import sync_playwright

    out = {}
    if not urls:
        return out
    with _FETCH_LOCK:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    locale="th-TH",
                    timezone_id="Asia/Bangkok",
                    viewport={"width": 1366, "height": 900},
                    service_workers="block",
                )
                page = ctx.new_page()
                with page.expect_response(
                    lambda r: _API_PATH in r.url and r.status == 200,
                    timeout=timeout_s * 1000,
                ):
                    page.goto(NEWS_PAGE, wait_until="domcontentloaded",
                              timeout=timeout_s * 1000)
                for u in urls:
                    try:
                        page.goto(u, wait_until="domcontentloaded",
                                  timeout=timeout_s * 1000)
                        # รอจนเนื้อข่าวโหลด (แบบ F45 มีคำว่า "กำไร" เสมอ)
                        page.wait_for_function(
                            "document.body && document.body.innerText.includes('กำไร')",
                            timeout=20000,
                        )
                        out[u] = page.inner_text("body")
                    except Exception:
                        continue
            finally:
                browser.close()
    return out


# ── แกะตัวเลขจากแบบสรุปผลการดำเนินงาน (F45) ─────────────────────

def _extract_numbers(line: str):
    """ตัวเลขบนบรรทัด F45 — วงเล็บ = ค่าติดลบ, ข้าม "(แก้ไข)" ที่ไม่มีตัวเลข"""
    out = []
    for tok in re.findall(r"\(?\d[\d,]*(?:\.\d+)?\)?", line):
        neg = tok.startswith("(") or tok.endswith(")")
        try:
            val = float(tok.strip("()").replace(",", ""))
        except ValueError:
            continue
        out.append(-val if neg else val)
    return out


def parse_f45(text: str):
    """อ่านแบบสรุปผลการดำเนินงาน (F45) จากข้อความหน้าเว็บ

    คืน {"period", "year", "profit_cur", "profit_prior"} (กำไรหน่วย: บาท)
    หรือ None ถ้าอ่านไม่ได้ — เค้าโครง F45 เป็นมาตรฐานเดียวกันทุกบริษัท:
    บรรทัด "กำไร (ขาดทุน)" แรกคือกำไรสุทธิ [ปีนี้ ปีก่อน ...]
    (แบบราย 6/9 เดือนมี 4 คอลัมน์ — สองตัวแรกคือไตรมาสล่าสุดเสมอ)
    """
    if "F45" not in text:
        return None
    unit = 1000.0 if re.search(r"หน่วย\s*:\s*พันบาท", text) else 1.0

    m = re.search(r"ไตรมาสที่\s*(\d)", text)
    if m:
        period = f"Q{m.group(1)}"
    elif re.search(r"งวด\s*1\s*ปี|ประจำปี", text):
        period = "งบปี"
    else:
        m6 = re.search(r"งวด\s*(\d+)\s*เดือน", text)
        period = f"งวด {m6.group(1)} ด." if m6 else ""

    year = None
    ym = re.search(r"^ปี\s+(\d{4})\s+(\d{4})", text, re.M)
    if ym:
        year = ym.group(1)

    for line in text.splitlines():
        if "กำไร" not in line or "ต่อหุ้น" in line:
            continue
        nums = _extract_numbers(line)
        if len(nums) >= 2:
            return {
                "period": period,
                "year": year,
                "profit_cur": nums[0] * unit,
                "profit_prior": nums[1] * unit,
            }
    return None


def format_f45_summary(f45) -> str:
    """สรุปสั้นๆ เช่น "Q1/2569: กำไร 120.5 ลบ. (+45% YoY)" """
    cur, prior = f45["profit_cur"], f45["profit_prior"]
    cur_mb, prior_mb = cur / 1e6, prior / 1e6
    label = f45["period"] or "งวดล่าสุด"
    if f45["year"]:
        label += f"/{f45['year']}"

    if cur >= 0 and prior > 0:
        pct = (cur - prior) / prior * 100
        body = f"กำไร {cur_mb:,.1f} ลบ. ({pct:+.0f}% YoY)"
    elif cur >= 0 and prior <= 0:
        body = (f"พลิกเป็นกำไร {cur_mb:,.1f} ลบ. "
                f"(ปีก่อนขาดทุน {abs(prior_mb):,.1f} ลบ.)")
    elif cur < 0 and prior >= 0:
        body = f"พลิกเป็นขาดทุน {abs(cur_mb):,.1f} ลบ."
    else:
        pct = (abs(cur) - abs(prior)) / abs(prior) * 100 if prior else None
        trend = ""
        if pct is not None:
            trend = (f" (ขาดทุนเพิ่ม {pct:.0f}%)" if pct > 0
                     else f" (ขาดทุนลด {abs(pct):.0f}%)")
        body = f"ขาดทุน {abs(cur_mb):,.1f} ลบ.{trend}"
    return f"{label}: {body}"


# ── เกณฑ์ "งบโตแรง" (ตรงกับ workflow: โต ≥30% YoY หรือพลิกเป็นกำไร) ──

MIN_STRONG_GROWTH_PCT = 30


def f45_growth_pct(parsed):
    """%YoY เทียบปีก่อน — None ถ้าปีก่อนไม่มีกำไรให้เทียบ"""
    if parsed["profit_prior"] > 0:
        return (parsed["profit_cur"] - parsed["profit_prior"]) / parsed["profit_prior"] * 100
    return None


def f45_is_strong(parsed) -> bool:
    if parsed["profit_cur"] <= 0:
        return False
    if parsed["profit_prior"] <= 0:
        return True  # พลิกเป็นกำไร
    return f45_growth_pct(parsed) >= MIN_STRONG_GROWTH_PCT


# ── สะสมตัวเลขงบลงไฟล์ (ไว้วิเคราะห์ว่างบโต → drift แรงจริงไหม) ──

_RESULTS_PATH = os.path.join(_BASE_DIR, "earnings_results.csv")


def _log_f45_result(entry):
    """ต่อท้าย earnings_results.csv — กันซ้ำด้วย (หุ้น, งวด, ปี)
    เพราะคำสั่ง "ข่าวงบ" อ่าน F45 ฉบับเดิมซ้ำได้หลายรอบ"""
    parsed = entry.get("f45_data")
    if not parsed:
        return
    key = (entry["symbol"], parsed.get("period") or "", str(parsed.get("year") or ""))
    try:
        with open(_RESULTS_PATH, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r["symbol"], r["period"], r["year"]) == key:
                    return
    except FileNotFoundError:
        pass
    is_new = not os.path.exists(_RESULTS_PATH)
    try:
        with open(_RESULTS_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["news_datetime", "symbol", "period", "year",
                            "profit_mb", "profit_prior_mb", "yoy_pct", "summary"])
            g = f45_growth_pct(parsed)
            w.writerow([
                entry["datetime"].strftime("%Y-%m-%d %H:%M"),
                entry["symbol"],
                parsed.get("period") or "",
                parsed.get("year") or "",
                f"{parsed['profit_cur'] / 1e6:.2f}",
                f"{parsed['profit_prior'] / 1e6:.2f}",
                f"{g:.1f}" if g is not None else "",
                entry.get("f45") or "",
            ])
    except Exception:
        pass  # ไฟล์เปิดค้างใน Excel ฯลฯ — อย่าให้ล้มการแจ้งเตือน


def _attach_f45_summaries(by_symbol):
    """เติมสรุปตัวเลข F45 ("f45"/"f45_data") ให้หุ้นที่มีข่าว F45
    และบันทึกตัวเลขลง earnings_results.csv"""
    targets = {sym: e["f45_url"] for sym, e in by_symbol.items() if e.get("f45_url")}
    if not targets:
        return
    # จัดคิวเมื่อวันพีคเกินโควตา: หุ้นในลิสต์ติดตาม/universe ได้อ่านก่อน
    # ที่เหลือเรียงตามเวลาข่าวใหม่สุด
    watch = set(stock_core.get_watchlist())
    uni = set(scanner.load_universe())
    ordered = sorted(
        targets,
        key=lambda s: (0 if (s in watch or s in uni) else 1,
                       -by_symbol[s]["datetime"].timestamp()),
    )
    urls = [targets[s] for s in ordered[:F45_DETAIL_MAX]]
    try:
        details = fetch_news_details(urls)
    except Exception:
        return  # อ่านรายละเอียดไม่ได้ ไม่เป็นไร — แจ้งเตือนแบบไม่มีตัวเลขแทน
    for sym in ordered[:F45_DETAIL_MAX]:
        text = details.get(targets[sym])
        if not text:
            continue
        parsed = parse_f45(text)
        if parsed:
            by_symbol[sym]["f45"] = format_f45_summary(parsed)
            by_symbol[sym]["f45_data"] = parsed
            _log_f45_result(by_symbol[sym])


# ── แยกประเภทข่าว "งบออกแล้ว" ───────────────────────────────────

def classify_earnings_news(headline: str):
    """คืนชนิดข่าวงบ หรือ None ถ้าไม่เกี่ยว

    ระวังข่าวหลอก เช่น "ชี้แจงกรณีงบการเงิน..." (คำถามจาก ตลท.)
    จึงเช็ค "งบการเงิน" เฉพาะตอนขึ้นต้นหัวข้อเท่านั้น
    """
    h = headline.strip()
    if "สรุปผลการดำเนินงาน" in h:
        return "F45"
    if h.startswith("งบการเงิน"):
        return "งบการเงิน"
    if "คำอธิบายและวิเคราะห์ของฝ่ายจัดการ" in h or "MD&A" in h.upper():
        return "MD&A"
    return None


# ── สถานะข่าวที่เห็นแล้ว (กันแจ้งซ้ำ) ───────────────────────────

def _load_seen():
    try:
        with open(_SEEN_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seen(seen):
    # เก็บเฉพาะ 7 วันล่าสุด กันไฟล์โตไม่จำกัด
    cutoff = (datetime.datetime.now(_BKK) - datetime.timedelta(days=7)).isoformat()
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    try:
        with open(_SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ── หน้าที่หลักสองแบบ ───────────────────────────────────────────

def check_new_earnings_news(max_age_hours: float = 14, days_back: int = 1):
    """สำหรับ job อัตโนมัติ: คืนเฉพาะข่าวงบ "ที่ยังไม่เคยเห็น"
    จัดกลุ่มต่อหุ้น [{symbol, datetime, kinds, headlines}]

    ทุกข่าวงบที่เจอ (ใหม่หรือเก่า) จะถูกบันทึกวันงบเข้าระบบเสมอ
    แต่แจ้งเตือนเฉพาะข่าวอายุไม่เกิน max_age_hours ชั่วโมง
    (กันสแปมย้อนหลังตอนเพิ่งเริ่มใช้ครั้งแรก)

    days_back ปกติ 1 วัน — โหมดเก็บตกหลังบอทปิดนานส่งค่ามากกว่านั้น
    """
    news = fetch_company_news(days_back=days_back)
    seen = _load_seen()
    now = datetime.datetime.now(_BKK)
    by_symbol = {}
    for it in news:
        kind = classify_earnings_news(it["headline"])
        if kind is None or it["id"] in seen:
            continue
        seen[it["id"]] = it["datetime"].isoformat()
        stock_core.set_manual_earnings_date(it["symbol"], it["datetime"].date())
        if (now - it["datetime"]).total_seconds() / 3600 > max_age_hours:
            continue
        entry = by_symbol.setdefault(it["symbol"], {
            "symbol": it["symbol"],
            "datetime": it["datetime"],
            "kinds": [],
            "headlines": [],
        })
        if kind not in entry["kinds"]:
            entry["kinds"].append(kind)
        entry["headlines"].append(it["headline"])
        # บางบริษัทยื่น F45 หลายฉบับพร้อมกัน (เช่นแก้ไขงวดเก่า + งวดล่าสุด)
        # ลูปไล่จากข่าวใหม่ → เก่า จึงเก็บเฉพาะฉบับแรกที่เจอ (= ใหม่สุด)
        if kind == "F45" and "f45_url" not in entry:
            entry["f45_url"] = it["url"]
        if it["datetime"] > entry["datetime"]:
            entry["datetime"] = it["datetime"]
    _save_seen(seen)
    _attach_f45_summaries(by_symbol)
    return sorted(by_symbol.values(), key=lambda x: x["datetime"], reverse=True)


def list_earnings_news(days_back: int = 1):
    """สำหรับคำสั่งเรียกดูเอง: คืนข่าวงบทั้งหมดในช่วง (เห็นแล้วก็แสดง)
    จัดกลุ่มต่อหุ้น พร้อมบันทึกวันงบเข้าระบบให้ด้วย"""
    news = fetch_company_news(days_back=days_back)
    by_symbol = {}
    for it in news:
        kind = classify_earnings_news(it["headline"])
        if kind is None:
            continue
        stock_core.set_manual_earnings_date(it["symbol"], it["datetime"].date())
        entry = by_symbol.setdefault(it["symbol"], {
            "symbol": it["symbol"],
            "datetime": it["datetime"],
            "kinds": [],
        })
        if kind not in entry["kinds"]:
            entry["kinds"].append(kind)
        if kind == "F45" and "f45_url" not in entry:
            entry["f45_url"] = it["url"]
        if it["datetime"] > entry["datetime"]:
            entry["datetime"] = it["datetime"]
    _attach_f45_summaries(by_symbol)
    return sorted(by_symbol.values(), key=lambda x: x["datetime"], reverse=True)


if __name__ == "__main__":
    print("\nกำลังเช็คข่าว SET (~15-30 วินาที)...\n")
    rows = list_earnings_news(days_back=2)
    if not rows:
        print("ไม่พบบริษัทแจ้งผลประกอบการใน 2 วันล่าสุด")
    for r in rows:
        print(f"  {r['datetime']:%d/%m %H:%M}  {r['symbol']:<10} {'/'.join(r['kinds'])}")
        if r.get("f45"):
            print(f"  {'':<18}{r['f45']}")
    print(f"\nรวม {len(rows)} บริษัท (บันทึกวันงบเข้าระบบแล้ว)\n")
