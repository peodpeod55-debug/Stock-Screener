"""สถานะบอทสำหรับคำสั่ง "สถานะ" / /status — ดูสุขภาพจากมือถือไม่ต้องเปิด bot_log.txt (port แนวคิดจากบอท US bot/status.py)

รวมทุกอย่างที่เคยต้องไล่ดูเองเวลาสงสัยว่าบอทโอเคไหม: process (PID/เริ่มเมื่อ/uptime),
โค้ดที่รันอยู่เทียบ HEAD (commit แล้วลืม restart ไหม), job รายวันแต่ละ slot วันนี้
(digest_state.json + _done/_fail_counts ใน memory ของ telegram_bot), ลิสต์ติดตาม,
ความสดของข่าว SET, สแกนล่าสุดจาก scan_log.csv และ WARNING/ERROR วันนี้ใน bot_log.txt

ไม่ยิง API ใด ๆ — อ่านไฟล์/memory ล้วน จึงตอบได้ทันทีแม้ Yahoo/เว็บ SET ล่ม ·
โมดูลนี้ **ไม่ import telegram_bot** (กัน import วน): ค่าที่อยู่ใน memory ของบอทถูกฉีดเข้ามาทาง
build_status ส่วน telegram_bot.build_status_report เป็นคนรวบให้ · ทุก path ฉีดได้เพื่อเทสต์
"""
import csv
import datetime
import html
import os
import re
from zoneinfo import ZoneInfo

import set_news
import stock_core

TZ = ZoneInfo("Asia/Bangkok")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_LEN = 3900                  # เพดานข้อความ Telegram ของบอท (TG_LIMIT ใน telegram_bot)
STATUS_LIST_MAX = 10            # รายชื่อหุ้นต่อบรรทัด เกินโชว์ "+N"

# ตาราง slot รายวัน (ตรงกับ job ใน telegram_bot.main): name = key ใน _done/_fail_counts ·
# state_key = key ใน digest_state.json ("เขียนเฉพาะเมื่อส่งถึง") · holiday_aware = core เช็ค
# holidays.txt ด้วย (heartbeat/เตือนวันงบ เช็คแค่เสาร์-อาทิตย์)
SCHEDULE = [
    ("heartbeat", "heartbeat", datetime.time(8, 30), "alive_last_sent", False),
    ("reminder", "เตือนวันงบ", datetime.time(8, 45), "remind_last_sent", False),
    ("digest", "สรุปงบเช้า", datetime.time(8, 55), "last_sent", True),
    ("confirm", "ยืนยันรอบเช้า", datetime.time(10, 30), "confirm_last_sent", True),
    ("scan", "สแกนอัตโนมัติ", datetime.time(17, 30), "scan_last_run", True),
    ("openpos", "รายงานไม้เปิด", datetime.time(17, 45), "openpos_last_sent", True),
]
# ปฏิทินงบเป็นรายสัปดาห์: key เก็บ anchor = วันอาทิตย์ล่าสุด (ส่งอา 19:00 หรือวันแรกของสัปดาห์ที่คอมเปิด)
CALENDAR = ("calendar", "ปฏิทินงบ", datetime.time(19, 0), "calendar_last_sent")

_THAI_WD = ["จ", "อ", "พ", "พฤ", "ศ", "ส", "อา"]
_JOB_ICON = {"done": "✅", "done_empty": "✅", "failed": "⚠️", "stopped": "⛔",
             "due": "❌", "pending": "⏳", "off": "—"}
# บรรทัด log ของบอท: "%(asctime)s %(levelname)s %(name)s: %(message)s" (asctime มี ,ms)
_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \S+ (\w+) (\S+): (.*)$")


