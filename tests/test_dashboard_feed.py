# -*- coding: utf-8 -*-
"""dashboard_feed.load_stock — หา payload ของ Trading_Dashboard (ในเครื่อง → HTTPS) แล้วคืนหุ้นหนึ่งตัว
ทั้งไฟล์นี้ใช้ payload ปลอมใน tmp_path และ mock ชั้น HTTP — ไม่แตะเน็ตจริง"""
import builtins
import datetime
import json
import os
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard_feed as df  # noqa: E402

TODAY = datetime.date(2026, 8, 15)
BUILD_A = "18f8448e0b5402633ce2"
BUILD_B = "aa11bb22cc33dd44ee55"


def _payload(build_id, asof, stocks=None, schema=1, intraday=False):
    """คืน {rel_path: json_text} ของ payload หนึ่ง build (โครงเดียวกับ site/data ของจริง)"""
    if stocks is None:
        stocks = [{"t": "AOT", "n": "ท่าอากาศยานไทย", "g": "TH", "c": 65.0, "rs": 86},
                  {"t": "PTT", "n": "ปตท.", "g": "TH", "c": 31.5, "rs": 0}]
    core = {"asof": asof, "markets": {"TH": {"asof": asof, "indexName": "SET Index",
                                            "intraday": intraday,
                                            "bench": {"close": [1500.0, 1600.0]}}}}
    manifest = {"schema_version": schema, "build_id": build_id,
                "chunks": {"core": {"url": f"data/{build_id}/core.json"},
                           "stocks:TH": {"url": f"data/{build_id}/stocks-TH.json"}}}
    return {
        "data/manifest.json": json.dumps(manifest, ensure_ascii=False),
        f"data/{build_id}/core.json": json.dumps(core, ensure_ascii=False),
        f"data/{build_id}/stocks-TH.json": json.dumps({"market": "TH", "stocks": stocks},
                                                       ensure_ascii=False),
    }


def write_site(root, files):
    for rel, text in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    return str(root)


@pytest.fixture(autouse=True)
def fresh_cache():
    df._CACHE.clear()
    yield
    df._CACHE.clear()


@pytest.fixture
def http(monkeypatch):
    """แทนชั้น HTTP ด้วย dict {rel: text} — ตั้ง http.files = None ให้ล้ม, นับจำนวนครั้งที่ถูกเรียก"""
    class Fake:
        files = None
        calls = []

    def _read_http(url, rel):
        Fake.calls.append((url, rel))
        if Fake.files is None or rel not in Fake.files:
            raise OSError(f"HTTP 404 {url}/{rel}")
        return Fake.files[rel]

    monkeypatch.setattr(df, "_read_http", _read_http)
    return Fake


# ── ลำดับการหา payload ──────────────────────────────────────────

def test_fresh_local_payload_is_used_without_touching_http(tmp_path, http):
    site = write_site(tmp_path, _payload(BUILD_A, "2026-08-14"))
    stock, meta = df.load_stock("AOT", site_dir=site, max_age_days=5, today=TODAY)
    assert stock["t"] == "AOT" and stock["c"] == 65.0
    assert meta["source"] == "local" and meta["build_id"] == BUILD_A
    assert meta["asof"] == "2026-08-14" and meta["core_asof"] == "2026-08-14"
    assert meta["age_days"] == 1 and meta["stale"] is False
    assert meta["indexName"] == "SET Index" and meta["bench"]["close"] == [1500.0, 1600.0]
    assert http.calls == []


def test_missing_local_falls_back_to_https(tmp_path, http):
    http.files = _payload(BUILD_A, "2026-08-14")
    stock, meta = df.load_stock("AOT", site_dir=str(tmp_path / "nowhere"),
                                url="https://example.test", today=TODAY)
    assert stock["t"] == "AOT"
    assert meta["source"] == "https"
    assert http.calls[0] == ("https://example.test", "data/manifest.json")


