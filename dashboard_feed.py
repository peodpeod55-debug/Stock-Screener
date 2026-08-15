# -*- coding: utf-8 -*-
"""dashboard_feed.py — อ่านหุ้นหนึ่งตัวจาก payload ของ Trading_Dashboard (โปรเจกต์ข้างเคียง)

หน้าที่เดียว: หา payload → คืน (stock, market_meta) — ไม่รู้จัก Telegram ไม่รู้จักการจัดข้อความ
stdlib ล้วน (json / urllib) ไม่อ่าน env เอง — ผู้เรียกส่ง site_dir / url / max_age_days มาให้
(telegram_bot อ่านจาก .env ผ่าน scanner._env_value)

โครง payload (ตรงกับ Trading_Dashboard/site):
    data/manifest.json            {"schema_version":1, "build_id":"<hex>", ...}   ← อ่านใหม่ทุกครั้ง (≈1KB)
    data/<build_id>/core.json     {"asof":..., "markets":{"TH":{asof,indexName,intraday,bench,...}}}
    data/<build_id>/stocks-TH.json {"market":"TH","stocks":[{t,n,g,c,sma20,...}]}

ลำดับการหา:
    1. site_dir (โฟลเดอร์ site ของ Trading_Dashboard ในเครื่อง) — ถ้าตั้งไว้
    2. ถ้าข้อ 1 อ่านไม่ได้ หรือข้อมูลเก่ากว่า max_age_days วัน → HTTPS <url>/data/... (dashboard ที่ deploy)
       ผลจาก HTTPS ใช้แม้ยังเก่า · ถ้า HTTPS ล้มแต่ในเครื่องมี (แม้เก่า) → ใช้ของในเครื่อง พร้อม stale=True
    3. ไม่ได้อะไรเลย → DashboardUnavailable พร้อม path/URL ที่ลองไปแล้ว
ความสด (stale) ตัดสินที่ชั้นข้อความ ไม่ใช่ชั้นนี้ — ชั้นนี้แค่บอก age_days / stale ให้

cache: core + stocks เก็บใน dict ระดับโมดูลตาม (source, build_id) — build ใหม่ build_id เปลี่ยน
cache หลุดเอง ไม่ต้องมี TTL · เก็บ build เดียว (แทนที่ของเก่าทิ้ง) กันบอทกินแรมสะสม
"""
import datetime
import json
import os
import re
import urllib.request
from zoneinfo import ZoneInfo

DEFAULT_URL = "https://sakura.peodbot.com"
# Cloudflare (error 1010) บล็อก User-Agent เริ่มต้นของ urllib — ต้องส่งแบบเบราว์เซอร์
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HTTP_TIMEOUT = 10                     # วินาที · ไม่ retry (ผู้ใช้กดใหม่ได้)
SCHEMA_VERSION = 1
_BUILD_ID_RE = re.compile(r"^[a-f0-9]{8,64}$")   # กัน path traversal จาก manifest ที่แปลกปลอม
_BKK = ZoneInfo("Asia/Bangkok")

_CACHE = {}                           # {(source, build_id): {"core": dict, "stocks": {market: {t: stock}}}}


class DashboardUnavailable(Exception):
    """อ่าน payload ของ dashboard ไม่ได้เลย (หรือ manifest ผิดรูป) — ข้อความบอกว่าลองที่ไหนไปแล้ว"""


# ── ชั้นอ่านดิบ (แยกไว้ให้เทสต์ monkeypatch/นับครั้งได้) ──────────

def _read_local(site_dir: str, rel: str) -> str:
    path = os.path.join(site_dir, *rel.split("/"))
    with open(path, encoding="utf-8") as fh:          # default ของ Windows คือ cp1252 → ต้องระบุเสมอ
        return fh.read()