def read_head_commit(root=_BASE_DIR):
    """commit ย่อ 7 ตัวที่ HEAD ชี้ — อ่านไฟล์ .git ตรงๆ ไม่พึ่ง git CLI · None = อ่านไม่ได้"""
    git = os.path.join(root, ".git")
    try:
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head[:7] or None
        ref = head[5:].strip()
        loose = os.path.join(git, *ref.split("/"))
        if os.path.exists(loose):
            with open(loose, encoding="utf-8") as f:
                return f.read().strip()[:7] or None
        with open(os.path.join(git, "packed-refs"), encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0][:7]
    except OSError:
        pass
    return None


# บันทึกตอน import (≈ เริ่ม process) — โค้ดที่โหลดตอนนั้นคือโค้ดที่รันอยู่จริง
# แม้ HEAD จะขยับไปแล้วหลังจากนั้น (กรณีที่ถามบ่อย: commit แล้วลืม restart)
STARTED_AT = datetime.datetime.now(TZ)
STARTED_COMMIT = read_head_commit()


def _last_sunday(day):
    return day - datetime.timedelta(days=(day.weekday() + 1) % 7)


def _slot_state(now, key_today, last, done, tries, max_tries, due_at, off):
    """สถานะ slot เดียว: off > done (key วันนี้) > failed/stopped > done_empty > due/pending"""
    if off:
        return "off"
    if last == key_today:
        return "done"
    if tries >= max_tries:
        return "stopped"
    if tries > 0:
        return "failed"
    if done:
        return "done_empty"
    return "due" if now >= due_at else "pending"


def job_states(now, state, done, fail_counts, is_holiday, max_tries=3):
    """สถานะ job ทุก slot ณ เวลา now — state = digest_state.json, done/fail_counts = dict ใน memory
    ของ telegram_bot (key (job, วัน ISO)), is_holiday(date) = วันหยุด SET

    done = ส่งแล้ว (key วันนี้) · done_empty = core จบดีแต่ไม่มีของให้ส่ง (มีแต่ _done) ·
    failed = ล้ม n ครั้ง catch-up จะลองใหม่ · stopped = ล้มครบ max_tries หยุดลองวันนี้ ·
    due = ถึงเวลาแล้วยังไม่รัน · pending = ยังไม่ถึงเวลา · off = ไม่มีรอบวันนี้ (เสาร์-อาทิตย์/วันหยุด)
    """
    today = now.date()
    today_iso = today.isoformat()
    weekend = today.weekday() >= 5
    holiday = is_holiday(today)
    out = []
    for name, label, t, state_key, holiday_aware in SCHEDULE:
        tries = fail_counts.get((name, today_iso), 0)
        st = _slot_state(
            now, today_iso, state.get(state_key), done.get((name, today_iso)), tries, max_tries,
            datetime.datetime.combine(today, t, tzinfo=now.tzinfo),
            off=weekend or (holiday_aware and holiday))
        out.append({"name": name, "label": label, "time": t, "state": st,
                    "last": state.get(state_key), "tries": tries, "weekly": False})
    name, label, t, state_key = CALENDAR
    anchor = _last_sunday(today)
    tries = fail_counts.get((name, today_iso), 0)
    st = _slot_state(
        now, anchor.isoformat(), state.get(state_key), done.get((name, today_iso)), tries, max_tries,
        datetime.datetime.combine(anchor, t, tzinfo=now.tzinfo), off=False)
    out.append({"name": name, "label": label, "time": t, "state": st,
                "last": state.get(state_key), "tries": tries, "weekly": True})
    return out


def log_today(lines, today_iso):
    """นับ WARNING/ERROR เฉพาะบรรทัดของวันนี้ + ข้อความ error ล่าสุด (บรรทัด traceback ไม่เข้าเกณฑ์ จึงถูกข้าม)"""
    warnings, errors, last_error = 0, 0, None
    for line in lines:
        m = _LOG_LINE.match(line.rstrip())
        if not m or m.group(1) != today_iso:
            continue
        level, msg = m.group(2), m.group(4)
        if level == "WARNING":
            warnings += 1
        elif level == "ERROR":
            errors += 1
            last_error = msg
    return {"warnings": warnings, "errors": errors, "last_error": last_error}


