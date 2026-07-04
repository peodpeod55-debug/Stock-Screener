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
import json
import datetime
import threading
from zoneinfo import ZoneInfo

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

def check_new_earnings_news(max_age_hours: float = 14):
    """สำหรับ job อัตโนมัติ: คืนเฉพาะข่าวงบ "ที่ยังไม่เคยเห็น"
    จัดกลุ่มต่อหุ้น [{symbol, datetime, kinds, headlines}]

    ทุกข่าวงบที่เจอ (ใหม่หรือเก่า) จะถูกบันทึกวันงบเข้าระบบเสมอ
    แต่แจ้งเตือนเฉพาะข่าวอายุไม่เกิน max_age_hours ชั่วโมง
    (กันสแปมย้อนหลังตอนเพิ่งเริ่มใช้ครั้งแรก)
    """
    news = fetch_company_news(days_back=1)
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
        if it["datetime"] > entry["datetime"]:
            entry["datetime"] = it["datetime"]
    _save_seen(seen)
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
        if it["datetime"] > entry["datetime"]:
            entry["datetime"] = it["datetime"]
    return sorted(by_symbol.values(), key=lambda x: x["datetime"], reverse=True)


if __name__ == "__main__":
    print("\nกำลังเช็คข่าว SET (~15-30 วินาที)...\n")
    rows = list_earnings_news(days_back=2)
    if not rows:
        print("ไม่พบบริษัทแจ้งผลประกอบการใน 2 วันล่าสุด")
    for r in rows:
        print(f"  {r['datetime']:%d/%m %H:%M}  {r['symbol']:<10} {'/'.join(r['kinds'])}")
    print(f"\nรวม {len(rows)} บริษัท (บันทึกวันงบเข้าระบบแล้ว)\n")
