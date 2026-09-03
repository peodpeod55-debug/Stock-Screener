# -*- coding: utf-8 -*-
"""/status (พิมพ์ "สถานะ" / "status" ก็ได้) — สุขภาพบอทจากมือถือ ไม่ยิง API (port แนวคิดจากบอท US bot/status.py)

bot_status.py เป็นโมดูลล้วน: ไม่ import telegram_bot (กัน import วน) — ค่าที่อยู่ใน memory ของบอท
(_done / _fail_counts / _news_fail_count / วันหยุด SET) ถูกฉีดเข้ามาผ่าน build_status ส่วน wrapper
telegram_bot.build_status_report เป็นคนรวบให้ · ทุก path ฉีดได้ → เทสต์ไม่แตะไฟล์จริง
"""
import asyncio
import datetime
import logging
import os
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# telegram_bot เรียก logging.basicConfig ตอน import → เปิด bot_log.txt ของจริง
logging.basicConfig(level=logging.WARNING, handlers=[logging.NullHandler()])

import bot_status  # noqa: E402
from bot_status import (  # noqa: E402
    MAX_LEN, build_status, format_status, job_states, latest_scan, log_today, read_head_commit,
)
import telegram_bot as tb  # noqa: E402

BKK = ZoneInfo("Asia/Bangkok")
MON = datetime.date(2026, 8, 31)        # จันทร์ วันทำการ (anchor ปฏิทิน = อา 2026-08-30)
SAT = datetime.date(2026, 9, 5)
SUN = datetime.date(2026, 9, 6)
NO_HOLIDAY = lambda d: False  # noqa: E731
DAILY = ["heartbeat", "reminder", "digest", "confirm", "scan", "openpos"]


def _at(day, h, m=0):
    return datetime.datetime.combine(day, datetime.time(h, m), tzinfo=BKK)


def _states(now, state=None, done=None, fail=None, is_holiday=NO_HOLIDAY):
    return {j["name"]: j for j in job_states(now, state or {}, done or {}, fail or {}, is_holiday)}


# ── job_states: สถานะ job แต่ละ slot วันนี้ (digest_state + _done/_fail_counts ใน memory) ──


def test_schedule_lists_every_slot_in_time_order():
    js = job_states(_at(MON, 9), {}, {}, {}, NO_HOLIDAY)
    assert [j["name"] for j in js] == DAILY + ["calendar"]
    times = [j["time"] for j in js if j["name"] in DAILY]
    assert times == sorted(times)
    assert times[0] == datetime.time(8, 30) and times[-1] == datetime.time(17, 45)
    assert [j["weekly"] for j in js] == [False] * 6 + [True]


def test_all_done_when_every_key_is_today():
    state = {"alive_last_sent": "2026-08-31", "remind_last_sent": "2026-08-31",
             "last_sent": "2026-08-31", "confirm_last_sent": "2026-08-31",
             "scan_last_run": "2026-08-31", "openpos_last_sent": "2026-08-31",
             "calendar_last_sent": "2026-08-30"}
    js = _states(_at(MON, 18, 0), state)
    assert {j["state"] for j in js.values()} == {"done"}


def test_before_time_is_pending_after_time_is_due():
    js = _states(_at(MON, 9, 0))
    assert [js[n]["state"] for n in DAILY] == ["due", "due", "due", "pending", "pending", "pending"]


def test_yesterdays_key_does_not_count_as_today_and_is_shown_as_last():
    js = _states(_at(MON, 9, 0), {"last_sent": "2026-08-28"})
    assert js["digest"]["state"] == "due"
    assert js["digest"]["last"] == "2026-08-28"


def test_done_in_memory_without_key_means_ran_but_nothing_to_send():
    # confirm/openpos/reminder เขียน key เฉพาะเมื่อส่ง — core จบดีแต่ไม่มีของ = _done อย่างเดียว
    js = _states(_at(MON, 11, 0), done={("confirm", "2026-08-31"): True})
    assert js["confirm"]["state"] == "done_empty"


def test_failed_count_shows_tries_and_stops_at_max():
    js = _states(_at(MON, 11, 0), fail={("digest", "2026-08-31"): 1, ("confirm", "2026-08-31"): 3})
    assert js["digest"]["state"] == "failed" and js["digest"]["tries"] == 1
    assert js["confirm"]["state"] == "stopped" and js["confirm"]["tries"] == 3