def _read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def latest_scan(path, tail_bytes=64 * 1024):
    """สแกนล่าสุดจาก scan_log.csv: วันที่ของแถวสุดท้าย + ticker ทุกตัวของวันนั้น (ตัด .BK) · None = ไม่มี/พัง

    อ่านเฉพาะท้ายไฟล์ (tail_bytes) — ไฟล์สะสมโตทุกวัน แต่คำตอบอยู่แค่วันล่าสุด
    บรรทัดแรกหลังจุด seek อาจโดนตัดกลาง → ทิ้ง (ท้าย 64KB ครอบหลายร้อยแถว
    เกินวันเดียวแน่)"""
    try:
        with open(path, "rb") as f:
            head = f.readline()
            f.seek(0, os.SEEK_END)
            start = max(len(head), f.tell() - tail_bytes)
            f.seek(start)
            tail = f.read()
        cols = next(csv.reader([head.decode("utf-8-sig", "replace").strip()]))
        i_date, i_tick = cols.index("scan_date"), cols.index("ticker")
        lines = tail.decode("utf-8", "replace").splitlines()
        if start > len(head) and lines:
            lines = lines[1:]
        rows = [r for r in csv.reader(lines) if len(r) > max(i_date, i_tick)]
    except (OSError, ValueError, csv.Error, StopIteration):
        return None
    if not rows or not rows[-1][i_date]:
        return None
    date = rows[-1][i_date]
    tickers = [r[i_tick].removesuffix(".BK")
               for r in rows if r[i_date] == date and r[i_tick]]
    return {"date": date, "tickers": tickers}


def build_status(now, chat_id, *, digest_state, done, fail_counts, news_fail_count, is_holiday,
                 n_chats, log_path, scan_log_path, max_tries=3, catchup_min=30,
                 started_at=None, started_commit=None, head_commit=None, root=_BASE_DIR):
    """รวมสถานะทุกแหล่งเป็น dict เดียวให้ format_status — ไฟล์ไหนหาย/พังก็ได้ค่าว่าง ไม่โยน"""
    return {
        "now": now,
        "pid": os.getpid(),
        "started_at": started_at if started_at is not None else STARTED_AT,
        "started_commit": started_commit if started_commit is not None else STARTED_COMMIT,
        "head_commit": head_commit if head_commit is not None else read_head_commit(root),
        "jobs": job_states(now, digest_state, done, fail_counts, is_holiday, max_tries),
        "watch_n": len(stock_core.get_watchlist(chat_id)),
        "n_chats": n_chats,
        "news_age_h": set_news.news_data_age_hours(),
        "news_fail_count": news_fail_count,
        "log": log_today(_read_lines(log_path), now.date().isoformat()),
        "scan": latest_scan(scan_log_path),
        "catchup_min": catchup_min,
        "max_tries": max_tries,
    }


def _fmt_duration(delta):
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "<1 น."
    days, rem = divmod(mins, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} วัน {hours} ชม."
    if hours:
        return f"{hours} ชม. {mins} น."
    return f"{mins} น."


def _sym_list(symbols):
    shown = " ".join(html.escape(s) for s in symbols[:STATUS_LIST_MAX])
    extra = len(symbols) - STATUS_LIST_MAX
    return f"{shown} +{extra}" if extra > 0 else shown


