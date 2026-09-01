# -*- coding: utf-8 -*-
"""set_news ต้องไม่ mark seen เมื่อเขียนไฟล์สะสมล้ม (improvement review 31 ส.ค. §4 ข้อ 2)

เดิม `_log_filing`/`_log_f45_result` กลืน exception เงียบ (`except: pass` — เช่น Excel
เปิดไฟล์ค้าง) แต่ข่าวถูก mark "เห็นแล้ว" ไปก่อนหน้า → แถวหายถาวรจาก filings_log/
earnings_results = หายจากสรุปงบเช้า + สแกนโหมดข่าว โดยไม่มีร่องรอย
ใหม่: ตัวเขียนคืนสำเร็จ/ล้ม + log.warning · เขียนล้ม → ไม่ mark seen + ข้ามแจ้งรอบนี้
(รอบถัดไป ~10 นาที retry ทั้งข่าว — แจ้งช้าแทนหาย) · คิว F45 คงตัวที่เขียนผลล้มไว้ลองใหม่
"""
import datetime
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import set_news  # noqa: E402

_BKK = ZoneInfo("Asia/Bangkok")


def _now():
    return datetime.datetime.now(_BKK)


# ── ตัวเขียนต้องรายงานผล ────────────────────────────────────────

def test_log_filing_returns_true_on_success():
    assert set_news._log_filing(_now(), "AOT", ["F45"]) is True
    with open(set_news._FILINGS_PATH, encoding="utf-8-sig") as f:
        assert "AOT" in f.read()


def test_log_filing_returns_false_when_file_locked(monkeypatch, tmp_path):
    # ชี้ path ไปที่ "โฟลเดอร์" — เปิด append ล้มเหมือนไฟล์ถูกล็อค
    monkeypatch.setattr(set_news, "_FILINGS_PATH", str(tmp_path))
    assert set_news._log_filing(_now(), "AOT", ["F45"]) is False


def test_log_f45_result_returns_false_when_file_locked(monkeypatch, tmp_path):
    monkeypatch.setattr(set_news, "_RESULTS_PATH", str(tmp_path))
    entry = {"symbol": "AOT", "datetime": _now(), "f45": "สรุป",
             "f45_data": {"period": "Q2", "year": 2026,
                          "profit_cur": 1_000_000.0, "profit_prior": 500_000.0}}
    assert set_news._log_f45_result(entry) is False


# ── ข่าวต้องไม่ถูก mark seen เมื่อ filings_log เขียนล้ม ─────────

def _news_item():
    return {"id": "n1", "symbol": "AOT", "url": "u1",
            "headline": "สรุปผลการดำเนินงาน ไตรมาส 2/2569",
            "datetime": _now()}


def test_filing_write_failure_keeps_news_unseen(monkeypatch):
    monkeypatch.setattr(set_news, "fetch_company_news", lambda days_back=1: [_news_item()])
    monkeypatch.setattr(set_news.stock_core, "set_manual_earnings_date", lambda *a, **k: None)
    monkeypatch.setattr(set_news, "_log_filing", lambda *a, **k: False)
    monkeypatch.setattr(set_news, "_attach_f45_summaries", lambda by: None)
    out = set_news.check_new_earnings_news()
    assert out == [], "เขียน filings_log ไม่ได้ → ข้ามแจ้งรอบนี้ (retry รอบหน้าแบบครบชุด)"
    assert set_news._load_seen() == {}, "ห้าม mark seen — ไม่งั้นแถวหายถาวรจาก digest/สแกน"


def test_filing_write_success_marks_seen_and_notifies(monkeypatch):
    monkeypatch.setattr(set_news, "fetch_company_news", lambda days_back=1: [_news_item()])
    monkeypatch.setattr(set_news.stock_core, "set_manual_earnings_date", lambda *a, **k: None)
    monkeypatch.setattr(set_news, "_attach_f45_summaries", lambda by: None)
    out = set_news.check_new_earnings_news()
    assert [e["symbol"] for e in out] == ["AOT"]
    assert "n1" in set_news._load_seen()


# ── คิว F45: เขียนผลล้ม = คงในคิวไว้ลองรอบหน้า ─────────────────

def test_f45_result_write_failure_keeps_symbol_in_backlog(monkeypatch):
    by = {"AOT": {"symbol": "AOT", "datetime": _now(), "f45_url": "u1"}}
    monkeypatch.setattr(set_news.stock_core, "get_all_watched_symbols", lambda: [])
    monkeypatch.setattr(set_news.scanner, "load_universe", lambda: [])
    monkeypatch.setattr(set_news, "fetch_news_details", lambda urls: {"u1": "เนื้อหา F45"})
    monkeypatch.setattr(set_news, "parse_f45",
                        lambda t: {"period": "Q2", "year": 2026,
                                   "profit_cur": 1_000_000.0, "profit_prior": 500_000.0})
    monkeypatch.setattr(set_news, "format_f45_summary", lambda p: "สรุปตัวเลข")
    monkeypatch.setattr(set_news, "_log_f45_result", lambda e: False)
    set_news._attach_f45_summaries(by)
    backlog = set_news._load_f45_backlog()
    assert "AOT" in backlog, "เขียน earnings_results ล้ม → ต้องคงในคิว (เดิม pop ทิ้ง = ตัวเลขหายถาวร)"