def test_failure_from_another_day_is_ignored():
    js = _states(_at(MON, 11, 0), fail={("digest", "2026-08-28"): 2})
    assert js["digest"]["state"] == "due" and js["digest"]["tries"] == 0


def test_weekend_turns_daily_jobs_off_but_calendar_stays():
    js = _states(_at(SAT, 10, 0))
    assert {js[n]["state"] for n in DAILY} == {"off"}
    assert js["calendar"]["state"] == "due"       # anchor อา 2026-08-30 19:00 ผ่านมาแล้ว ยังไม่ส่ง


def test_set_holiday_turns_off_only_holiday_aware_jobs():
    # heartbeat/เตือนวันงบ เช็คแค่เสาร์-อาทิตย์ (core จริงไม่ดู holidays.txt) — ที่เหลือหยุดวันหยุด SET
    js = _states(_at(MON, 9, 0), is_holiday=lambda d: d == MON)
    assert js["heartbeat"]["state"] == "due" and js["reminder"]["state"] == "due"
    assert {js[n]["state"] for n in ("digest", "confirm", "scan", "openpos")} == {"off"}


def test_calendar_done_only_when_anchor_is_this_weeks_sunday():
    assert _states(_at(MON, 9), {"calendar_last_sent": "2026-08-30"})["calendar"]["state"] == "done"
    old = _states(_at(MON, 9), {"calendar_last_sent": "2026-08-23"})["calendar"]
    assert old["state"] == "due" and old["last"] == "2026-08-23"


def test_calendar_pending_on_sunday_before_1900_then_due():
    assert _states(_at(SUN, 18, 0), {"calendar_last_sent": "2026-08-30"})["calendar"]["state"] == "pending"
    assert _states(_at(SUN, 19, 30), {"calendar_last_sent": "2026-08-30"})["calendar"]["state"] == "due"


# ── log_today: นับ WARNING/ERROR เฉพาะวันนี้จาก bot_log.txt (รูปแบบ asctime level name: msg) ──

LOG = """2026-08-26 09:13:48,602 WARNING bot: network hiccup: Bad Gateway
2026-08-27 09:00:00,000 INFO bot: bot starting
2026-08-27 09:05:00,000 WARNING bot: set_my_commands failed: x
2026-08-27 09:10:00,000 ERROR bot: startup morning digest failed
Traceback (most recent call last):
  File "x.py", line 1, in <module>
RuntimeError: boom
2026-08-27 09:20:00,000 ERROR bot: broadcast failed (chat 1)
2026-08-27 09:30:00,000 INFO apscheduler.executors.default: Running job "x"
""".splitlines()


def test_counts_only_todays_warnings_and_errors():
    r = log_today(LOG, "2026-08-27")
    assert (r["warnings"], r["errors"]) == (1, 2)


def test_last_error_is_latest_message_without_timestamp_or_traceback():
    assert log_today(LOG, "2026-08-27")["last_error"] == "broadcast failed (chat 1)"


def test_quiet_day_has_zero_counts_and_no_error():
    assert log_today(LOG, "2026-08-28") == {"warnings": 0, "errors": 0, "last_error": None}


# ── latest_scan: สแกนล่าสุดจาก scan_log.csv (วันที่ + ตัวที่ติด) ────────────────────


def _scan_csv(path, rows, header="scan_date,ticker,earn_date,score"):
    body = "\n".join([header, *(",".join(r) for r in rows)]) + "\n"
    path.write_text(body, encoding="utf-8-sig")       # ไฟล์จริงมี BOM
    return str(path)


def test_latest_scan_picks_last_date_and_strips_bk(tmp_path):
    p = _scan_csv(tmp_path / "scan_log.csv", [
        ("2026-08-20", "PTT.BK", "2026-08-15", "8"),
        ("2026-08-26", "LUXF.BK", "2026-08-20", "12"),
        ("2026-08-26", "SR.BK", "2026-08-20", "6"),
    ])
    assert latest_scan(p) == {"date": "2026-08-26", "tickers": ["LUXF", "SR"]}


def test_latest_scan_none_when_missing_or_header_only(tmp_path):
    assert latest_scan(str(tmp_path / "none.csv")) is None
    assert latest_scan(_scan_csv(tmp_path / "empty.csv", [])) is None