def _read_http(url: str, rel: str) -> str:
    req = urllib.request.Request(url.rstrip("/") + "/" + rel,
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8")


# ── ตัวช่วย ─────────────────────────────────────────────────

def _parse_manifest(text: str) -> str:
    """คืน build_id ที่ตรวจแล้ว — schema/build_id ผิด → DashboardUnavailable (ไม่พยายามอ่านต่อ)"""
    man = json.loads(text)
    ver = man.get("schema_version")
    if ver != SCHEMA_VERSION:
        raise DashboardUnavailable(
            f"manifest ของ dashboard เป็น schema_version {ver!r} (รองรับ {SCHEMA_VERSION}) — "
            "ต้องอัปเดตโค้ด dashboard_feed.py ให้ตรงก่อน")
    bid = man.get("build_id")
    if not isinstance(bid, str) or not _BUILD_ID_RE.match(bid):
        raise DashboardUnavailable(f"build_id ใน manifest ผิดรูป ({bid!r}) — ไม่อ่านต่อ")
    return bid


def _age_days(asof, today):
    """อายุข้อมูล (วัน) จากสตริง asof 'YYYY-MM-DD…' — parse ไม่ได้ → None"""
    if not asof:
        return None
    try:
        d = datetime.date.fromisoformat(str(asof)[:10])
    except ValueError:
        return None
    return (today - d).days


def _load_core(reader, source: str):
    """อ่าน manifest ทุกครั้ง แล้วดึง core ของ build นั้นจาก cache หรือจากแหล่ง → (build_id, core)"""
    build_id = _parse_manifest(reader("data/manifest.json"))
    key = (source, build_id)
    entry = _CACHE.get(key)
    if entry is None:
        core = json.loads(reader(f"data/{build_id}/core.json"))
        entry = {"core": core, "stocks": {}}
        _CACHE.clear()                                    # เก็บ build เดียว
        _CACHE[key] = entry
    return build_id, entry["core"]


def _load_stocks(reader, source: str, build_id: str, market: str):
    """stocks-<market>.json ของ build ที่ _load_core เพิ่งคืนมา → {t: stock} (cache ต่อ market)"""
    entry = _CACHE[(source, build_id)]
    if market not in entry["stocks"]:
        raw = json.loads(reader(f"data/{build_id}/stocks-{market}.json"))
        entry["stocks"][market] = {str(s.get("t", "")).upper(): s for s in raw.get("stocks", [])}
    return entry["stocks"][market]


def _normalize(symbol: str) -> str:
    return symbol.upper().strip().replace(".BK", "")


# ── API ──────────────────────────────────────────────────────

def load_stock(symbol: str, market: str = "TH", *, site_dir: str = "", url: str = DEFAULT_URL,
               max_age_days: int = 5, today: datetime.date | None = None):
    """คืน (stock, market_meta)

    stock       dict ของหุ้นจาก stocks-<market>.json หรือ None ถ้า payload อ่านได้แต่ไม่มีหุ้นตัวนั้น
    market_meta core["markets"][market] (asof, indexName, intraday, bench, …) + core_asof, source
                ('local' | 'https'), build_id, age_days (None ถ้าไม่รู้), stale (age_days > max_age_days)
    โยน DashboardUnavailable ถ้าอ่าน payload ไม่ได้จากทั้งสองทาง"""
    if today is None:
        today = datetime.datetime.now(_BKK).date()
    sym = _normalize(symbol)
    tried = []
    read_local = lambda rel: _read_local(site_dir, rel)   # noqa: E731
    read_http = lambda rel: _read_http(url, rel)          # noqa: E731
    _IO_ERRORS = (OSError, ValueError, KeyError, TypeError)   # DashboardUnavailable ไม่อยู่ในนี้ → ทะลุออกไปเลย

    local = None                                       # (build_id, core) ของในเครื่อง — เก็บไว้เผื่อ HTTPS ล้ม
    if site_dir:
        try:
            local = _load_core(read_local, "local")
        except _IO_ERRORS as e:
            tried.append(f"{site_dir}: {type(e).__name__}: {e}")
    else:
        tried.append("(ไม่ได้ตั้ง DASHBOARD_SITE_DIR)")

    chosen = None                                      # (source, reader, build_id, core)
    if local is not None:
        age = _age_days(_asof_of(local[1], market), today)
        if age is None or age <= max_age_days:
            chosen = ("local", read_local) + local
    if chosen is None:                                 # ในเครื่องไม่มี/เก่า → HTTPS
        try:
            chosen = ("https", read_http) + _load_core(read_http, "https")
        except _IO_ERRORS as e:
            tried.append(f"{url}: {type(e).__name__}: {e}")
            if local is not None:                      # HTTPS ล้ม → ใช้ของเก่าในเครื่อง (meta.stale จะเป็น True)
                chosen = ("local", read_local) + local
    if chosen is None:
        raise DashboardUnavailable("อ่าน payload ของ dashboard ไม่ได้ — ลองแล้ว: " + " · ".join(tried))

    source, reader, build_id, core = chosen
    try:
        stocks = _load_stocks(reader, source, build_id, market)
    except _IO_ERRORS as e:
        raise DashboardUnavailable(
            f"อ่าน stocks-{market}.json ของ build {build_id} จาก {source} ไม่ได้: {type(e).__name__}: {e}")
    meta = dict((core.get("markets") or {}).get(market) or {})
    meta["core_asof"] = core.get("asof")
    meta["source"] = source
    meta["build_id"] = build_id
    meta["age_days"] = _age_days(meta.get("asof") or meta["core_asof"], today)
    meta["stale"] = meta["age_days"] is not None and meta["age_days"] > max_age_days
    return stocks.get(sym), meta


def _asof_of(core, market):
    mk = (core.get("markets") or {}).get(market) or {}
    return mk.get("asof") or core.get("asof")