def test_empty_site_dir_skips_local_and_uses_https(http):
    http.files = _payload(BUILD_A, "2026-08-14")
    stock, meta = df.load_stock("AOT", site_dir="", today=TODAY)
    assert stock["t"] == "AOT" and meta["source"] == "https"


def test_stale_local_falls_back_to_https_when_available(tmp_path, http):
    site = write_site(tmp_path, _payload(BUILD_A, "2026-08-01"))       # เก่า 14 วัน
    http.files = _payload(BUILD_B, "2026-08-14")
    stock, meta = df.load_stock("AOT", site_dir=site, max_age_days=5, today=TODAY)
    assert meta["source"] == "https" and meta["build_id"] == BUILD_B
    assert meta["age_days"] == 1 and meta["stale"] is False


def test_stale_local_is_served_with_stale_flag_when_https_is_down(tmp_path, http):
    site = write_site(tmp_path, _payload(BUILD_A, "2026-08-01"))
    stock, meta = df.load_stock("AOT", site_dir=site, max_age_days=5, today=TODAY)
    assert stock["t"] == "AOT"
    assert meta["source"] == "local" and meta["age_days"] == 14 and meta["stale"] is True


def test_https_result_is_used_even_when_still_stale(http):
    http.files = _payload(BUILD_A, "2026-07-01")
    stock, meta = df.load_stock("AOT", site_dir="", max_age_days=5, today=TODAY)
    assert stock["t"] == "AOT" and meta["stale"] is True and meta["age_days"] == 45


def test_both_sources_fail_raises_with_paths_tried(tmp_path, http):
    missing = str(tmp_path / "nowhere")
    with pytest.raises(df.DashboardUnavailable) as ei:
        df.load_stock("AOT", site_dir=missing, url="https://example.test", today=TODAY)
    msg = str(ei.value)
    assert missing in msg and "https://example.test" in msg


# ── กติกาความปลอดภัยของ manifest ────────────────────────────────

def test_unknown_schema_version_is_rejected_not_parsed(tmp_path, http):
    site = write_site(tmp_path, _payload(BUILD_A, "2026-08-14", schema=2))
    http.files = _payload(BUILD_A, "2026-08-14")            # HTTPS ดีอยู่ แต่ต้องไม่ถูกลอง
    with pytest.raises(df.DashboardUnavailable) as ei:
        df.load_stock("AOT", site_dir=site, today=TODAY)
    assert "schema_version" in str(ei.value) and "อัปเดตโค้ด" in str(ei.value)
    assert http.calls == []


@pytest.mark.parametrize("bad", ["../evil", "ABCDEF0123", "18f8448e", "x" * 70, "", "18f8448e0b54/../x"])
def test_malformed_build_id_is_rejected(tmp_path, http, bad):
    files = _payload(BUILD_A, "2026-08-14")
    files["data/manifest.json"] = json.dumps({"schema_version": 1, "build_id": bad})
    site = write_site(tmp_path, files)
    with pytest.raises(df.DashboardUnavailable) as ei:
        df.load_stock("AOT", site_dir=site, today=TODAY)
    assert "build_id" in str(ei.value)


def test_valid_short_hex_build_id_is_accepted(tmp_path, http):
    site = write_site(tmp_path, _payload("abcdef12", "2026-08-14"))
    stock, meta = df.load_stock("AOT", site_dir=site, today=TODAY)
    assert meta["build_id"] == "abcdef12"


# ── ผลลัพธ์รายหุ้น ───────────────────────────────────────────

def test_unknown_symbol_returns_none_with_meta(tmp_path, http):
    site = write_site(tmp_path, _payload(BUILD_A, "2026-08-14"))
    stock, meta = df.load_stock("ZZZZ", site_dir=site, today=TODAY)
    assert stock is None
    assert meta["asof"] == "2026-08-14" and meta["source"] == "local"