def test_latest_scan_corrupt_file_returns_none(tmp_path):
    assert latest_scan(_scan_csv(tmp_path / "bad.csv", [("1", "2")], header="a,b")) is None


# ── read_head_commit: commit ที่ HEAD ชี้ (ไม่ใช้ git CLI) ───────────────────────────


def _git(tmp_path, head, refs=None, packed=None):
    g = tmp_path / ".git"
    g.mkdir()
    (g / "HEAD").write_text(head, encoding="utf-8")
    for ref, sha in (refs or {}).items():
        p = g / ref
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sha + "\n", encoding="utf-8")
    if packed:
        (g / "packed-refs").write_text(packed, encoding="utf-8")
    return tmp_path


def test_head_via_branch_ref_file(tmp_path):
    root = _git(tmp_path, "ref: refs/heads/main\n",
                refs={"refs/heads/main": "9a0896fdec0b2e2cf124720759cfdef59dab415b"})
    assert read_head_commit(root) == "9a0896f"


def test_head_via_packed_refs_when_loose_ref_missing(tmp_path):
    packed = ("# pack-refs with: peeled fully-peeled sorted\n"
              "1111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa refs/heads/other\n"
              "2222222bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb refs/heads/main\n")
    root = _git(tmp_path, "ref: refs/heads/main\n", packed=packed)
    assert read_head_commit(root) == "2222222"


def test_detached_head_is_the_hash_itself(tmp_path):
    assert read_head_commit(_git(tmp_path, "abcdef0123456789abcdef0123456789abcdef01\n")) == "abcdef0"


def test_no_git_dir_returns_none(tmp_path):
    assert read_head_commit(tmp_path) is None


def test_real_repo_head_matches_git_layout():
    sha = read_head_commit()
    assert sha is not None and len(sha) == 7
    int(sha, 16)


def test_module_records_start_time_and_commit_at_import():
    assert isinstance(bot_status.STARTED_AT, datetime.datetime)
    assert bot_status.STARTED_AT.tzinfo is not None
    assert bot_status.STARTED_COMMIT == read_head_commit()


# ── build_status: รวมทุกแหล่งเป็น dict เดียว — ไฟล์หาย/พังได้ค่าว่าง ─────────────────


def _build(tmp_path, monkeypatch, **over):
    monkeypatch.setattr(bot_status.stock_core, "get_watchlist", lambda chat_id: ["AOT", "PTT"])
    monkeypatch.setattr(bot_status.set_news, "news_data_age_hours", lambda: 2.3)
    kw = dict(digest_state={"last_sent": "2026-08-31"}, done={}, fail_counts={},
              news_fail_count=0, is_holiday=NO_HOLIDAY, n_chats=2,
              log_path=str(tmp_path / "bot_log.txt"), scan_log_path=str(tmp_path / "scan_log.csv"),
              started_at=_at(MON, 9, 13), started_commit="ae4ceb7", head_commit="ae4ceb7")
    kw.update(over)
    return build_status(_at(MON, 11, 0), 4242, **kw)


def test_build_status_assembles_all_sources(tmp_path, monkeypatch):
    (tmp_path / "bot_log.txt").write_text("\n".join(LOG).replace("2026-08-27", "2026-08-31"),
                                          encoding="utf-8")
    _scan_csv(tmp_path / "scan_log.csv", [("2026-08-26", "LUXF.BK", "2026-08-20", "12")])
    st = _build(tmp_path, monkeypatch, fail_counts={("confirm", "2026-08-31"): 1})
    assert st["pid"] == os.getpid()
    assert st["started_at"] == _at(MON, 9, 13) and st["head_commit"] == "ae4ceb7"
    jobs = {j["name"]: j["state"] for j in st["jobs"]}
    assert jobs["digest"] == "done" and jobs["confirm"] == "failed"
    assert st["watch_n"] == 2 and st["n_chats"] == 2
    assert st["news_age_h"] == 2.3 and st["news_fail_count"] == 0
    assert st["log"] == {"warnings": 1, "errors": 2, "last_error": "broadcast failed (chat 1)"}
    assert st["scan"] == {"date": "2026-08-26", "tickers": ["LUXF"]}