def _job_line(j, catchup_min, max_tries):
    icon = _JOB_ICON.get(j["state"], "?")
    if j["weekly"]:
        label = f"{j['label']} สัปดาห์นี้"
        when = f" ({_THAI_WD[6]} {j['time']:%H:%M})"
        not_run = "ถึงเวลาแล้วยังไม่ส่ง"
    else:
        label = f"{j['label']} {j['time']:%H:%M}"
        when = ""
        not_run = "ถึงเวลาแล้วยังไม่รัน"
    last = f" · ล่าสุด {j['last']}" if j.get("last") else ""
    st = j["state"]
    if st == "done":
        return f"{icon} {label}{when}"
    if st == "done_empty":
        return f"{icon} {label} (ทำแล้ว ไม่มีอะไรส่ง)"
    if st == "failed":
        return f"{icon} {label} — ล้ม {j['tries']}/{max_tries} รอลองใหม่ (catch-up ทุก {catchup_min} น.){last}"
    if st == "stopped":
        return f"{icon} {label} — ล้มครบ {j['tries']}/{max_tries} หยุดลองวันนี้ (ดู bot_log.txt){last}"
    if st == "due":
        return f"{icon} {label} — {not_run} (catch-up ทุก {catchup_min} น.){last}"
    if st == "pending":
        return f"{icon} {label} (ยังไม่ถึงเวลา){last}"
    return f"{icon} {label} (ไม่มีรอบวันนี้){last}"


def format_status(st):
    """ข้อความ HTML จาก build_status — ตอบว่ารันอยู่ไหม/ต้อง restart ไหม/job วันนี้ครบไหม/ปัญหาวันนี้"""
    now = st["now"]
    lines = [f"🤖 สถานะบอท {now:%Y-%m-%d %H:%M} ({_THAI_WD[now.weekday()]})"]
    started = st.get("started_at")
    if started:
        since = f"{started:%H:%M}" if started.date() == now.date() else f"{started:%Y-%m-%d %H:%M}"
        lines.append(f"รันอยู่ PID {st['pid']} · เริ่ม {since} · uptime {_fmt_duration(now - started)}")
    else:
        lines.append(f"รันอยู่ PID {st['pid']} · เริ่มเมื่อ ไม่ทราบ")
    run_c, head_c = st.get("started_commit"), st.get("head_commit")
    if not run_c:
        lines.append("โค้ดที่รัน: ไม่ทราบ commit")
    elif head_c and head_c != run_c:
        # กรณีที่เจอบ่อย: commit แล้วลืม restart — โค้ดใหม่ยังไม่ live
        lines.append(f"โค้ด {run_c} ⚠️ HEAD ใหม่กว่า ({head_c}) — restart บอทเพื่อโหลดโค้ดใหม่")
    elif head_c:
        lines.append(f"โค้ด {run_c} ✅ ตรงกับ HEAD")
    else:
        lines.append(f"โค้ด {run_c}")

    lines += ["", "📅 Job วันนี้"]
    for j in st["jobs"]:
        lines.append(_job_line(j, st.get("catchup_min", 30), st.get("max_tries", 3)))

    lines.append("")
    lines.append(f"📌 ลิสต์ติดตาม {st['watch_n']} ตัว · ผู้รับ {st['n_chats']} chat")
    age = st.get("news_age_h")
    news = "ยังไม่เคยดึงข่าว" if age is None else f"ข้อมูลอายุ {age:.1f} ชม."
    fails = st.get("news_fail_count") or 0
    news += f" · ⚠️ ดึงล้มติดกัน {fails} รอบ" if fails else " · ดึงล้มติดกัน 0 รอบ"
    lines.append(f"📰 ข่าว SET: {news}")
    scan = st.get("scan")
    if scan:
        syms = scan["tickers"]
        lines.append(f"🔍 สแกนล่าสุด {scan['date']}: ติด {len(syms)} ตัว"
                     + (f" ({_sym_list(syms)})" if syms else ""))
    else:
        lines.append("🔍 ยังไม่เคยสแกน (scan_log.csv ว่าง)")
    log = st["log"]
    lines.append(f"🪵 log วันนี้: เตือน {log['warnings']} · error {log['errors']}")
    if log.get("last_error"):
        lines.append(f"↳ error ล่าสุด: {html.escape(log['last_error'][:150])}")
    return "\n".join(lines)[:MAX_LEN]