def test_symbol_is_normalized_case_and_bk_suffix(tmp_path, http):
    site = write_site(tmp_path, _payload(BUILD_A, "2026-08-14"))
    stock, _ = df.load_stock("ptt.bk", site_dir=site, today=TODAY)
    assert stock["t"] == "PTT" and stock["rs"] == 0


def test_thai_text_survives_roundtrip_and_every_open_declares_utf8(tmp_path, http, monkeypatch):
    site = write_site(tmp_path, _payload(BUILD_A, "2026-08-14"))
    real_open = builtins.open
    seen = []

    def spy_open(file, *a, **kw):
        if "dashboard_feed" not in str(sys._getframe(1).f_code.co_filename):
            return real_open(file, *a, **kw)
        seen.append(kw.get("encoding"))
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy_open)
    stock, _ = df.load_stock("AOT", site_dir=site, today=TODAY)
    assert stock["n"] == "ท่าอากาศยานไทย"
    assert seen and all(e == "utf-8" for e in seen), seen


def test_missing_market_meta_gives_empty_dict_not_crash(tmp_path, http):
    files = _payload(BUILD_A, "2026-08-14")
    core = json.loads(files[f"data/{BUILD_A}/core.json"])
    core["markets"] = {}
    files[f"data/{BUILD_A}/core.json"] = json.dumps(core)
    site = write_site(tmp_path, files)
    stock, meta = df.load_stock("AOT", site_dir=site, today=TODAY)
    assert stock["t"] == "AOT"
    assert meta.get("asof") is None and meta["core_asof"] == "2026-08-14"


# ── cache ตาม build_id ─────────────────────────────────────────

def test_same_build_reads_core_and_stocks_once(tmp_path, http, monkeypatch):
    site = write_site(tmp_path, _payload(BUILD_A, "2026-08-14"))
    reads = []
    real = df._read_local

    def counting(site_dir, rel):
        reads.append(rel)
        return real(site_dir, rel)

    monkeypatch.setattr(df, "_read_local", counting)
    df.load_stock("AOT", site_dir=site, today=TODAY)
    df.load_stock("PTT", site_dir=site, today=TODAY)
    df.load_stock("AOT", site_dir=site, today=TODAY)
    assert reads.count("data/manifest.json") == 3            # manifest เล็ก อ่านใหม่ทุกครั้ง
    assert reads.count(f"data/{BUILD_A}/core.json") == 1
    assert reads.count(f"data/{BUILD_A}/stocks-TH.json") == 1
    assert len(df._CACHE) == 1


def test_new_build_evicts_old_cache_entry(tmp_path, http):
    site = write_site(tmp_path, _payload(BUILD_A, "2026-08-13"))
    df.load_stock("AOT", site_dir=site, today=TODAY)
    assert list(df._CACHE) == [("local", BUILD_A)]
    write_site(tmp_path, _payload(BUILD_B, "2026-08-14",
                                  stocks=[{"t": "AOT", "n": "x", "g": "TH", "c": 70.0}]))
    stock, meta = df.load_stock("AOT", site_dir=site, today=TODAY)
    assert stock["c"] == 70.0 and meta["build_id"] == BUILD_B
    assert list(df._CACHE) == [("local", BUILD_B)]           # เก็บ build เดียว


# ── ชั้น HTTP จริง (mock urlopen) ─────────────────────────────

def test_read_http_sends_browser_user_agent_and_timeout(monkeypatch):
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return "{\"ok\": \"ไทย\"}".encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    text = df._read_http("https://example.test/", "data/manifest.json")
    assert text == "{\"ok\": \"ไทย\"}"
    req = captured["req"]
    assert req.full_url == "https://example.test/data/manifest.json"
    ua = req.get_header("User-agent")
    assert ua and ua.startswith("Mozilla/5.0")               # CF error 1010 บล็อก Python-urllib
    assert captured["timeout"] == df.HTTP_TIMEOUT == 10