def test_build_status_survives_missing_files(tmp_path, monkeypatch):
    st = _build(tmp_path, monkeypatch)
    assert st["log"] == {"warnings": 0, "errors": 0, "last_error": None}
    assert st["scan"] is None


def test_build_status_defaults_to_process_start_info(tmp_path, monkeypatch):
    st = _build(tmp_path, monkeypatch, started_at=None, started_commit=None, head_commit=None)
    assert st["started_at"] == bot_status.STARTED_AT
    assert st["started_commit"] == bot_status.STARTED_COMMIT
    assert st["head_commit"] == read_head_commit()


# ── format_status: ข้อความ HTML ที่ผู้ใช้เห็น ───────────────────────────────────────


def _job(name, label, h, m, state, last=None, tries=0, weekly=False):
    return {"name": name, "label": label, "time": datetime.time(h, m), "state": state,
            "last": last, "tries": tries, "weekly": weekly}


def _status(**over):
    base = {
        "now": _at(MON, 11, 5), "pid": 13920,
        "started_at": _at(MON, 9, 3), "started_commit": "ae4ceb7", "head_commit": "ae4ceb7",
        "jobs": [
            _job("heartbeat", "heartbeat", 8, 30, "done"),
            _job("reminder", "เตือนวันงบ", 8, 45, "done_empty"),
            _job("digest", "สรุปงบเช้า", 8, 55, "due"),
            _job("confirm", "ยืนยันรอบเช้า", 10, 30, "failed", tries=1),
            _job("scan", "สแกนอัตโนมัติ", 17, 30, "pending", last="2026-08-26"),
            _job("openpos", "รายงานไม้เปิด", 17, 45, "stopped", tries=3),
            _job("calendar", "ปฏิทินงบ", 19, 0, "done", last="2026-08-30", weekly=True),
        ],
        "watch_n": 26, "n_chats": 1,
        "news_age_h": 2.3, "news_fail_count": 0,
        "news_last_error": None, "news_next_try": None,
        "log": {"warnings": 0, "errors": 0, "last_error": None},
        "scan": {"date": "2026-08-26", "tickers": ["LUXF", "SR"]},
        "catchup_min": 30, "max_tries": 3,
    }
    base.update(over)
    return base


def test_format_header_pid_uptime_and_matching_commit():
    msg = format_status(_status())
    assert msg.startswith("🤖 สถานะบอท 2026-08-31 11:05 (จ)")
    assert "PID 13920" in msg and "เริ่ม 09:03" in msg and "uptime 2 ชม. 2 น." in msg
    assert "โค้ด ae4ceb7 ✅ ตรงกับ HEAD" in msg


def test_format_warns_when_head_is_newer_than_running_code():
    msg = format_status(_status(head_commit="b1c2d3e"))
    assert "โค้ด ae4ceb7 ⚠️ HEAD ใหม่กว่า (b1c2d3e)" in msg and "restart" in msg


def test_format_unknown_start_and_commit():
    msg = format_status(_status(started_at=None, started_commit=None, head_commit=None))
    assert "เริ่มเมื่อ ไม่ทราบ" in msg and "ไม่ทราบ commit" in msg


def test_format_job_lines_per_state():
    msg = format_status(_status())
    assert "✅ heartbeat 08:30" in msg
    assert "✅ เตือนวันงบ 08:45 (ทำแล้ว ไม่มีอะไรส่ง)" in msg
    assert "❌ สรุปงบเช้า 08:55 — ถึงเวลาแล้วยังไม่รัน (catch-up ทุก 30 น.)" in msg
    assert "⚠️ ยืนยันรอบเช้า 10:30 — ล้ม 1/3 รอลองใหม่" in msg
    assert "⏳ สแกนอัตโนมัติ 17:30 (ยังไม่ถึงเวลา) · ล่าสุด 2026-08-26" in msg
    assert "⛔ รายงานไม้เปิด 17:45 — ล้มครบ 3/3 หยุดลองวันนี้" in msg
    assert "✅ ปฏิทินงบ สัปดาห์นี้ (อา 19:00)" in msg


