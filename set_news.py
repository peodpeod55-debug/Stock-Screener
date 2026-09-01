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
import logging
import time
import datetime
import threading
from zoneinfo import ZoneInfo

import scanner
import stock_core

log = logging.getLogger("bot.set_news")

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
_FILINGS_PATH = os.path.join(_BASE_DIR, "filings_log.csv")


def _log_f45_result(entry):
    """ต่อท้าย earnings_results.csv — กันซ้ำด้วย (หุ้น, งวด, ปี)
    เพราะคำสั่ง "ข่าวงบ" อ่าน F45 ฉบับเดิมซ้ำได้หลายรอบ

    บริษัทยื่นฉบับแก้ไขได้ (ตัวเลข/สรุปเปลี่ยนจากที่บันทึกไว้) — เคสนั้น
    ต่อท้ายเป็นแถวใหม่แทนการ skip: ไฟล์ยัง append-only (เก็บประวัติว่า
    เคยประกาศผิด) และฝั่งอ่าน (สรุปงบเช้า/ยืนยันรอบเช้า) เลือกแถว
    news_datetime ล่าสุดต่อ symbol อยู่แล้ว จึงได้เลขแก้ไขเองอัตโนมัติ"""
    parsed = entry.get("f45_data")
    if not parsed:
        return True  # ไม่มีตัวเลขให้เขียน = ไม่ใช่ความล้มเหลว
    key = (entry["symbol"], parsed.get("period") or "", str(parsed.get("year") or ""))
    new_vals = (f"{parsed['profit_cur'] / 1e6:.2f}",
                f"{parsed['profit_prior'] / 1e6:.2f}",
                entry.get("f45") or "")
    last_match = None  # ยื่นแก้ไขได้หลายครั้ง — ต้องเทียบกับฉบับล่าสุดของงวดนี้
    try:
        with open(_RESULTS_PATH, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r["symbol"], r["period"], r["year"]) == key:
                    last_match = r
    except OSError:
        pass  # ไฟล์ไม่มี/อ่านไม่ได้ (เช่น Excel ล็อค) — เทียบไม่ได้ก็ลองเขียนไปเลย
    if last_match is not None and new_vals == (
            last_match.get("profit_mb") or "",
            last_match.get("profit_prior_mb") or "",
            last_match.get("summary") or ""):
        return True  # ฉบับเดิมถูกอ่านซ้ำ — ไม่ใช่ฉบับแก้ไข (ไฟล์มีเลขนี้แล้ว)
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
                new_vals[0],
                new_vals[1],
                f"{g:.1f}" if g is not None else "",
                new_vals[2],
            ])
    except Exception as e:
        # ไฟล์เปิดค้างใน Excel ฯลฯ — อย่าให้ล้มการแจ้งเตือน แต่ต้องไม่เงียบ:
        # ผู้เรียกคงตัวนี้ไว้ในคิว f45_backlog ลองเขียนใหม่รอบหน้า
        log.warning("เขียน earnings_results.csv ไม่ได้ (%s: %s) — %s จะลองใหม่รอบหน้า",
                    type(e).__name__, e, entry.get("symbol"))
        return False
    return True


def _log_filing(news_datetime, symbol, kinds):
    """ต่อท้าย filings_log.csv — บันทึกดิบว่าใครแจ้งงบเมื่อไหร่ (ทุกข่าวงบ
    ที่ยังไม่เคยเห็น รวมข่าวเก่าเกิน max_age_hours ด้วย) ใช้ทำสรุปงบเช้า
    หนึ่งแถวต่อหนึ่งข่าว — หุ้นเดียวแจ้งหลายข่าวจะมีหลายแถว"""
    is_new = not os.path.exists(_FILINGS_PATH)
    try:
        with open(_FILINGS_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["news_datetime", "symbol", "kinds"])
            w.writerow([
                news_datetime.strftime("%Y-%m-%d %H:%M"),
                symbol,
                "/".join(kinds),
            ])
    except Exception as e:
        # ไฟล์เปิดค้างใน Excel ฯลฯ — อย่าให้ล้มการแจ้งเตือน แต่ต้องไม่เงียบ:
        # ผู้เรียกไม่ mark seen แล้วปล่อยรอบถัดไป retry (แถวหายจาก filings_log
        # = หายจากสรุปงบเช้า + สแกนโหมดข่าว ถาวร)
        log.warning("เขียน filings_log.csv ไม่ได้ (%s: %s) — %s จะลองใหม่รอบหน้า",
                    type(e).__name__, e, symbol)
        return False
    return True