def test_format_off_and_calendar_due_lines():
    msg = format_status(_status(jobs=[
        _job("scan", "สแกนอัตโนมัติ", 17, 30, "off", last="2026-08-26"),
        _job("calendar", "ปฏิทินงบ", 19, 0, "due", last="2026-08-23", weekly=True),
    ]))
    assert "— สแกนอัตโนมัติ 17:30 (ไม่มีรอบวันนี้) · ล่าสุด 2026-08-26" in msg
    assert "❌ ปฏิทินงบ สัปดาห์นี้ — ถึงเวลาแล้วยังไม่ส่ง (catch-up ทุก 30 น.) · ล่าสุด 2026-08-23" in msg


def test_format_watch_news_scan_and_log_lines():
    msg = format_status(_status())
    assert "📌 ลิสต์ติดตาม 26 ตัว · ผู้รับ 1 chat" in msg
    assert "📰 ข่าว SET: ข้อมูลอายุ 2.3 ชม." in msg
    assert "🔍 สแกนล่าสุด 2026-08-26: ติด 2 ตัว (LUXF SR)" in msg
    assert "🪵 log วันนี้: เตือน 0 · error 0" in msg
    assert "error ล่าสุด" not in msg


def test_format_news_failing_and_last_error_escaped():
    msg = format_status(_status(news_fail_count=4,
                                log={"warnings": 2, "errors": 1, "last_error": "x <b>boom</b>"}))
    assert "⚠️ ดึงล้มติดกัน 4 รอบ" in msg
    assert "เตือน 2 · error 1" in msg
    assert "↳ error ล่าสุด: x &lt;b&gt;boom&lt;/b&gt;" in msg


def test_format_news_line_shows_last_reason_and_backoff():
    # ล้มติดกันแล้วต้องบอกได้ว่า "เพราะอะไร" และ "รอบหน้าเมื่อไหร่" (incident 1 ก.ย. — log แยกอาการไม่ออก)
    msg = format_status(_status(news_fail_count=4,
                                news_last_error=(_at(MON, 10, 42), "Blocked HTTP 403"),
                                news_next_try=_at(MON, 11, 25)))
    assert ("📰 ข่าว SET: ข้อมูลอายุ 2.3 ชม. · ⚠️ ดึงล้มติดกัน 4 รอบ · "
            "ล่าสุด: Blocked HTTP 403 @10:42 · backoff ถึง 11:25") in msg


def test_format_news_line_unchanged_when_healthy():
    msg = format_status(_status(news_last_error=None, news_next_try=None))
    assert "📰 ข่าว SET: ข้อมูลอายุ 2.3 ชม. · ดึงล้มติดกัน 0 รอบ" in msg
    assert "ล่าสุด:" not in msg and "backoff" not in msg


def test_format_never_fetched_news_and_no_scan():
    msg = format_status(_status(news_age_h=None, scan=None))
    assert "ยังไม่เคยดึงข่าว" in msg and "🔍 ยังไม่เคยสแกน" in msg


def test_format_uptime_in_days_and_long_ticker_list_fits_limit():
    msg = format_status(_status(started_at=_at(datetime.date(2026, 8, 29), 9, 3),
                                scan={"date": "2026-08-26", "tickers": [f"S{i}" for i in range(40)]}))
    assert "2 วัน 2 ชม." in msg
    assert "+30" in msg and len(msg) <= MAX_LEN


# ── wiring ใน telegram_bot: เมนู + "สถานะ"/"status" + wrapper รวบค่าจาก memory ──────


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


class FakeUpdate:
    def __init__(self, text):
        self.message = FakeMessage(text)
        self.effective_chat = SimpleNamespace(id=4242)


def _dispatch(monkeypatch, text):
    monkeypatch.setattr(tb, "_register_chat", lambda chat_id: None)
    upd = FakeUpdate(text)
    asyncio.run(tb._dispatch_text(upd, SimpleNamespace(bot=None, args=[]), text))
    return upd.message.replies


def test_status_in_menu_before_help_with_slash_alias():
    names = [c for c, _ in tb.BOT_COMMANDS]
    assert "status" in names and names.index("status") < names.index("help")
    assert tb.SLASH_ALIASES["status"] == "status"


def test_thai_and_english_text_route_to_status(monkeypatch):
    monkeypatch.setattr(tb, "build_status_report", lambda chat_id: f"สถานะของ {chat_id}")
    for word in ("สถานะ", "status", "STATUS"):
        replies = _dispatch(monkeypatch, word)
        assert replies == [("สถานะของ 4242", {"reply_markup": None, "parse_mode": "HTML"})], word


def test_status_failure_reports_to_user_not_crash(monkeypatch):
    def boom(chat_id):
        raise RuntimeError("disk <gone>")
    monkeypatch.setattr(tb, "build_status_report", boom)
    replies = _dispatch(monkeypatch, "สถานะ")
    assert len(replies) == 1 and replies[0][0].startswith("⚠️") and "disk &lt;gone&gt;" in replies[0][0]


def _freeze(monkeypatch, when):
    class FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(tb, "datetime", SimpleNamespace(
        datetime=FakeDT, date=datetime.date, time=datetime.time, timedelta=datetime.timedelta))


def test_build_status_report_wires_bot_memory_and_files(monkeypatch, tmp_path):
    # ค่าใน memory ของบอท + ไฟล์ที่ conftest ชี้ไป tmp ต้องโผล่ในข้อความจริง — ไม่ยิงเน็ต (conftest บล็อกอยู่)
    _freeze(monkeypatch, _at(MON, 11, 0))
    tb._fail_counts.clear()
    tb._done.clear()
    tb._fail_counts[("confirm", "2026-08-31")] = 1
    tb._done[("reminder", "2026-08-31")] = True
    monkeypatch.setattr(tb, "_news_fail_count", 4)
    monkeypatch.setattr(tb, "_load_chat_ids", lambda: [1, 2])
    monkeypatch.setattr(tb.stock_core, "get_watchlist", lambda chat_id: ["AOT"] if chat_id == 4242 else [])
    tb._save_digest_state({"last_sent": "2026-08-31"})
    with open(tb._LOG_PATH, "w", encoding="utf-8") as f:
        f.write("2026-08-31 09:10:00,000 ERROR bot: startup scan failed\n")
    msg = tb.build_status_report(4242)
    assert "✅ สรุปงบเช้า 08:55" in msg
    assert "✅ เตือนวันงบ 08:45 (ทำแล้ว ไม่มีอะไรส่ง)" in msg
    assert "⚠️ ยืนยันรอบเช้า 10:30 — ล้ม 1/3" in msg
    assert "ลิสต์ติดตาม 1 ตัว · ผู้รับ 2 chat" in msg
    assert "ดึงล้มติดกัน 4 รอบ" in msg
    assert "error 1" in msg and "↳ error ล่าสุด: startup scan failed" in msg
    assert "🔍 ยังไม่เคยสแกน" in msg          # scanner.LOG_PATH ชี้ tmp (ว่าง)
    tb._fail_counts.clear()
    tb._done.clear()


def test_build_status_report_wires_news_error_and_backoff(monkeypatch):
    # ค่าใหม่สองตัวต้องเดินทางจาก memory ของ telegram_bot → bot_status (ที่ไม่ import กลับมา)
    _freeze(monkeypatch, _at(MON, 11, 0))
    tb._fail_counts.clear()
    tb._done.clear()
    monkeypatch.setattr(tb, "_news_fail_count", 3)
    monkeypatch.setattr(tb, "_news_last_error", (_at(MON, 10, 40), "Challenged (WAF)"))
    monkeypatch.setattr(tb, "_news_next_try", _at(MON, 11, 20))
    monkeypatch.setattr(tb, "_load_chat_ids", lambda: [1])
    monkeypatch.setattr(tb.stock_core, "get_watchlist", lambda chat_id: [])
    msg = tb.build_status_report(4242)
    assert "ดึงล้มติดกัน 3 รอบ · ล่าสุด: Challenged (WAF) @10:40 · backoff ถึง 11:20" in msg


def test_help_mentions_status():
    upd = FakeUpdate("/help")
    asyncio.run(tb.cmd_help(upd, SimpleNamespace(bot=None, args=[])))
    text = "\n".join(t for t, _ in upd.message.replies)
    assert "<code>สถานะ</code>" in text and "<code>status</code>" in text


def test_apscheduler_logger_is_quiet_and_log_path_is_a_constant():
    # log เดิม 97% เป็น apscheduler INFO ("Running job…/executed successfully") — ตัดทิ้งให้ retention ยาวขึ้น ~40 เท่า
    assert logging.getLogger("apscheduler").level == logging.WARNING
    assert os.path.basename(tb._LOG_PATH) == "bot_log.txt"