# ── คิว F45 ที่ยังไม่ได้อ่านตัวเลข (วันพีคเกินโควตาต่อรอบ) ──────
# เดิมข่าวถูก mark "เห็นแล้ว" ทันทีแต่อ่านรายละเอียดได้แค่ F45_DETAIL_MAX
# ฉบับ/รอบ → ตัวที่เกินไม่มีวันได้ตัวเลข (ไม่เข้าสรุปงบเช้า/ยืนยันรอบเช้า)
# แก้ด้วยคิวถาวร: ตัวที่เกิน (หรือโหลดหน้าไม่สำเร็จ) เข้าคิวไว้ทยอยอ่าน
# รอบถัดไปจนหมด — ตัวเลขแค่มาช้า ไม่หายอีก

_F45_BACKLOG_PATH = os.path.join(_BASE_DIR, "f45_backlog.json")
F45_BACKLOG_MAX_TRIES = 3   # โหลดหน้าล้มเหลวได้กี่ครั้งก่อนตัดทิ้ง (ลิงก์ตาย)
F45_BACKLOG_MAX_DAYS = 7    # เก็บคิวไว้นานสุดกี่วัน (เท่ากรอบ news_seen)


def _load_f45_backlog():
    try:
        with open(_F45_BACKLOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_f45_backlog(backlog):
    # ตัดตัวที่เก่าเกินกรอบ / ลองครบโควตาแล้ว ออกตอนบันทึก
    cutoff = (datetime.datetime.now(_BKK)
              - datetime.timedelta(days=F45_BACKLOG_MAX_DAYS)).isoformat()
    pruned = {sym: it for sym, it in backlog.items()
              if it.get("datetime", "") >= cutoff
              and it.get("tries", 0) < F45_BACKLOG_MAX_TRIES}
    try:
        stock_core.save_json_atomic(_F45_BACKLOG_PATH, pruned)
    except Exception:
        pass


def _backlog_put(backlog, sym, url, dt, failed=False, corrected=False):
    """เพิ่ม/อัปเดตหุ้นในคิว — ฉบับใหม่สุดชนะ (บริษัทยื่นแก้ไขได้)
    failed=True คือเพิ่งโหลดหน้าไม่สำเร็จ → นับครั้งไว้ตัดตอนครบโควตา
    corrected ติดไปกับฉบับในคิว — ป้าย 📝 ต้องไม่หายแม้อ่านข้ามรอบ"""
    item = backlog.setdefault(sym, {"tries": 0})
    item["url"] = url
    item["datetime"] = dt.isoformat()
    item["corrected"] = corrected
    if failed:
        item["tries"] = item.get("tries", 0) + 1


def _attach_f45_summaries(by_symbol):
    """เติมสรุปตัวเลข F45 ("f45"/"f45_data") ให้หุ้นที่มีข่าว F45
    และบันทึกตัวเลขลง earnings_results.csv

    โควตาที่เหลือในรอบ (วันเงียบ) ใช้ล้างคิว f45_backlog ของรอบก่อนๆ
    — ตัวเลขจากคิวลง earnings_results.csv อย่างเดียว (แจ้งเตือนของมัน
    ผ่านไปแล้วตอนข่าวเข้าครั้งแรก)"""
    targets = {sym: e["f45_url"] for sym, e in by_symbol.items() if e.get("f45_url")}
    backlog = _load_f45_backlog()
    if not targets and not backlog:
        return
    # จัดคิวเมื่อวันพีคเกินโควตา: หุ้นในลิสต์ติดตาม/universe ได้อ่านก่อน
    # ที่เหลือเรียงตามเวลาข่าวใหม่สุด (union ทุกคน — เป็นแค่ลำดับความสำคัญ)
    watch = set(stock_core.get_all_watched_symbols())
    uni = set(scanner.load_universe())
    ordered = sorted(
        targets,
        key=lambda s: (0 if (s in watch or s in uni) else 1,
                       -by_symbol[s]["datetime"].timestamp()),
    )
    take = ordered[:F45_DETAIL_MAX]
    for sym in ordered[F45_DETAIL_MAX:]:  # เกินโควตารอบนี้ → เข้าคิว
        _backlog_put(backlog, sym, targets[sym], by_symbol[sym]["datetime"],
                     corrected=by_symbol[sym].get("f45_corrected", False))
    # โควตาที่เหลือหยิบจากคิวเก่ามาอ่าน — ข้ามตัวที่อ่านรอบนี้อยู่แล้ว
    spare = [s for s in backlog
             if s not in targets][: max(0, F45_DETAIL_MAX - len(take))]
    urls = ([targets[s] for s in take]
            + [backlog[s]["url"] for s in spare])
    if not urls:
        _save_f45_backlog(backlog)
        return
    try:
        details = fetch_news_details(urls)
    except Exception:
        _save_f45_backlog(backlog)  # คิวที่เพิ่งเพิ่มต้องไม่หาย
        return  # อ่านรายละเอียดไม่ได้ ไม่เป็นไร — แจ้งเตือนแบบไม่มีตัวเลขแทน
    for sym in take:
        text = details.get(targets[sym])
        if not text:
            # โหลดหน้าไม่สำเร็จ → เข้าคิวลองใหม่รอบหน้า
            _backlog_put(backlog, sym, targets[sym], by_symbol[sym]["datetime"],
                         failed=True,
                         corrected=by_symbol[sym].get("f45_corrected", False))
            continue
        parsed = parse_f45(text)
        if not parsed:
            backlog.pop(sym, None)  # อ่านเนื้อหาได้แต่ parse ไม่ได้ — ไม่ลองซ้ำ
            continue
        summary = format_f45_summary(parsed)
        if by_symbol[sym].get("f45_corrected"):
            summary += " (📝 ฉบับแก้ไข)"
        by_symbol[sym]["f45"] = summary
        by_symbol[sym]["f45_data"] = parsed
        if _log_f45_result(by_symbol[sym]):
            backlog.pop(sym, None)  # ตัวเลขลงไฟล์แล้ว — พ้นคิว
        else:
            # เขียน earnings_results ไม่ได้ (เช่น Excel ล็อค) — เข้าคิวลองเขียน
            # รอบหน้า (ไม่นับ tries: ไม่ใช่ลิงก์ตาย แค่ไฟล์ไม่ว่าง — แจ้งเตือน
            # รอบนี้ยังมีตัวเลขให้ผู้ใช้ตามปกติ)
            _backlog_put(backlog, sym, targets[sym], by_symbol[sym]["datetime"],
                         corrected=by_symbol[sym].get("f45_corrected", False))
    for sym in spare:
        item = backlog[sym]
        text = details.get(item["url"])
        if not text:
            item["tries"] = item.get("tries", 0) + 1
            continue
        parsed = parse_f45(text)
        if not parsed:
            del backlog[sym]  # อ่านเนื้อหาได้แต่ parse ไม่ได้ — ไม่ลองซ้ำ
            continue
        try:
            dt = datetime.datetime.fromisoformat(item["datetime"])
        except (KeyError, ValueError):
            dt = datetime.datetime.now(_BKK)
        summary = format_f45_summary(parsed)
        if item.get("corrected"):
            summary += " (📝 ฉบับแก้ไข)"
        if _log_f45_result({"symbol": sym, "datetime": dt,
                            "f45": summary,
                            "f45_data": parsed}):
            del backlog[sym]
        # เขียนล้ม → คงในคิว (ไม่นับ tries) ลองเขียนรอบหน้า
    _save_f45_backlog(backlog)


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
        stock_core.save_json_atomic(_SEEN_PATH, pruned)
    except Exception:
        pass


def news_data_age_hours():
    """อายุ (ชั่วโมง) ของข้อมูลข่าวรอบล่าสุดที่ดึงสำเร็จ — ดูจาก mtime ของ
    news_seen.json ซึ่งถูกเขียนทุกครั้งที่ poll สำเร็จ (แม้ไม่มีข่าวใหม่)

    ใช้สองที่: สแกนโหมดข่าวแจ้งงบเช็คว่าข้อมูลสดพอไหม และ news poll
    คำนวณว่าต้องดึงย้อนกี่วันเพื่ออุดช่องที่ขาด — คืน None ถ้ายังไม่เคยดึงเลย"""
    try:
        mtime = os.path.getmtime(_SEEN_PATH)
    except OSError:
        return None
    return (time.time() - mtime) / 3600


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
        stock_core.set_manual_earnings_date(it["symbol"], it["datetime"].date())
        if not _log_filing(it["datetime"], it["symbol"], [kind]):
            # เขียน filings_log ไม่ได้ (เช่น Excel เปิดค้าง) → ไม่ mark seen +
            # ไม่แจ้งรอบนี้ — รอบถัดไป (~10 นาที) เห็นข่าวนี้อีกและลองใหม่ทั้งชุด
            # (แจ้งช้าดีกว่าแถวหายถาวรจากสรุปงบเช้า/สแกนโหมดข่าว)
            continue
        seen[it["id"]] = it["datetime"].isoformat()
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
            # ป้ายฉบับแก้ไข — หัวข่าว SET ต่อท้ายด้วย "(แก้ไข)" เสมอ
            # ต้องตั้งคู่กับ f45_url เพื่อให้ป้ายตรงกับฉบับที่ถูกอ่านจริง
            entry["f45_corrected"] = "(แก้ไข" in it["headline"]
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
            entry["f45_corrected"] = "(แก้ไข" in it["headline"]
        if it["datetime"] > entry["datetime"]:
            entry["datetime"] = it["datetime"]
    _attach_f45_summaries(by_symbol)
    return sorted(by_symbol.values(), key=lambda x: x["datetime"], reverse=True)


# ── อ่านย้อนหลังสำหรับ "สรุปงบเช้า" (ไม่ยิง Playwright เลย) ──────

def load_results_since(dt):
    """คืน list ของ dict จาก earnings_results.csv ที่ news_datetime >= dt
    (profit แปลงเป็น float, yoy_pct เป็น float หรือ None)

    ไฟล์ไม่มี/อ่านพัง → คืนลิสต์ว่าง แถวเสียข้ามไปเงียบๆ ไม่ล้ม caller"""
    out = []
    try:
        with open(_RESULTS_PATH, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    news_dt = datetime.datetime.strptime(
                        r["news_datetime"], "%Y-%m-%d %H:%M").replace(tzinfo=_BKK)
                    if news_dt < dt:
                        continue
                    yoy = r.get("yoy_pct") or ""
                    out.append({
                        "news_datetime": news_dt,
                        "symbol": r["symbol"],
                        "period": r.get("period") or "",
                        "year": r.get("year") or "",
                        "profit_mb": float(r["profit_mb"]),
                        "profit_prior_mb": float(r["profit_prior_mb"]),
                        "yoy_pct": float(yoy) if yoy else None,
                        "summary": r.get("summary") or "",
                    })
                except (KeyError, ValueError, TypeError):
                    continue  # แถวเสีย (คอลัมน์ขาด/parse ไม่ได้) — ข้าม
    except Exception:
        return []
    return out


def load_filings_since(dt):
    """คืน {symbol: {"datetime": ..., "kinds": [...]}} จาก filings_log.csv
    ตั้งแต่ dt เป็นต้นมา — เก็บเวลาล่าสุดต่อ symbol พร้อมรวม kinds ทุกแถว

    ไฟล์ไม่มี/อ่านพัง → คืน dict ว่าง แถวเสียข้ามไปเงียบๆ ไม่ล้ม caller"""
    out = {}
    try:
        with open(_FILINGS_PATH, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    news_dt = datetime.datetime.strptime(
                        r["news_datetime"], "%Y-%m-%d %H:%M").replace(tzinfo=_BKK)
                    if news_dt < dt:
                        continue
                    sym = r["symbol"]
                    kinds = [k for k in (r.get("kinds") or "").split("/") if k]
                except (KeyError, ValueError, TypeError):
                    continue  # แถวเสีย — ข้าม
                entry = out.setdefault(sym, {"datetime": news_dt, "kinds": []})
                for k in kinds:
                    if k not in entry["kinds"]:
                        entry["kinds"].append(k)
                if news_dt > entry["datetime"]:
                    entry["datetime"] = news_dt
    except Exception:
        return {}
    return out


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
