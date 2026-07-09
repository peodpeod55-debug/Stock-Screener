import os
import html
import json
import time
import asyncio
import datetime
import logging
from logging.handlers import RotatingFileHandler

from zoneinfo import ZoneInfo

from yfinance.exceptions import YFRateLimitError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import stats
import scanner
import set_news
import stock_core
from stock_core import (
    format_pct,
    format_signed_pct,
    format_volume,
    volume_flag,
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── log การทำงานลง bot_log.txt (หมุนไฟล์เองเมื่อเกิน 1 MB) ─────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler(
            os.path.join(_BASE_DIR, "bot_log.txt"),
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")


def _load_bot_token() -> str:
    """อ่าน BOT_TOKEN จาก environment variable หรือไฟล์ .env ข้างๆ สคริปต์"""
    token = os.environ.get("BOT_TOKEN", "").strip()
    if token:
        return token
    env_path = os.path.join(_BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    return ""


BOT_TOKEN = _load_bot_token()

# ── ข้อความยาว: Telegram จำกัด 4096 ตัวอักษร/ข้อความ ────────────
# (ลิสต์ติดตามโตๆ หรือผลสแกนเยอะๆ จะเกิน) → ตัดแบ่งตามบรรทัด

TG_LIMIT = 3900


def _split_message(text: str):
    if len(text) <= TG_LIMIT:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > TG_LIMIT:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


async def _reply_long(message, text: str, reply_markup=None, **kwargs):
    """ตอบข้อความโดยตัดแบ่งอัตโนมัติ (ปุ่มแนบไปกับท่อนสุดท้าย)"""
    parts = _split_message(text)
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        await message.reply_text(part, reply_markup=markup, **kwargs)


async def _send_long(bot, chat_id: int, text: str, reply_markup=None, **kwargs):
    parts = _split_message(text)
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        await bot.send_message(chat_id, part, reply_markup=markup, **kwargs)


# ── วันหยุดตลาด (ไม่บังคับ): ใส่วันที่ใน holidays.txt บรรทัดละวัน ──

_HOLIDAYS_PATH = os.path.join(_BASE_DIR, "holidays.txt")


def _is_market_holiday(day: datetime.date) -> bool:
    try:
        with open(_HOLIDAYS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if _parse_thai_date(line) == day:
                    return True
    except FileNotFoundError:
        pass
    return False


def build_message(ticker_input: str) -> str:
    """ดึงข้อมูลแล้วประกอบเป็นข้อความ Telegram (HTML)"""
    try:
        d = stock_core.get_stock_data(ticker_input, log=True)
    except YFRateLimitError:
        return "⏳ Yahoo จำกัดการเรียกข้อมูลชั่วคราว (rate limit)\nลองใหม่อีกครั้งใน 1-2 นาทีนะครับ"
    except Exception as e:
        return f"⚠️ เกิดข้อผิดพลาด: {html.escape(type(e).__name__)}\nลองใหม่อีกครั้งนะครับ"

    if d is None:
        return (
            f"❌ ไม่พบข้อมูล <b>{html.escape(ticker_input)}</b>\n"
            "ลองพิมพ์ ticker ให้ตรง เช่น <code>AOT</code> / <code>PTT</code> / <code>CPALL</code>"
        )

    name = html.escape(str(d["name"]))
    price = d["price"]
    cur = d["currency"]

    lines = [
        f"📊 <b>{name}</b>  (<code>{d['ticker']}</code>)",
        f"🗓 ข้อมูล ณ {d['last_date']} (ดึง {d['fetched_time']} น. ราคาดีเลย์ ~15 นาที)",
        "",
        f"💰 ราคาล่าสุด : <b>{price:.2f} {cur}</b>   {format_signed_pct(d['day_change_pct'])} วันนี้",
        "",
        "⚡ <b>ปฏิกิริยาราคาวันนี้</b>",
        f"• Gap เปิดวันนี้   : {format_signed_pct(d['gap_pct'])}",
        f"• จากเปิดถึงล่าสุด : {format_signed_pct(d['intraday_pct'])}",
        f"• เปลี่ยน 5 วัน    : {format_signed_pct(d['chg_5d_pct'])}",
        f"• เปลี่ยน 1 เดือน  : {format_signed_pct(d['chg_1m_pct'])}",
    ]

    if d["vol_ratio"] is not None:
        lines.append(
            f"• วอลุ่มวันนี้     : {format_volume(d['volume'])} "
            f"(<b>{d['vol_ratio']:.1f}x</b> ของเฉลี่ย 20 วัน){volume_flag(d['vol_ratio'])}"
        )
    else:
        lines.append(f"• วอลุ่มวันนี้     : {format_volume(d['volume'])}")

    if d["last_earnings"] or d["next_earnings"]:
        lines += ["", "🗓 <b>วันประกาศงบ</b>"]
        if d["last_earnings"]:
            line = (
                f"• งบล่าสุด   : {d['last_earnings']:%d/%m/%Y} "
                f"({d['days_since_earnings']} วันก่อน)"
            )
            if d["since_earnings_pct"] is not None:
                line += f"  → ตั้งแต่งบ <b>{format_signed_pct(d['since_earnings_pct'])}</b>"
            lines.append(line)
            r = d["earn_reaction"]
            if r is not None and r["change_pct"] is not None:
                line = f"• วันตอบรับงบ : <b>{format_signed_pct(r['change_pct'])}</b>"
                if r["vol_ratio"] is not None:
                    line += f" (วอลุ่ม {r['vol_ratio']:.1f}x{volume_flag(r['vol_ratio'])})"
                lines.append(line)
        if d["next_earnings"]:
            lines.append(
                f"• งบรอบถัดไป : {d['next_earnings']:%d/%m/%Y} "
                f"(อีก {d['days_to_earnings']} วัน)"
            )

    s = d["post_signals"]
    if s is not None and (d["days_since_earnings"] or 999) <= 60:
        score, stars = stock_core.signal_score(d)
        header = f"📌 <b>สัญญาณหลังงบ</b>"
        if score is not None:
            header += f"  {stars} ({score}/{stock_core.SCORE_MAX})"
        lines += ["", header]
        hi5_status = "✅ ผ่านแล้ว" if s["broke_pre5d_high"] else "ยังไม่ผ่าน"
        lines.append(f"• ไฮ 5 วันก่อนงบ : <code>{s['pre5d_high']:.2f}</code>  {hi5_status}")
        hi_status = "✅ ทะลุแล้ว" if s["broke_pre3m_high"] else "ยังไม่ผ่าน"
        lines.append(f"• ไฮ 3 ด. ก่อนงบ : <code>{s['pre3m_high']:.2f}</code>  {hi_status}")
        dsh = s["days_since_new_high"]
        dsh_txt = "วันนี้ 🔥" if dsh == 0 else f"{dsh} วันทำการก่อน"
        lines.append(f"• ไฮใหม่ล่าสุด   : {dsh_txt}")
        if s.get("new_high_ratio") is not None:
            lines.append(
                f"• ความถี่ไฮใหม่  : {s['new_high_days']}/{s['post_days']} "
                f"วันหลังงบ ({s['new_high_ratio'] * 100:.0f}%)"
            )
        if (s.get("weeks_observed") or 0) >= 2:
            streak = s.get("weekly_hh_streak") or 0
            streak_txt = (f"ยกขึ้น {streak} สัปดาห์ติด{' 🔥' if streak >= 2 else ''}"
                          if streak else "สัปดาห์นี้ยังไม่ยกขึ้น")
            lines.append(f"• ไฮรายสัปดาห์  : {streak_txt}")
        low_status = "✅ ยังเหนือ" if s["above_pre_low"] else "⛔ หลุดแล้ว"
        lines.append(
            f"• Low ก่อนงบ     : <code>{s['pre_earn_low']:.2f}</code>  "
            f"{low_status} ({format_signed_pct(s['pct_above_pre_low'])})"
        )

    lines += [
        "",
        f"📅 <b>High 5 วัน</b>  :  <code>{d['week_high']:.2f}</code>   {format_pct(price, d['week_high'])}",
        f"📅 <b>Low  5 วัน</b>  :  <code>{d['week_low']:.2f}</code>   {format_pct(price, d['week_low'])}",
        "",
        f"📆 <b>High 3 เดือน</b> :  <code>{d['hi3m']:.2f}</code>   {format_pct(price, d['hi3m'])}",
        f"📆 <b>Low  3 เดือน</b> :  <code>{d['lo3m']:.2f}</code>   {format_pct(price, d['lo3m'])}",
    ]

    if d["hi52"] and d["lo52"]:
        lines += [
            "",
            f"📈 <b>52w High</b> :  <code>{d['hi52']:.2f}</code>   {format_pct(price, d['hi52'])}",
            f"📉 <b>52w Low</b>  :  <code>{d['lo52']:.2f}</code>   {format_pct(price, d['lo52'])}",
        ]

    return "\n".join(lines)


# handlers

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_chat(update.effective_chat.id)
    await update.message.reply_text(
        "👋 <b>Stock Lookup Bot</b> — หุ้นไทย SET\n\n"
        "พิมพ์ชื่อหุ้นเลยได้เลย เช่น\n"
        "<code>AOT</code>   <code>PTT</code>   <code>CPALL</code>   <code>SPVI.BK</code>\n\n"
        "หรือหลายตัวพร้อมกัน: <code>AOT PTT CPALL</code>",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>วิธีใช้</b>\n\n"
        "• พิมพ์ ticker หุ้นไทยได้เลย (ไม่ต้องใส่ .BK)\n"
        "• หลายตัวพร้อมกัน (สูงสุด 10): <code>AOT PTT</code>\n"
        "• ตัวเลขที่แสดง:\n"
        "  – % เปลี่ยนแปลงวันนี้ / Gap เปิด\n"
        "  – % เปลี่ยน 5 วัน / 1 เดือน\n"
        "  – วอลุ่มเทียบค่าเฉลี่ย 20 วัน (🔥 = ผิดปกติ)\n"
        "  – วันงบล่าสุด/ถัดไป + % ราคาตั้งแต่งบออก\n"
        "  – High / Low 5 วัน, 3 เดือน, 52 สัปดาห์\n"
        "  – % ห่างจากราคาปัจจุบัน\n\n"
        "<b>วันประกาศงบ</b>\n"
        "• <code>งบ AOT</code> — ดูวันงบล่าสุด/ถัดไป\n"
        "• <code>งบ AOT 13/11/2569</code> — บันทึกวันงบเอง\n\n"
        "<b>ติดตามหุ้นหลังงบ</b>\n"
        "• <code>ติดตาม AOT</code> — เพิ่มเข้าลิสต์ (หรือกดปุ่ม ➕ ใต้ผลสแกน)\n"
        "• <code>ลิสต์</code> — จัดอันดับตามคะแนนสัญญาณหลังงบ\n"
        "• <code>เลิกติดตาม AOT</code> — เอาออกจากลิสต์ (หลายตัวพร้อมกันได้)\n"
        "• บอทเช็คลิสต์ให้เองช่วงตลาดเปิด แจ้งทันทีเมื่อ\n"
        "  ทะลุไฮ 3 ด. / ผ่านไฮ 5 วัน / ทำไฮใหม่ / ⛔ หลุด Low ก่อนงบ\n"
        "• เตือนตอนเช้าถ้าหุ้นในลิสต์งบออกวันนี้/พรุ่งนี้\n\n"
        "<b>ขนาดไม้ (เสี่ยง 1% ของพอร์ตต่อไม้)</b>\n"
        "• <code>พอร์ต 500000</code> — ตั้งขนาดพอร์ต (ครั้งเดียว จำถาวร)\n"
        "• <code>ไม้ AOT</code> — ควรซื้อกี่หุ้น ให้หลุด ⛔ แล้วเสียแค่ 1%\n"
        "  (<code>ไม้ AOT 64.50</code> = ระบุราคาเข้าเอง)\n\n"
        "<b>บันทึกไม้จริง (trade journal)</b>\n"
        "• <code>ซื้อ AOT 32.50</code> — จดไม้เข้า พร้อมเก็บคะแนน/เส้น ⛔\n"
        "  ณ ตอนเข้า (ใส่จำนวนหุ้นได้: <code>ซื้อ AOT 32.50 300</code>)\n"
        "• <code>ขาย AOT 35.00</code> — ปิดไม้ สรุป % กำไรและ R ทันที\n"
        "• <code>เทรด</code> — ไม้ที่เปิดอยู่ + ผลไม้ที่ปิดแล้ว\n"
        "• <code>สถิติ</code> เทียบผลไม้จริงกับผลระบบให้อัตโนมัติ\n\n"
        "<b>ข่าวแจ้งงบจากเว็บ SET</b>\n"
        "• <code>ข่าวงบ</code> — ใครแจ้งผลประกอบการแล้วบ้าง (2 วันล่าสุด)\n"
        "• <code>สรุปงบ</code> — สรุปหุ้นแจ้งงบเมื่อวาน+เช้านี้ เรียงตามกำไรโต "
        "(สรุปงบ 3 = ย้อน 3 วัน)\n"
        "• บอทเฝ้าข่าวให้เองทุก 10 นาที ช่วงเช้า/หลังปิดตลาด\n"
        "  เจอบริษัทแจ้งงบ → เตือนทันที + บันทึกวันงบอัตโนมัติ\n"
        "  พร้อมตัวเลขจาก F45 เช่น กำไร 120.5 ลบ. (+45% YoY)\n"
        "• บอทส่งสรุปงบเช้าให้เองทุกวันทำการ 08:55 น.\n"
        "• <code>ยืนยัน</code> — ตลาดตอบรับหุ้นงบโตแรงเมื่อคืนยังไง\n"
        "  (gap + วอลุ่มเช้า) บอทส่งเองทุกวันทำการ 10:30 น.\n\n"
        "<b>สแกนหุ้นตอบรับงบดี</b>\n"
        "• <code>สแกน</code> — สแกนทั้งกระดานเดี๋ยวนี้ (~1-3 นาที)\n"
        "• อัตโนมัติทุกวันทำการ 17:30 น. บอทส่งผลให้เอง\n"
        "• ผลบันทึกลง scan_log.csv ไว้เปิดดูใน Excel\n\n"
        "<b>สถิติย้อนหลัง</b>\n"
        "• <code>สถิติ</code> — หุ้นติดสแกนแต่ละช่วงคะแนน\n"
        "  ผ่านไป 5/10/20 วัน ชนะกี่ % เฉลี่ยกี่ %\n\n"
        "ทุกคำสั่งพิมพ์เป็นอังกฤษได้ (ตัวเล็ก-ใหญ่ไม่สำคัญ):\n"
        "<code>scan</code> / <code>news</code> / <code>digest</code> / "
        "<code>confirm</code> / <code>stats</code> / <code>earn AOT</code> / "
        "<code>watch AOT</code> / <code>unwatch AOT</code> / <code>list</code> / "
        "<code>port 500000</code> / <code>size AOT</code> / "
        "<code>buy AOT 32.50</code> / <code>sell AOT 35</code> / <code>trades</code>",
        parse_mode="HTML",
    )


MAX_TICKERS = 10

# ── ผู้รับผลสแกนอัตโนมัติ (จำ chat ทุกคนที่เคยคุยกับบอท) ────────

_CHAT_IDS_PATH = os.path.join(_BASE_DIR, "chat_ids.json")


def _load_chat_ids():
    try:
        with open(_CHAT_IDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _register_chat(chat_id: int):
    ids = _load_chat_ids()
    if chat_id not in ids:
        ids.append(chat_id)
        try:
            with open(_CHAT_IDS_PATH, "w", encoding="utf-8") as f:
                json.dump(ids, f)
        except Exception:
            pass


SCAN_HOUR, SCAN_MINUTE = 17, 30      # เวลาสแกนอัตโนมัติ (หลังตลาดปิด 16:30)
REMIND_HOUR, REMIND_MINUTE = 8, 45   # เวลาเตือนวันงบตอนเช้า
HEART_HOUR, HEART_MINUTE = 8, 30     # เวลาส่ง heartbeat เช้า
DIGEST_HOUR, DIGEST_MINUTE = 8, 55   # เวลาส่งสรุปงบเช้า
CONFIRM_HOUR, CONFIRM_MINUTE = 10, 30  # เวลารายงานยืนยันรอบเช้า (ครึ่งชม.แรกหลังเปิด)
MONITOR_INTERVAL_MIN = 15            # เช็คลิสต์ติดตามทุกกี่นาที (ช่วงตลาดเปิด)
NEWS_POLL_MINUTES = 10               # เช็คข่าวแจ้งงบจากเว็บ SET ทุกกี่นาที


# ── รู้ตัวว่าปิดไปนานแค่ไหน (ใช้ตัดสินใจเก็บตกข่าวตอนเปิด) ──────

_ALIVE_PATH = os.path.join(_BASE_DIR, "last_alive.json")
CATCHUP_MIN_GAP_HOURS = 20   # หายเกินกี่ชม. ถึงเริ่มเก็บตก (ข้ามหนึ่งช่วงเย็น)
CATCHUP_MAX_DAYS = 7         # เก็บตกย้อนได้ลึกสุดกี่วัน


def _write_alive():
    try:
        with open(_ALIVE_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.datetime.now(ZoneInfo("Asia/Bangkok")).isoformat()}, f)
    except Exception:
        pass


def _read_alive():
    try:
        with open(_ALIVE_PATH, encoding="utf-8") as f:
            return datetime.datetime.fromisoformat(json.load(f)["ts"])
    except Exception:
        return None


def _catchup_days(last_alive, now):
    """ควรเก็บตกข่าวย้อนกี่วัน — None ถ้าไม่ต้อง (เพิ่งปิดไปไม่นาน)"""
    if last_alive is None:
        return None  # เพิ่งใช้ครั้งแรก ไม่มีช่วงที่หายไป
    gap_h = (now - last_alive).total_seconds() / 3600
    if gap_h < CATCHUP_MIN_GAP_HOURS:
        return None
    return min(CATCHUP_MAX_DAYS, int(gap_h // 24) + 1)


async def alive_job(context: ContextTypes.DEFAULT_TYPE):
    _write_alive()


async def startup_catchup_job(context: ContextTypes.DEFAULT_TYPE):
    """รันครั้งเดียวตอนบอทเปิด: ถ้าหายไปนาน ให้ดึงข่าวย้อนช่วงที่หาย
    มาบันทึกวันงบ/ตัวเลข F45 ให้ครบ (ข่าวเก่าบันทึกเงียบๆ ไม่สแปม)"""
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    days = _catchup_days(_read_alive(), now)
    _write_alive()
    if days is None:
        return
    log.info("catch-up: fetching %d days of news after downtime", days)
    try:
        hits = await asyncio.to_thread(
            set_news.check_new_earnings_news, 14, days)
    except Exception:
        log.exception("startup catch-up failed")
        return
    text = (f"🔄 บอทกลับมาทำงาน — เก็บตกข่าวแจ้งงบย้อน {days} วัน"
            "ให้เรียบร้อยแล้ว (บันทึกวันงบ/ตัวเลข F45 ครบ)")
    if hits:
        text += "\n\n" + build_news_alert_text(hits)
    for cid in _load_chat_ids():
        try:
            await _send_long(context.bot, cid, text, parse_mode="HTML")
        except Exception:
            pass


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    """ส่งสั้นๆ ทุกเช้าวันทำการว่ายังมีชีวิต — เช้าไหนไม่มีข้อความนี้
    = บอทตายอยู่ ให้ไปเปิดใหม่"""
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5:
        return
    for cid in _load_chat_ids():
        n_watch = len(stock_core.get_watchlist(cid))
        text = (f"✅ บอททำงานปกติ • ลิสต์ติดตาม {n_watch} ตัว • "
                f"สแกนอัตโนมัติ {SCAN_HOUR:02d}:{SCAN_MINUTE:02d} น.")
        try:
            await context.bot.send_message(cid, text)
        except Exception:
            pass


# ── เฝ้าข่าว "แจ้งผลประกอบการ" จากเว็บ SET ──────────────────────
# บริษัทส่วนใหญ่ยื่นงบหลังปิดตลาด (~17:00-21:30) หรือเช้าก่อนเปิด
# → poll เฉพาะสองช่วงนั้นพอ นอกช่วง = ไม่เปิดเบราว์เซอร์ให้เปลืองเครื่อง

def _in_news_window(t: datetime.time) -> bool:
    return (datetime.time(7, 0) <= t <= datetime.time(9, 45)
            or datetime.time(17, 0) <= t <= datetime.time(21, 45))


def build_news_alert_text(hits) -> str:
    """ประกอบข้อความแจ้งเตือนข่าวงบ — คืนพีคมีบริษัทยื่นเป็นร้อย
    จึงแสดงเต็มเฉพาะ "ตัวเด่น" (อยู่ในลิสต์/universe หรืองบโตแรง 🔥)
    ที่เหลือรวบเป็นสรุปย่อท้ายข้อความ กันแจ้งเตือนท่วม"""
    watch = set(stock_core.get_all_watched_symbols())  # union: มีคนติดตามอยู่
    uni = set(scanner.load_universe())
    top, rest = [], []
    for h in hits:
        parsed = h.get("f45_data")
        strong = parsed is not None and set_news.f45_is_strong(parsed)
        if h["symbol"] in watch or h["symbol"] in uni or strong:
            top.append((h, strong))
        else:
            rest.append(h)

    lines = ["📢 <b>บริษัทแจ้งผลประกอบการ (ข่าว SET)</b>", ""]
    for h, strong in top:
        star = "  ⭐ อยู่ในลิสต์" if h["symbol"] in watch else ""
        fire = " 🔥 งบโตแรง" if strong else ""
        lines.append(
            f"• <b>{html.escape(h['symbol'])}</b> {h['datetime']:%H:%M} น. — "
            f"{'/'.join(h['kinds'])}{fire}{star}"
        )
        if h.get("f45"):
            lines.append(f"    {html.escape(h['f45'])}")
    if rest:
        shown = ", ".join(html.escape(h["symbol"]) for h in rest[:30])
        if len(rest) > 30:
            shown += f" ...(+{len(rest) - 30})"
        if top:
            lines.append("")
        lines.append(f"อีก {len(rest)} บริษัท: {shown}")
        lines.append("(ดูรายละเอียด: พิมพ์ <code>ข่าวงบ</code>)")
    lines += [
        "",
        "บันทึกวันงบให้อัตโนมัติแล้ว — ดูปฏิกิริยาราคา: พิมพ์ชื่อหุ้น",
        "ตัวที่ตอบรับดีจะติดสแกนรอบ 17:30 (หรือพิมพ์ <code>สแกน</code>)",
    ]
    return "\n".join(lines)


# ── watchdog: ดึงข่าว SET ล้มติดต่อกันต้องไม่เงียบ ──────────────
# ล้มครั้งเดียวเป็นเรื่องปกติ (เน็ตสะดุด/เว็บช้า) แต่ล้มติดกันหลายรอบ
# ช่วงฤดูงบ = กำลังพลาดข่าวโดยไม่รู้ตัว — เด้งเตือนครั้งเดียวต่อการล่ม
# หนึ่งช่วง แล้วแจ้งอีกทีตอนกลับมาดึงได้ (นับใน memory — รีสตาร์ทบอท
# แล้วเริ่มนับใหม่ ซึ่งโอเค เพราะรีสตาร์ทคือความพยายามแก้อยู่แล้ว)
_NEWS_FAIL_ALERT_AFTER = 3   # 3 รอบ x 10 นาที ≈ ล่มต่อเนื่องครึ่งชั่วโมง
_news_fail_count = 0
_news_fail_alerted = False


async def _broadcast(bot, chat_ids, text: str):
    for cid in chat_ids:
        try:
            await bot.send_message(cid, text, parse_mode="HTML")
        except Exception:
            log.exception("broadcast failed (chat %s)", cid)


async def news_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    global _news_fail_count, _news_fail_alerted
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5 or _is_market_holiday(now.date()):
        return
    if not _in_news_window(now.time()):
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    try:
        hits = await asyncio.to_thread(set_news.check_new_earnings_news)
    except Exception:
        log.exception("SET news poll failed")
        _news_fail_count += 1
        if _news_fail_count >= _NEWS_FAIL_ALERT_AFTER and not _news_fail_alerted:
            _news_fail_alerted = True
            await _broadcast(
                context.bot, chat_ids,
                f"⚠️ <b>ดึงข่าวจากเว็บ SET ล้มเหลวติดต่อกัน {_news_fail_count} รอบ</b> "
                f"(~{_news_fail_count * 10} นาที)\n"
                "ช่วงนี้อาจพลาดข่าวแจ้งงบ — สาเหตุที่พบบ่อย: เน็ตมีปัญหา "
                "หรือเว็บ SET เปลี่ยนโครงสร้าง\n"
                "เช็คอาการได้ด้วยการรัน <code>python set_news.py</code> ที่เครื่องบอท\n"
                "ถ้ากลับมาดึงได้ปกติจะแจ้งให้ทราบอีกครั้ง",
            )
        return
    if _news_fail_alerted:
        await _broadcast(context.bot, chat_ids,
                         "✅ ดึงข่าวจากเว็บ SET กลับมาทำงานปกติแล้ว")
    _news_fail_count = 0
    _news_fail_alerted = False
    if not hits:
        return
    text = build_news_alert_text(hits)
    for cid in chat_ids:
        try:
            await _send_long(context.bot, cid, text, parse_mode="HTML")
        except Exception:
            log.exception("send news alert failed (chat %s)", cid)


def build_earnings_news_summary() -> str:
    """คำสั่ง "ข่าวงบ": ใครแจ้งผลประกอบการแล้วบ้างใน 2 วันล่าสุด"""
    rows = set_news.list_earnings_news(days_back=2)
    if not rows:
        return "📢 ไม่พบบริษัทแจ้งผลประกอบการใน 2 วันล่าสุด (ข่าวจากเว็บ SET)"
    watch = set(stock_core.get_all_watched_symbols())  # union: มีคนติดตามอยู่
    lines = ["📢 <b>บริษัทแจ้งผลประกอบการล่าสุด</b> (ข่าว SET ย้อน 2 วัน)", ""]
    for r in rows:
        star = "  ⭐" if r["symbol"] in watch else ""
        lines.append(
            f"• {r['datetime']:%d/%m %H:%M}  <b>{html.escape(r['symbol'])}</b> — "
            f"{'/'.join(r['kinds'])}{star}"
        )
        if r.get("f45"):
            lines.append(f"    {html.escape(r['f45'])}")
    lines += ["", "บันทึกวันงบเข้าระบบให้แล้ว — ดูปฏิกิริยาราคา: พิมพ์ชื่อหุ้น"]
    return "\n".join(lines)


# ── สรุปงบเช้า: รวบหุ้นแจ้งงบตั้งแต่เย็นวาน+เช้านี้เป็นข้อความเดียว ──
# ไม่ยิง Playwright เพิ่ม — อ่านจาก earnings_results.csv/filings_log.csv
# ที่ job poll ข่าว (news_monitor_job) สะสมไว้แล้วเท่านั้น

_DIGEST_STATE_PATH = os.path.join(_BASE_DIR, "digest_state.json")
_THAI_WEEKDAY = ["จ", "อ", "พ", "พฤ", "ศ", "ส", "อา"]


def _load_digest_state():
    try:
        with open(_DIGEST_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_digest_state(state):
    try:
        with open(_DIGEST_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _digest_window_start(now: datetime.datetime) -> datetime.datetime:
    """จุดเริ่ม window ปกติ: 09:00 ของวันทำการก่อนหน้า now (ข้ามเสาร์-อาทิตย์
    และวันหยุดตลาด) — เช้าวันจันทร์เลยได้ศุกร์ 09:00 ครอบเสาร์-อาทิตย์ไปด้วย"""
    day = now.date()
    while True:
        day -= datetime.timedelta(days=1)
        if day.weekday() < 5 and not _is_market_holiday(day):
            break
    return datetime.datetime.combine(day, datetime.time(9, 0), tzinfo=now.tzinfo)


def build_morning_digest(since_dt: datetime.datetime, now: datetime.datetime = None,
                          window_label: str = None) -> str:
    """ประกอบข้อความสรุปงบเช้า จากข้อมูลที่สะสมไว้แล้วเท่านั้น (ไม่ยิงเครือข่าย)

    since_dt: จุดเริ่ม window (ดู _digest_window_start / คำสั่ง "สรุปงบ N")
    window_label: ข้อความอธิบายช่วงเวลาในหัวเรื่อง (None = ใช้แบบ "ตั้งแต่เย็นวาน")
    คืน (ข้อความ, รายชื่อตัวโตแรงที่ยังไม่อยู่ในลิสต์ — ไว้ทำปุ่ม ➕ ติดตาม)
    หรือ ("", []) ถ้าไม่มีบริษัทแจ้งงบเลยในช่วงนั้น (ผู้เรียกตัดสินใจเองว่าจะพิมพ์อะไร)
    """
    if now is None:
        now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))

    results = set_news.load_results_since(since_dt)
    filings = set_news.load_filings_since(since_dt)

    # เฉพาะ symbol ล่าสุดของแต่ละตัวใน results (กันซ้ำถ้ามีหลายงวดในหน้าต่างเดียว)
    latest_result = {}
    for r in results:
        cur = latest_result.get(r["symbol"])
        if cur is None or r["news_datetime"] > cur["news_datetime"]:
            latest_result[r["symbol"]] = r

    watch = set(stock_core.get_all_watched_symbols())  # union: มีคนติดตามอยู่
    uni = set(scanner.load_universe())

    strong_in, strong_out, weak = [], [], []
    for sym, r in latest_result.items():
        parsed = {
            "profit_cur": r["profit_mb"] * 1e6,
            "profit_prior": r["profit_prior_mb"] * 1e6,
        }
        strong = set_news.f45_is_strong(parsed)
        bucket = None
        if strong:
            bucket = strong_in if (sym in watch or sym in uni) else strong_out
        else:
            weak.append(r)
            continue
        bucket.append(r)

    def sort_key(r):
        # พลิกเป็นกำไรอยู่บนสุด (profit_prior_mb <= 0) เรียง profit_mb มาก→น้อย
        # ที่เหลือเรียง yoy_pct มาก→น้อย
        flipped = r["profit_prior_mb"] <= 0
        return (0 if flipped else 1,
                -r["profit_mb"] if flipped else 0,
                -(r["yoy_pct"] if r["yoy_pct"] is not None else float("-inf")))

    strong_in.sort(key=sort_key)
    strong_out.sort(key=sort_key)
    weak.sort(key=lambda r: (r["yoy_pct"] is None,
                              -(r["yoy_pct"] if r["yoy_pct"] is not None else 0)))

    # 📄 filings ที่ยังไม่มีตัวเลข (อยู่ใน filings_log แต่ไม่อยู่ใน results ของช่วงนี้)
    numberless = sorted(sym for sym in filings if sym not in latest_result)

    total_symbols = len(set(latest_result) | set(filings))
    if total_symbols == 0:
        return "", []

    # ตัวโตแรงที่ยังไม่ได้เฝ้า — เรียงตามลำดับที่แสดง ไว้ให้ผู้เรียกทำปุ่ม ➕
    hot = [r["symbol"] for r in strong_in + strong_out if r["symbol"] not in watch]

    idx = 0

    def fmt_line(r, star):
        nonlocal idx
        idx += 1
        mark = " ⭐" if star else ""
        return f"{idx}. <b>{html.escape(r['symbol'])}</b>{mark}  {html.escape(r['summary'])}"

    lines = []
    weekday_th = _THAI_WEEKDAY[now.weekday()]
    if window_label is None:
        window_label = f"แจ้งงบตั้งแต่เย็นวาน {total_symbols} บริษัท"
    lines.append(f"🌅 <b>สรุปงบเช้านี้</b> — {weekday_th} {now:%d/%m} ({window_label})")
    lines.append("")

    if strong_in:
        lines.append(f"🔥 <b>งบโตแรง — อยู่ใน universe/ลิสต์</b> ({len(strong_in)})")
        for r in strong_in:
            lines.append(fmt_line(r, r["symbol"] in watch))
        lines.append("")

    if strong_out:
        lines.append(f"🔥 <b>งบโตแรง — นอก universe</b> ({len(strong_out)})")
        for r in strong_out:
            lines.append(fmt_line(r, r["symbol"] in watch))
        lines.append("")

    if weak:
        lines.append(f"📊 <b>มีตัวเลขแต่ไม่เข้าเกณฑ์โตแรง</b> ({len(weak)})")
        for r in weak:
            lines.append(fmt_line(r, r["symbol"] in watch))
        lines.append("")

    if numberless:
        shown = ", ".join(html.escape(s) for s in numberless[:30])
        if len(numberless) > 30:
            shown += f" ...(+{len(numberless) - 30})"
        lines.append(f"📄 แจ้งงบแล้วแต่ยังอ่านตัวเลขไม่ได้ ({len(numberless)}):")
        lines.append(shown)
        lines.append("")

    if hot:
        lines.append("เฝ้าตัวไหน: กดปุ่ม ➕ ด้านล่าง • ดูปฏิกิริยาราคา: พิมพ์ชื่อหุ้น")
    else:
        lines.append("ดูปฏิกิริยาราคา: พิมพ์ชื่อหุ้น • เฝ้าตัวไหน: <code>ติดตาม XXX</code>")
    lines.append("ตัวที่ตอบรับดีจะติดสแกนรอบ 17:30 วันนี้")
    return "\n".join(lines), hot


# สอง job (08:55 กับตอนเปิดบอท) อาจตกวันเดียวกัน — เช็ค last_sent ทั้งคู่
# กันส่งซ้ำ + ธง in-flight กันกรณียิงชนวินาทีเดียวกัน (state ยังไม่ทันบันทึก)
_digest_in_flight = False


async def _run_morning_digest(context: ContextTypes.DEFAULT_TYPE, label: str):
    """ตัวส่งสรุปงบเช้ากลาง: ส่งครั้งเดียวต่อวัน — ไม่มีข้อมูล = ไม่ส่ง ไม่บันทึกสถานะ"""
    global _digest_in_flight
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5 or _is_market_holiday(now.date()):
        return
    if _digest_in_flight:
        return
    if _load_digest_state().get("last_sent") == now.date().isoformat():
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    _digest_in_flight = True
    try:
        since_dt = _digest_window_start(now)
        try:
            text, hot = await asyncio.to_thread(build_morning_digest, since_dt, now)
        except Exception:
            log.exception("%s failed", label)
            return
        if not text:
            return
        buttons = _watch_buttons(hot)
        for cid in chat_ids:
            try:
                await _send_long(context.bot, cid, text,
                                 reply_markup=buttons, parse_mode="HTML")
            except Exception:
                log.exception("send %s failed (chat %s)", label, cid)
        state = _load_digest_state()
        state["last_sent"] = now.date().isoformat()
        _save_digest_state(state)
    finally:
        _digest_in_flight = False


async def morning_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """job รายวัน 08:55: สรุปงบเช้า"""
    await _run_morning_digest(context, "morning digest")


async def startup_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """ตอนเปิดบอท (หน่วง 300 วิ ให้ startup catch-up + news poll รอบแรกจบก่อน):
    ถ้าวันนี้ยังไม่ได้ส่งสรุปงบเช้า และยังไม่สายเกิน 16:30 → ส่งเลย
    (รองรับกรณีผู้ใช้เปิดคอมสาย ~09:30 เกินเวลา job 08:55 ไปแล้ว)"""
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.time() >= datetime.time(16, 30):
        return
    await _run_morning_digest(context, "startup morning digest")


# ── ยืนยันรอบเช้า (10:30): ตลาดตอบรับหุ้นงบโตแรงเมื่อคืนยังไง ───
# ปิด loop ของ workflow "เย็นเก็บโจทย์ → เช้ายืนยัน": เอาหุ้นงบโตแรง
# ตั้งแต่เย็นวาน (earnings_results.csv) มาดูราคา/วอลุ่มครึ่งชั่วโมงแรก
# แล้วแบ่ง ✅ ตลาดยืนยัน / 😐 ยังไม่ชัด / ❌ เปิดนิ่ง-ลบ = ตัดทิ้งตามกติกา
# (ราคา Yahoo delay ~15 นาที — 10:30 คือจุดแรกที่เห็นภาพครึ่งชม.แรกจริง)

CONFIRM_STRONG_CHG = 2.0   # วันนี้วิ่ง ≥ นี้ (%) = ตลาดตอบรับชัด
CONFIRM_FLAT_CHG = 0.5     # วิ่งไม่เกินนี้ (%) = เปิดนิ่ง/ลบ → ตัดทิ้ง
CONFIRM_VOL_RATIO = 0.5    # วอลุ่มสะสมเช้า ≥ ครึ่งของเฉลี่ยทั้งวัน 20 วัน = หนา
CONFIRM_MAX_SYMBOLS = 15   # เพดานตัวที่ดึงราคา (เท่า limit ปุ่ม ➕)


def build_morning_confirm(now: datetime.datetime = None):
    """รายงานยืนยันรอบเช้า — ยิง Yahoo เฉพาะตัวงบโตแรง (สูงสุด CONFIRM_MAX_SYMBOLS)

    คืน (ข้อความ, รายชื่อกลุ่ม ✅ ที่ยังไม่มีใครเฝ้า — ไว้ทำปุ่ม ➕)
    หรือ ("", []) ถ้าไม่มีหุ้นงบโตแรงตั้งแต่เย็นวาน"""
    if now is None:
        now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    since_dt = _digest_window_start(now)
    results = set_news.load_results_since(since_dt)

    # เฉพาะงวดล่าสุดของแต่ละ symbol (กันซ้ำ) แล้วคัดเฉพาะงบโตแรง
    latest = {}
    for r in results:
        cur = latest.get(r["symbol"])
        if cur is None or r["news_datetime"] > cur["news_datetime"]:
            latest[r["symbol"]] = r
    strong = [r for r in latest.values() if set_news.f45_is_strong({
        "profit_cur": r["profit_mb"] * 1e6,
        "profit_prior": r["profit_prior_mb"] * 1e6,
    })]
    if not strong:
        return "", []

    # เรียงพลิกกำไรก่อน ตามด้วย %YoY (ลำดับเดียวกับสรุปงบเช้า) — เกินเพดานตัดท้าย
    def sort_key(r):
        flipped = r["profit_prior_mb"] <= 0
        return (0 if flipped else 1,
                -r["profit_mb"] if flipped else 0,
                -(r["yoy_pct"] if r["yoy_pct"] is not None else float("-inf")))

    strong.sort(key=sort_key)
    dropped = max(0, len(strong) - CONFIRM_MAX_SYMBOLS)
    strong = strong[:CONFIRM_MAX_SYMBOLS]

    confirmed, unclear, rejected, failed = [], [], [], []
    for r in strong:
        try:
            d = stock_core.get_stock_data(r["symbol"])
        except Exception:
            d = None
        if d is None or d["day_change_pct"] is None:
            failed.append(r["symbol"])
            continue
        chg = d["day_change_pct"]
        vr = d["vol_ratio"] or 0
        if chg >= CONFIRM_STRONG_CHG and vr >= CONFIRM_VOL_RATIO:
            confirmed.append((r, d))
        elif chg <= CONFIRM_FLAT_CHG:
            rejected.append((r, d))
        else:
            unclear.append((r, d))

    idx = 0

    def fmt(r, d):
        nonlocal idx
        idx += 1
        return (f"{idx}. <b>{html.escape(r['symbol'])}</b>  "
                f"{format_signed_pct(d['day_change_pct'])} วันนี้ "
                f"(gap {format_signed_pct(d['gap_pct'])}, วอล {d['vol_ratio'] or 0:.1f}x)\n"
                f"    {html.escape(r['summary'])}")

    weekday_th = _THAI_WEEKDAY[now.weekday()]
    lines = [
        f"☀️ <b>ยืนยันรอบเช้า</b> — {weekday_th} {now:%d/%m %H:%M} "
        f"(งบโตแรงตั้งแต่เย็นวาน {len(strong) + dropped} ตัว)",
        "วอล = วอลุ่มสะสมวันนี้เทียบเฉลี่ยทั้งวัน 20 วัน",
        "",
    ]
    if confirmed:
        lines.append(f"✅ <b>ตลาดยืนยัน — เปิดวิ่งพร้อมวอลุ่ม</b> ({len(confirmed)})")
        lines += [fmt(r, d) for r, d in confirmed]
        lines.append("")
    if unclear:
        lines.append(f"😐 <b>ยังไม่ชัด — บวกแต่ไม่แรง/วอลุ่มเบา</b> ({len(unclear)})")
        lines += [fmt(r, d) for r, d in unclear]
        lines.append("")
    if rejected:
        lines.append(f"❌ <b>เปิดนิ่ง/ลบทั้งที่งบดี — ตัดทิ้งตามกติกา</b> ({len(rejected)})")
        lines += [fmt(r, d) for r, d in rejected]
        lines.append("")
    if failed:
        lines.append("⚠️ ดึงราคาไม่ได้: " +
                      ", ".join(html.escape(s) for s in failed))
        lines.append("")
    if dropped:
        lines.append(f"(แสดง {len(strong)} ตัวแรก — โตแรงทั้งหมด {len(strong) + dropped} ตัว "
                      f"ที่เหลือดูใน <code>สรุปงบ</code>)")
        lines.append("")
    lines.append("จุดเข้าตามกติกา: รอปิดวัน ≥+2.5% วอล ≥2x "
                 "หรือทะลุไฮ 3 ด. (เฝ้าให้เด้ง 🔥: กด ➕ / <code>ติดตาม XXX</code>)")

    watch = set(stock_core.get_all_watched_symbols())
    hot = [r["symbol"] for r, _ in confirmed if r["symbol"] not in watch]
    return "\n".join(lines), hot


# กันส่งซ้ำแบบเดียวกับสรุปงบเช้า: job 10:30 กับ startup fallback
# ใช้ digest_state.json ร่วมกัน (คนละ key) + ธง in-flight
_confirm_in_flight = False


async def _run_morning_confirm(context: ContextTypes.DEFAULT_TYPE, label: str):
    """ตัวส่งยืนยันรอบเช้ากลาง: ส่งครั้งเดียวต่อวัน — ไม่มีตัวโตแรง = เงียบ"""
    global _confirm_in_flight
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5 or _is_market_holiday(now.date()):
        return
    if _confirm_in_flight:
        return
    if _load_digest_state().get("confirm_last_sent") == now.date().isoformat():
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    _confirm_in_flight = True
    try:
        try:
            text, hot = await asyncio.to_thread(build_morning_confirm, now)
        except Exception:
            log.exception("%s failed", label)
            return
        if not text:
            return
        buttons = _watch_buttons(hot)
        for cid in chat_ids:
            try:
                await _send_long(context.bot, cid, text,
                                 reply_markup=buttons, parse_mode="HTML")
            except Exception:
                log.exception("send %s failed (chat %s)", label, cid)
        state = _load_digest_state()
        state["confirm_last_sent"] = now.date().isoformat()
        _save_digest_state(state)
    finally:
        _confirm_in_flight = False


async def morning_confirm_job(context: ContextTypes.DEFAULT_TYPE):
    """job รายวัน 10:30: ยืนยันรอบเช้า"""
    await _run_morning_confirm(context, "morning confirm")


async def startup_confirm_job(context: ContextTypes.DEFAULT_TYPE):
    """ตอนเปิดบอท: ถ้าเลย 10:30 แต่ยังไม่เที่ยง และวันนี้ยังไม่ได้ส่ง → ส่งเลย
    (เกินเที่ยงภาพ "ครึ่งชั่วโมงแรก" หมดความหมายแล้ว — เรียกเองได้ด้วย ยืนยัน)"""
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if not (datetime.time(CONFIRM_HOUR, CONFIRM_MINUTE)
            <= now.time() < datetime.time(12, 0)):
        return
    await _run_morning_confirm(context, "startup morning confirm")


# ── แจ้งเตือนเมื่อสถานะหุ้นในลิสต์เปลี่ยน (เช็คช่วงตลาดเปิด) ────
# จำสถานะรอบก่อนไว้ใน watch_state.json แล้ว push เฉพาะตอนเปลี่ยน:
# หลุด Low ก่อนงบ (จุดตัดขาดทุน) / ทะลุไฮ 3 เดือน / ผ่านไฮ 5 วัน
# / ทำไฮใหม่หลังงบ (เตือนวันละครั้ง)

_WATCH_STATE_PATH = os.path.join(_BASE_DIR, "watch_state.json")


def _load_watch_state():
    try:
        with open(_WATCH_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_watch_state(state):
    try:
        with open(_WATCH_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def check_watchlist_changes():
    """คืน dict {หุ้น: [ข้อความแจ้งเตือน]} (ว่าง = ไม่มีอะไรเปลี่ยน)
    watch_state เป็น global ต่อหุ้น — คำนวณครั้งเดียว ผู้เรียก route เอง"""
    symbols = stock_core.get_all_watched_symbols()
    if not symbols:
        return {}
    state = _load_watch_state()
    today = datetime.date.today().isoformat()
    alerts = {}
    for sym in symbols:
        try:
            d = stock_core.get_stock_data(sym)
        except Exception:
            d = None
        if d is None:
            continue
        s = d["post_signals"]
        if s is None or (d["days_since_earnings"] or 999) > 60:
            continue
        prev = state.get(sym)
        cur = {
            "above_pre_low": bool(s["above_pre_low"]),
            "broke_pre3m_high": bool(s["broke_pre3m_high"]),
            "broke_pre5d_high": bool(s["broke_pre5d_high"]),
            "new_high_date": today if s["days_since_new_high"] == 0
            else (prev or {}).get("new_high_date"),
        }
        # เจอครั้งแรก = เก็บ baseline เฉยๆ ไม่เตือน (กันเตือนถล่มตอนเพิ่มเข้าลิสต์)
        if prev is not None:
            price_txt = (f"ราคา {d['price']:.2f} "
                         f"({format_signed_pct(d['day_change_pct'])} วันนี้)")
            msgs = []
            if prev.get("above_pre_low", True) and not cur["above_pre_low"]:
                msgs.append(
                    f"⛔ <b>{sym}</b> หลุด Low ก่อนงบ "
                    f"(<code>{s['pre_earn_low']:.2f}</code>) — สัญญาณเสีย\n{price_txt}"
                )
            if not prev.get("broke_pre3m_high") and cur["broke_pre3m_high"]:
                msgs.append(
                    f"🔥 <b>{sym}</b> ทะลุไฮ 3 เดือนก่อนงบ "
                    f"(<code>{s['pre3m_high']:.2f}</code>) แล้ว\n{price_txt}"
                )
            elif not prev.get("broke_pre5d_high") and cur["broke_pre5d_high"]:
                msgs.append(
                    f"✅ <b>{sym}</b> ผ่านไฮ 5 วันก่อนงบ "
                    f"(<code>{s['pre5d_high']:.2f}</code>) แล้ว\n{price_txt}"
                )
            if (not msgs and s["days_since_new_high"] == 0
                    and prev.get("new_high_date") != today):
                msgs.append(f"📈 <b>{sym}</b> ทำไฮใหม่หลังงบวันนี้\n{price_txt}")
            if msgs:
                alerts[sym] = msgs
        state[sym] = cur
    # prune: ทิ้งสถานะหุ้นที่ไม่มีใครติดตามแล้ว (คง baseline-on-add ตอนเพิ่มกลับ)
    for sym in list(state.keys()):
        if sym not in symbols:
            state.pop(sym, None)
    _save_watch_state(state)
    return alerts


async def watchlist_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5 or _is_market_holiday(now.date()):
        return
    if not (datetime.time(10, 0) <= now.time() <= datetime.time(17, 0)):
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    try:
        alerts_by_symbol = await asyncio.to_thread(check_watchlist_changes)
    except Exception:
        log.exception("watchlist monitor failed")
        return
    if not alerts_by_symbol:
        return
    for cid in chat_ids:
        msgs = []
        for sym in stock_core.get_watchlist(cid):
            msgs.extend(alerts_by_symbol.get(sym, []))
        if not msgs:
            continue
        text = "📣 <b>แจ้งเตือนลิสต์ติดตาม</b>\n\n" + "\n\n".join(msgs)
        try:
            await _send_long(context.bot, cid, text, parse_mode="HTML")
        except Exception:
            log.exception("send monitor alert failed (chat %s)", cid)


# ── เตือนตอนเช้า: หุ้นในลิสต์ตัวไหนงบออกวันนี้/พรุ่งนี้ ─────────

def build_earnings_reminder(chat_id):
    soon = []
    for sym in stock_core.get_watchlist(chat_id):
        try:
            d = stock_core.get_stock_data(sym)
        except Exception:
            continue
        if d is None or d["next_earnings"] is None:
            continue
        days = d["days_to_earnings"]
        if days is not None and 0 <= days <= 1:
            when = "วันนี้" if days == 0 else "พรุ่งนี้"
            soon.append(f"• <b>{sym}</b> งบออก{when} ({d['next_earnings']:%d/%m/%Y})")
    if not soon:
        return None
    return "🗓 <b>เตือนวันประกาศงบ (หุ้นในลิสต์)</b>\n" + "\n".join(soon)


async def earnings_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5:
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    for cid in chat_ids:
        try:
            text = await asyncio.to_thread(build_earnings_reminder, cid)
        except Exception:
            log.exception("earnings reminder failed (chat %s)", cid)
            continue
        if not text:
            continue
        try:
            await context.bot.send_message(cid, text, parse_mode="HTML")
        except Exception:
            log.exception("send reminder failed (chat %s)", cid)


# ── ปุ่ม "➕ ติดตาม" ใต้ผลสแกน/สรุปงบ (กดแล้วเข้าลิสต์ทันที) ─────
# รับได้ทั้ง dict ผลสแกน ({"ticker": ...}) และชื่อหุ้นเปล่าๆ (จากสรุปงบ)

def _watch_buttons(hits, limit=15):
    if not hits:
        return None
    rows, row = [], []
    for d in hits[:limit]:
        base = (d["ticker"] if isinstance(d, dict) else d).replace(".BK", "")
        row.append(InlineKeyboardButton(f"➕ {base}", callback_data=f"watch:{base}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    if data.startswith("watch:"):
        base, symbols = stock_core.add_to_watchlist(
            data.split(":", 1)[1], update.effective_chat.id
        )
        await q.answer(f"เพิ่ม {base} เข้าลิสต์แล้ว ({len(symbols)} ตัว)")
        try:
            await q.message.reply_text(
                f"✅ เพิ่ม <b>{html.escape(base)}</b> เข้าลิสต์แล้ว "
                f"(รวม {len(symbols)} ตัว) — ดูทั้งหมด: <code>ลิสต์</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        await q.answer()


def run_best_scan():
    """สแกนทั้งตลาดจากข้อมูล Trading_Dashboard ถ้าข้อมูลสดพอ
    ไม่งั้นถอยไปสแกนรายชื่อหลัก 94 ตัวผ่าน Yahoo ตามเดิม

    คืน (hits, scanned_count, source_note, unknowns)"""
    full = scanner.run_full_scan()
    if full is not None:
        hits, n, last_date, unknowns = full
        note = f"โหมดทั้งตลาด • ข้อมูล Trading_Dashboard ถึง {last_date:%d/%m/%Y}"
        return hits, n, note, unknowns
    hits, n = scanner.run_scan()
    note = ("โหมดรายชื่อหลัก (ข้อมูล Trading_Dashboard ไม่สด — "
            "รัน bt_fetch.py ที่โปรเจคนั้นเพื่อปลดล็อกสแกนทั้งตลาด 883 ตัว)")
    return hits, n, note, None


async def daily_scan_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5 or _is_market_holiday(now.date()):
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    try:
        hits, n, note, unknowns = await asyncio.to_thread(run_best_scan)
    except Exception:
        log.exception("daily scan failed")
        return
    report = scanner.format_report(hits, n, source_note=note, unknowns=unknowns)
    buttons = _watch_buttons(hits)
    for cid in chat_ids:
        try:
            await _send_long(context.bot, cid, report,
                             reply_markup=buttons, parse_mode="HTML")
        except Exception:
            log.exception("send scan report failed (chat %s)", cid)


def _parse_thai_date(s: str):
    """แปลง dd/mm, dd/mm/yyyy (ค.ศ. หรือ พ.ศ.) เป็น date — คืน None ถ้าไม่ใช่วันที่"""
    parts = s.split("/")
    if len(parts) not in (2, 3):
        return None
    try:
        day, month = int(parts[0]), int(parts[1])
        if len(parts) == 3:
            year = int(parts[2])
            if year < 100:
                # ปีย่อ 2 หลัก: 60-99 = พ.ศ. 25xx (69 → 2569)
                # ต่ำกว่านั้น = ค.ศ. 20xx (26 → 2026) — สองแบบนี้ชี้ปีเดียวกัน
                year += 2500 if year >= 60 else 2000
            if year > 2200:         # พ.ศ. → ค.ศ.
                year -= 543
        else:
            year = datetime.date.today().year
        return datetime.date(year, month, day)
    except ValueError:
        return None


def handle_earnings_command(tokens) -> str:
    """คำสั่ง: งบ TICKER [วันที่] — ดูหรือบันทึกวันประกาศงบ"""
    if len(tokens) < 2:
        return (
            "วิธีใช้คำสั่ง <b>งบ</b>\n"
            "• <code>งบ AOT</code> — ดูวันประกาศงบล่าสุด/ถัดไป\n"
            "• <code>งบ AOT 13/11/2569</code> — บันทึกวันงบเอง (ใส่ พ.ศ. หรือ ค.ศ. ก็ได้)"
        )

    ticker = tokens[1]
    if len(tokens) >= 3:
        date_obj = _parse_thai_date(tokens[2])
        if date_obj is None:
            return "⚠️ รูปแบบวันที่ไม่ถูกต้อง — ใช้ <code>วัน/เดือน/ปี</code> เช่น <code>13/11/2569</code>"
        base = stock_core.set_manual_earnings_date(ticker, date_obj)
        return (
            f"✅ บันทึกแล้ว: <b>{html.escape(base)}</b> งบออก <b>{date_obj:%d/%m/%Y}</b>\n"
            "วันที่นี้จะแสดงในผลลัพธ์ของหุ้นตัวนี้ทุกครั้ง"
        )

    # ดูอย่างเดียว
    try:
        d = stock_core.get_stock_data(ticker)
    except Exception:
        d = None
    if d is None:
        return f"❌ ไม่พบข้อมูล <b>{html.escape(ticker)}</b>"
    lines = [f"🗓 <b>วันประกาศงบ — {html.escape(str(d['name']))}</b>"]
    if d["last_earnings"]:
        line = f"• งบล่าสุด   : {d['last_earnings']:%d/%m/%Y} ({d['days_since_earnings']} วันก่อน)"
        if d["since_earnings_pct"] is not None:
            line += f"  → ตั้งแต่งบ <b>{format_signed_pct(d['since_earnings_pct'])}</b>"
        lines.append(line)
    if d["next_earnings"]:
        lines.append(f"• งบรอบถัดไป : {d['next_earnings']:%d/%m/%Y} (อีก {d['days_to_earnings']} วัน)")
    if not d["last_earnings"] and not d["next_earnings"]:
        lines.append("ยังไม่มีข้อมูล — บันทึกเองได้: <code>งบ " + html.escape(ticker.upper()) + " 13/11/2569</code>")
    return "\n".join(lines)


def build_watchlist_summary(chat_id) -> str:
    """สรุปหุ้นที่ติดตาม เรียงตามแรงตอบรับวันงบ (มาก → น้อย)"""
    symbols = stock_core.get_watchlist(chat_id)
    if not symbols:
        return (
            "📋 ยังไม่มีหุ้นในลิสต์\n"
            "เพิ่มด้วย: <code>ติดตาม AOT</code>"
        )

    rows = []
    errors = []
    for sym in symbols:
        try:
            d = stock_core.get_stock_data(sym)
        except Exception:
            d = None
        if d is None:
            errors.append(sym)
            continue
        rows.append(d)
        time.sleep(0.3)  # เว้นจังหวะกัน rate limit

    def rank_key(d):
        score, _ = stock_core.signal_score(d)
        r = d["earn_reaction"]
        react = r["change_pct"] if (r and r["change_pct"] is not None) else float("-inf")
        return (score if score is not None else -1, react)

    rows.sort(key=rank_key, reverse=True)

    lines = ["📋 <b>หุ้นที่ติดตาม</b> (เรียงตามคะแนนสัญญาณหลังงบ)", ""]
    for i, d in enumerate(rows, 1):
        base = d["ticker"].replace(".BK", "")
        r = d["earn_reaction"]
        s = d["post_signals"]
        score, stars = stock_core.signal_score(d)
        head = f"{i}. <b>{base}</b>"
        if score is not None:
            head += f"  {stars} ({score}/{stock_core.SCORE_MAX})"
        lines.append(head)
        if r is not None and r["change_pct"] is not None:
            vol_txt = f" วอล {r['vol_ratio']:.1f}x{volume_flag(r['vol_ratio'])}" if r["vol_ratio"] else ""
            lines.append(f"    วันงบ <b>{format_signed_pct(r['change_pct'])}</b>{vol_txt}")
        flags = []
        if s is not None:
            if s["broke_pre3m_high"]:
                flags.append("ทะลุไฮ 3 ด. ✅")
            elif s["broke_pre5d_high"]:
                flags.append("ผ่านไฮ 5 วัน ✅")
            if s["days_since_new_high"] == 0:
                flags.append("ไฮใหม่วันนี้ 🔥")
            elif s["days_since_new_high"] <= 5:
                flags.append(f"ไฮใหม่ {s['days_since_new_high']} วันก่อน")
            streak = s.get("weekly_hh_streak") or 0
            if streak >= 2:
                flags.append(f"ไฮยกขึ้น {streak} สัปดาห์ติด 🔥")
            if not s["above_pre_low"]:
                flags.append("⛔ หลุด Low ก่อนงบ")
        since_txt = ""
        if d["since_earnings_pct"] is not None:
            since_txt = f" • ตั้งแต่งบ <b>{format_signed_pct(d['since_earnings_pct'])}</b>"
        lines.append(f"    วันนี้ {format_signed_pct(d['day_change_pct'])}{since_txt}")
        if flags:
            lines.append("    " + " • ".join(flags))
    if errors:
        lines.append("")
        lines.append("⚠️ ดึงไม่ได้: " + ", ".join(errors))
    lines.append("")
    lines.append("ลบออก: <code>เลิกติดตาม AOT</code>")
    return "\n".join(lines)


# ── คำนวณขนาดไม้ (position sizing) ตามกติกาท้าย workflow ─────────
# เสี่ยงคงที่ RISK_PCT_PER_TRADE% ของพอร์ตต่อไม้: จำนวนหุ้น = เงินเสี่ยง
# หารด้วยระยะจากราคาเข้าถึงเส้น ⛔ (Low 5 วันก่อนงบ) ปัดลงเป็น board lot
# ขนาดพอร์ตตั้งครั้งเดียวต่อ chat เก็บใน port_settings.json

RISK_PCT_PER_TRADE = 1.0   # % ของพอร์ตที่ยอมเสียต่อไม้
BOARD_LOT = 100            # หุ้นไทยซื้อขายขั้นต่ำ lot ละ 100 หุ้น

_PORT_SETTINGS_PATH = os.path.join(_BASE_DIR, "port_settings.json")


def _load_port_settings():
    try:
        with open(_PORT_SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_port_size(chat_id):
    v = _load_port_settings().get(str(chat_id))
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def set_port_size(chat_id, value: float):
    data = _load_port_settings()
    data[str(chat_id)] = value
    try:
        with open(_PORT_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        log.exception("save port settings failed")


def build_position_size(chat_id, ticker_input: str, entry: float = None) -> str:
    """คำนวณจำนวนหุ้นให้เสี่ยง RISK_PCT_PER_TRADE% ของพอร์ตพอดีถ้าหลุด ⛔

    entry=None ใช้ราคาล่าสุด / ระบุเองได้กรณีวางแผนเข้าที่ราคาอื่น"""
    port = get_port_size(chat_id)
    if not port:
        return ("ยังไม่ได้ตั้งขนาดพอร์ต — พิมพ์ <code>พอร์ต 500000</code> "
                "ก่อนครับ (ตั้งครั้งเดียว บอทจำถาวร)")
    try:
        d = stock_core.get_stock_data(ticker_input)
    except YFRateLimitError:
        return "⏳ โดน Yahoo จำกัดการเรียกชั่วคราว — รอสักครู่แล้วลองใหม่ครับ"
    if d is None:
        return f"❌ ไม่พบข้อมูล {html.escape(ticker_input.upper())}"
    base = d["ticker"].replace(".BK", "")
    s = d.get("post_signals")
    if not s or s.get("pre_earn_low") is None:
        return (f"ยังไม่รู้วันงบล่าสุดของ <b>{html.escape(base)}</b> "
                "เลยหาเส้น ⛔ (Low 5 วันก่อนงบ) ไม่ได้\n"
                f"บันทึกวันงบก่อน: <code>งบ {html.escape(base)} 13/11/2569</code>")

    stop = s["pre_earn_low"]
    price = entry if entry is not None else d["price"]
    if price <= stop:
        return (f"⛔ ราคา {price:,.2f} ต่ำกว่า/เท่ากับเส้น ⛔ {stop:,.2f} แล้ว "
                "— สัญญาณเสียตามกติกา ไม่ควรเข้าไม้นี้ครับ")

    risk_amount = port * RISK_PCT_PER_TRADE / 100
    per_share_risk = price - stop
    shares = int(risk_amount / per_share_risk // BOARD_LOT) * BOARD_LOT
    if shares <= 0:
        lot_risk = per_share_risk * BOARD_LOT
        return (f"ระยะเข้า {price:,.2f} → ⛔ {stop:,.2f} กว้างเกินสำหรับพอร์ตนี้\n"
                f"ขั้นต่ำ 1 lot ({BOARD_LOT} หุ้น) หลุด ⛔ จะเสีย {lot_risk:,.0f} บ. "
                f"≈ {lot_risk / port * 100:.1f}% ของพอร์ต "
                f"(เกินเกณฑ์ {RISK_PCT_PER_TRADE:.0f}%) — ข้ามตัวนี้ดีกว่าครับ")

    cost = shares * price
    capped = False
    if cost > port:
        # ตามสูตรต้องใช้เงินมากกว่าที่มี (⛔ ชิดราคามาก) — ลดเหลือเท่าที่ซื้อไหว
        shares = int(port / price // BOARD_LOT) * BOARD_LOT
        if shares <= 0:
            return (f"ราคา {price:,.2f} ต่อหุ้น 1 lot ({BOARD_LOT} หุ้น) "
                    "แพงกว่าพอร์ตทั้งก้อน — ซื้อไม่ได้ครับ")
        cost = shares * price
        capped = True
    actual_risk = shares * per_share_risk

    dist_pct = (stop - price) / price * 100
    when = (f"ราคาเข้าที่ระบุเอง" if entry is not None
            else f"ราคาล่าสุด {d['fetched_time']} น.")
    lines = [
        f"📐 <b>ขนาดไม้ {html.escape(base)}</b> — พอร์ต {port:,.0f} บ. "
        f"เสี่ยง {RISK_PCT_PER_TRADE:.0f}%/ไม้",
        f"เข้า {price:,.2f} ({when}) → เส้น ⛔ {stop:,.2f} ({dist_pct:.1f}%)",
        "",
        f"ซื้อ <b>{shares:,} หุ้น</b> ≈ {cost:,.0f} บ. "
        f"({cost / port * 100:.1f}% ของพอร์ต)",
        f"หลุด ⛔ ขาดทุน ~{actual_risk:,.0f} บ. "
        f"({actual_risk / port * 100:.2f}% ของพอร์ต)",
    ]
    if capped:
        lines.append(f"⚠️ ตามสูตรต้องใช้เงินเกินพอร์ต — ปรับลงเหลือเท่าที่ซื้อไหว "
                     f"ความเสี่ยงจริงจึงต่ำกว่าเกณฑ์")
    elif cost / port > 0.30:
        lines.append("⚠️ ไม้นี้กินพอร์ตเกิน 30% (เส้น ⛔ ชิดราคาทำให้ไม้ใหญ่) "
                     "— เงินจมกระจุกตัว พิจารณาลดขนาดเอง")
    if (d["days_since_earnings"] or 0) > 60:
        lines.append(f"⚠️ งบออกมา {d['days_since_earnings']} วันแล้ว "
                     "— เส้น ⛔ ก่อนงบอาจหมดความหมายไปแล้ว")
    lines.append("")
    lines.append(f"เส้น ⛔ = Low 5 วันก่อนงบ — <code>ติดตาม {html.escape(base)}</code> "
                 "แล้วบอทเฝ้าให้ทุก 15 นาที")
    return "\n".join(lines)


# ── บันทึกไม้จริง (trade journal): ซื้อ / ขาย / เทรด ─────────────
# เก็บลง trades_log.csv ผ่าน stock_core.log_trade — แถวซื้อเก็บบริบท
# ณ ตอนเข้า (วันงบ/เส้น ⛔/คะแนน) เพื่อให้ "สถิติ" เทียบผลจริงกับ
# ผลระบบย้อนหลังได้


def record_buy(chat_id, ticker_input: str, price: float, shares=None) -> str:
    """บันทึกไม้ซื้อ + เก็บบริบทระบบ ณ ตอนเข้า (ดึง Yahoo ตัวเดียว)"""
    existing = stock_core.get_open_trade(chat_id, ticker_input)
    if existing:
        base = existing["symbol"]
        return (f"มีไม้ <b>{html.escape(base)}</b> เปิดอยู่แล้ว "
                f"(ซื้อ {existing['price']:,.2f} เมื่อ {existing['datetime']:%d/%m})\n"
                f"ปิดก่อนด้วย <code>ขาย {html.escape(base)} ราคา</code> "
                "ถึงจะเปิดไม้ใหม่ได้ครับ")

    # บริบทระบบ ณ ตอนเข้า — ดึงไม่ได้ก็บันทึกไม้ได้ (ช่องว่าง)
    earn_date = stop = score = None
    base = ticker_input.upper().replace(".BK", "")
    try:
        d = stock_core.get_stock_data(ticker_input)
    except Exception:
        d = None
    if d is not None:
        base = d["ticker"].replace(".BK", "")
        earn_date = d.get("last_earnings")
        s = d.get("post_signals")
        if s:
            stop = s.get("pre_earn_low")
        score, _ = stock_core.signal_score(d)

    stock_core.log_trade(chat_id, base, "buy", price, shares=shares,
                         earn_date=earn_date, stop=stop, score=score)

    lines = [f"📝 บันทึกซื้อ <b>{html.escape(base)}</b> @ {price:,.2f}"
             + (f" × {shares:,} หุ้น ≈ {price * shares:,.0f} บ." if shares else "")]
    if stop is not None:
        dist = (stop - price) / price * 100
        lines.append(f"เส้น ⛔ {stop:,.2f} ({dist:.1f}%)"
                     + (f" — หลุดแล้วเสีย ~{(price - stop) * shares:,.0f} บ."
                        if shares else ""))
        if price <= stop:
            lines.append("⚠️ ราคาซื้อต่ำกว่าเส้น ⛔ อยู่แล้ว — นอกกติการะบบนะครับ")
    if score is not None:
        lines.append(f"คะแนนระบบ ณ ตอนเข้า: {score}/{stock_core.SCORE_MAX}")
    if d is None:
        lines.append("(ดึงข้อมูลระบบไม่ได้ — บันทึกเฉพาะราคา)")
    lines.append("")
    tail = (f"ปิดไม้: <code>ขาย {html.escape(base)} ราคา</code> • "
            "ดูไม้ทั้งหมด: <code>เทรด</code>")
    if base not in stock_core.get_watchlist(chat_id):
        tail += f" • เฝ้า ⛔ ให้: <code>ติดตาม {html.escape(base)}</code>"
    lines.append(tail)
    return "\n".join(lines)


def record_sell(chat_id, ticker_input: str, price: float) -> str:
    """ปิดไม้: บันทึกแถวขาย แล้วสรุปผลของคู่ซื้อ-ขายทันที"""
    open_trade = stock_core.get_open_trade(chat_id, ticker_input)
    base = ticker_input.upper().replace(".BK", "")
    if open_trade is None:
        return (f"ไม่มีไม้ <b>{html.escape(base)}</b> ที่เปิดอยู่ครับ "
                f"— บันทึกซื้อก่อน: <code>ซื้อ {html.escape(base)} ราคา</code>")
    base = open_trade["symbol"]
    stock_core.log_trade(chat_id, base, "sell", price,
                         shares=open_trade["shares"])

    buy = open_trade
    pnl_pct = (price - buy["price"]) / buy["price"] * 100
    held_days = (datetime.datetime.now(ZoneInfo("Asia/Bangkok")).date()
                 - buy["datetime"].date()).days
    emoji = "✅" if pnl_pct > 0 else "❌"
    lines = [
        f"{emoji} ปิดไม้ <b>{html.escape(base)}</b>: "
        f"{buy['price']:,.2f} → {price:,.2f} = <b>{pnl_pct:+.1f}%</b> "
        f"(ถือ {held_days} วัน)"
    ]
    if buy["shares"]:
        pnl_baht = (price - buy["price"]) * buy["shares"]
        lines.append(f"กำไร/ขาดทุน ~{pnl_baht:+,.0f} บ. ({buy['shares']:,} หุ้น)")
    if buy["stop"] is not None and buy["price"] > buy["stop"]:
        r_multiple = (price - buy["price"]) / (buy["price"] - buy["stop"])
        lines.append(f"R-multiple: {r_multiple:+.1f}R "
                     f"(เสี่ยง 1R = {buy['price'] - buy['stop']:,.2f} บ./หุ้น)")
    lines.append("")
    lines.append("ดูสถิติสะสม: <code>สถิติ</code> • ไม้ที่เหลือ: <code>เทรด</code>")
    return "\n".join(lines)


def build_trades_report(chat_id) -> str:
    """คำสั่ง "เทรด": ไม้ที่เปิดอยู่ (พร้อมกำไรลอยตัว) + ไม้ปิดล่าสุด"""
    rows = stock_core.load_trades(chat_id)
    if not rows:
        return ("ยังไม่เคยบันทึกไม้ครับ\n"
                "ซื้อจริงเมื่อไหร่พิมพ์ <code>ซื้อ AOT 32.50</code> "
                "(ใส่จำนวนหุ้นได้: <code>ซื้อ AOT 32.50 300</code>)\n"
                "ขายแล้วพิมพ์ <code>ขาย AOT 35.00</code> — "
                "สะสมไว้ให้ <code>สถิติ</code> เทียบผลจริงกับผลระบบ")
    closed, open_ = stock_core.pair_trades(rows)
    lines = []

    if open_:
        lines.append(f"📂 <b>ไม้ที่เปิดอยู่</b> ({len(open_)})")
        for t in sorted(open_, key=lambda x: x["datetime"]):
            try:
                d = stock_core.get_stock_data(t["symbol"])
            except Exception:
                d = None
            cur = f"ตอนนี้ {d['price']:,.2f} ({(d['price'] - t['price']) / t['price'] * 100:+.1f}%)" \
                if d is not None else "ดึงราคาล่าสุดไม่ได้"
            stop_txt = ""
            if t["stop"] is not None:
                stop_txt = f" ⛔ {t['stop']:,.2f}"
                if d is not None and d["price"] <= t["stop"]:
                    stop_txt += " — หลุดแล้ว!"
            lines.append(
                f"• <b>{html.escape(t['symbol'])}</b> "
                f"ซื้อ {t['price']:,.2f} ({t['datetime']:%d/%m}) → {cur}{stop_txt}"
            )
        lines.append("")

    if closed:
        recent = sorted(closed, key=lambda p: p["sell"]["datetime"])[-10:]
        lines.append(f"📁 <b>ไม้ที่ปิดแล้ว</b> (ล่าสุด {len(recent)}/{len(closed)})")
        for p in reversed(recent):
            b_, s_ = p["buy"], p["sell"]
            pct = (s_["price"] - b_["price"]) / b_["price"] * 100
            mark = "✅" if pct > 0 else "❌"
            lines.append(
                f"{mark} {html.escape(b_['symbol'])} "
                f"{b_['price']:,.2f}→{s_['price']:,.2f} = {pct:+.1f}% "
                f"({b_['datetime']:%d/%m}–{s_['datetime']:%d/%m})"
            )
        pcts = [(p["sell"]["price"] - p["buy"]["price"]) / p["buy"]["price"] * 100
                for p in closed]
        wins = sum(1 for x in pcts if x > 0)
        lines.append("")
        lines.append(f"รวม {len(closed)} ไม้: ชนะ {wins} ({wins / len(closed) * 100:.0f}%) "
                     f"เฉลี่ย {sum(pcts) / len(pcts):+.1f}%")
        lines.append("เทียบกับผลระบบ: <code>สถิติ</code>")
    return "\n".join(lines)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_chat(update.effective_chat.id)
    text = update.message.text.strip()
    tickers = text.split()

    # สแกนทั้งกระดานเดี๋ยวนี้: "สแกน"
    if len(tickers) == 1 and tickers[0].lower() in ("สแกน", "scan"):
        await update.message.reply_text(
            "🔍 กำลังสแกน... ใช้เวลาประมาณ 1-3 นาที รอสักครู่ครับ"
        )
        hits, n, note, unknowns = await asyncio.to_thread(run_best_scan)
        await _reply_long(
            update.message,
            scanner.format_report(hits, n, source_note=note, unknowns=unknowns),
            reply_markup=_watch_buttons(hits),
            parse_mode="HTML",
        )
        return

    # ข่าวแจ้งงบจากเว็บ SET: "ข่าวงบ"
    if len(tickers) == 1 and tickers[0].lower() in ("ข่าวงบ", "news"):
        await update.message.reply_text(
            "⏳ กำลังเช็คข่าวจากเว็บ SET (~1 นาที ถ้ามี F45 ให้อ่านตัวเลข)..."
        )
        result = await asyncio.to_thread(build_earnings_news_summary)
        await _reply_long(update.message, result, parse_mode="HTML")
        return

    # สรุปงบเช้า: "สรุปงบ" (window ปกติ) / "สรุปงบ N" (ย้อน N วัน, clamp 1..7)
    # อ่านจากไฟล์ล้วนๆ ไม่ยิงเครือข่าย — ตอบได้ทันทีไม่ต้องมีข้อความ "⏳ กำลัง..."
    if tickers and tickers[0].lower() in ("สรุปงบ", "digest"):
        now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
        if len(tickers) >= 2 and tickers[1].isdigit():
            n_days = max(1, min(7, int(tickers[1])))
            since_dt = now - datetime.timedelta(days=n_days)
            window_label = f"ย้อนหลัง {n_days} วัน"
        else:
            since_dt = _digest_window_start(now)
            window_label = None
        result, hot = build_morning_digest(since_dt, now, window_label=window_label)
        if not result:
            await update.message.reply_text(
                f"📭 ไม่มีบริษัทแจ้งงบตั้งแต่ {since_dt:%d/%m %H:%M} น. ครับ"
            )
            return
        await _reply_long(update.message, result,
                          reply_markup=_watch_buttons(hot), parse_mode="HTML")
        return

    # ยืนยันรอบเช้า: "ยืนยัน" — ตลาดตอบรับหุ้นงบโตแรงเมื่อคืนยังไง
    if len(tickers) == 1 and tickers[0].lower() in ("ยืนยัน", "confirm"):
        await update.message.reply_text(
            "⏳ กำลังเช็คราคาหุ้นงบโตแรงตั้งแต่เย็นวาน..."
        )
        text, hot = await asyncio.to_thread(build_morning_confirm)
        if not text:
            await update.message.reply_text(
                "📭 ไม่มีหุ้นงบโตแรง (+30% YoY / พลิกเป็นกำไร) "
                "ตั้งแต่เย็นวานครับ — ดูทั้งหมด: <code>สรุปงบ</code>",
                parse_mode="HTML",
            )
            return
        await _reply_long(update.message, text,
                          reply_markup=_watch_buttons(hot), parse_mode="HTML")
        return

    # สถิติย้อนหลังจาก scan_log.csv: "สถิติ"
    if len(tickers) == 1 and tickers[0].lower() in ("สถิติ", "stats"):
        await update.message.reply_text(
            "⏳ กำลังคำนวณสถิติจาก scan_log.csv (อาจใช้เวลาสักครู่)..."
        )
        result = await asyncio.to_thread(
            stats.build_stats_report, True, update.effective_chat.id
        )
        await _reply_long(update.message, result, parse_mode="HTML")
        return

    # คำสั่งจัดการวันประกาศงบ: "งบ AOT" / "งบ AOT 13/11/2569"
    if tickers and tickers[0].lower() in ("งบ", "earn", "earnings"):
        result = await asyncio.to_thread(handle_earnings_command, tickers)
        await update.message.reply_text(result, parse_mode="HTML")
        return

    # บันทึกไม้จริง: "ซื้อ AOT 32.50 [300]" / "ขาย AOT 35.00" / "เทรด"
    if tickers and tickers[0].lower() in ("ซื้อ", "buy"):
        if len(tickers) < 3:
            await update.message.reply_text(
                "พิมพ์ <code>ซื้อ AOT 32.50</code> "
                "(ใส่จำนวนหุ้นได้: <code>ซื้อ AOT 32.50 300</code>)",
                parse_mode="HTML",
            )
            return
        try:
            price = float(tickers[2].replace(",", ""))
            if price <= 0:
                raise ValueError
            shares = None
            if len(tickers) >= 4:
                shares = int(tickers[3].replace(",", ""))
                if shares <= 0:
                    raise ValueError
        except ValueError:
            await update.message.reply_text(
                "รูปแบบ: <code>ซื้อ AOT 32.50</code> หรือ "
                "<code>ซื้อ AOT 32.50 300</code> (ราคา แล้วค่อยจำนวนหุ้น)",
                parse_mode="HTML",
            )
            return
        await update.message.reply_text("⏳ กำลังบันทึกพร้อมเก็บบริบทระบบ...")
        result = await asyncio.to_thread(
            record_buy, update.effective_chat.id, tickers[1], price, shares
        )
        await update.message.reply_text(result, parse_mode="HTML")
        return

    if tickers and tickers[0].lower() in ("ขาย", "sell"):
        if len(tickers) < 3:
            await update.message.reply_text(
                "พิมพ์ <code>ขาย AOT 35.00</code>", parse_mode="HTML"
            )
            return
        try:
            price = float(tickers[2].replace(",", ""))
            if price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "รูปแบบ: <code>ขาย AOT 35.00</code>", parse_mode="HTML"
            )
            return
        result = await asyncio.to_thread(
            record_sell, update.effective_chat.id, tickers[1], price
        )
        await update.message.reply_text(result, parse_mode="HTML")
        return

    if len(tickers) == 1 and tickers[0].lower() in ("เทรด", "trades"):
        await update.message.reply_text("⏳ กำลังสรุปไม้...")
        result = await asyncio.to_thread(
            build_trades_report, update.effective_chat.id
        )
        await _reply_long(update.message, result, parse_mode="HTML")
        return

    # ตั้ง/ดูขนาดพอร์ต: "พอร์ต 500000" / "พอร์ต"
    if tickers and tickers[0].lower() in ("พอร์ต", "port"):
        chat_id = update.effective_chat.id
        if len(tickers) == 1:
            port = get_port_size(chat_id)
            if port:
                await update.message.reply_text(
                    f"💼 พอร์ตปัจจุบัน {port:,.0f} บาท — "
                    f"เสี่ยง {RISK_PCT_PER_TRADE:.0f}%/ไม้ "
                    f"= {port * RISK_PCT_PER_TRADE / 100:,.0f} บ.\n"
                    "เปลี่ยน: <code>พอร์ต 600000</code>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    "ยังไม่ได้ตั้งขนาดพอร์ต — พิมพ์ <code>พอร์ต 500000</code>",
                    parse_mode="HTML",
                )
            return
        try:
            value = float(tickers[1].replace(",", ""))
            if value <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "ใส่ตัวเลขบาท เช่น <code>พอร์ต 500000</code>", parse_mode="HTML"
            )
            return
        set_port_size(chat_id, value)
        await update.message.reply_text(
            f"✅ ตั้งพอร์ต {value:,.0f} บาทแล้ว — "
            f"เสี่ยง {RISK_PCT_PER_TRADE:.0f}%/ไม้ "
            f"= {value * RISK_PCT_PER_TRADE / 100:,.0f} บ.\n"
            f"คำนวณขนาดไม้: <code>ไม้ AOT</code>",
            parse_mode="HTML",
        )
        return

    # คำนวณขนาดไม้: "ไม้ AOT" (เข้าราคาตอนนี้) / "ไม้ AOT 64.50" (ระบุราคาเข้า)
    if tickers and tickers[0].lower() in ("ไม้", "size"):
        if len(tickers) < 2:
            await update.message.reply_text(
                "พิมพ์ <code>ไม้ AOT</code> (เข้าราคาตอนนี้) หรือ "
                "<code>ไม้ AOT 64.50</code> (ระบุราคาเข้าเอง)",
                parse_mode="HTML",
            )
            return
        entry = None
        if len(tickers) >= 3:
            try:
                entry = float(tickers[2].replace(",", ""))
                if entry <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "ราคาเข้าต้องเป็นตัวเลข เช่น <code>ไม้ AOT 64.50</code>",
                    parse_mode="HTML",
                )
                return
        await update.message.reply_text("⏳ กำลังคำนวณขนาดไม้...")
        result = await asyncio.to_thread(
            build_position_size, update.effective_chat.id, tickers[1], entry
        )
        await update.message.reply_text(result, parse_mode="HTML")
        return

    # watchlist: "ติดตาม AOT" / "เลิกติดตาม AOT" / "ลิสต์"
    if tickers and tickers[0].lower() in ("ติดตาม", "watch"):
        if len(tickers) < 2:
            await update.message.reply_text("พิมพ์ <code>ติดตาม AOT</code>", parse_mode="HTML")
            return
        added = []
        for t in tickers[1:]:
            base, _ = stock_core.add_to_watchlist(t, update.effective_chat.id)
            added.append(base)
        await update.message.reply_text(
            f"✅ เพิ่มเข้าลิสต์แล้ว: <b>{html.escape(' '.join(added))}</b>\n"
            "ดูทั้งหมด: <code>ลิสต์</code>",
            parse_mode="HTML",
        )
        return

    if tickers and tickers[0].lower() in ("เลิกติดตาม", "ถอน", "unwatch"):
        if len(tickers) < 2:
            await update.message.reply_text(
                "พิมพ์ <code>เลิกติดตาม AOT</code> "
                "(หลายตัวพร้อมกันได้: <code>เลิกติดตาม AOT PTT</code>)",
                parse_mode="HTML",
            )
            return
        removed, not_found = [], []
        remaining = stock_core.get_watchlist(update.effective_chat.id)
        for t in tickers[1:]:
            base, remaining = stock_core.remove_from_watchlist(
                t, update.effective_chat.id
            )
            if base:
                removed.append(base)
            else:
                not_found.append(t.upper())
        lines = []
        if removed:
            lines.append(
                f"🗑 เอาออกจากลิสต์แล้ว: <b>{html.escape(' '.join(removed))}</b> "
                f"(เหลือ {len(remaining)} ตัว)"
            )
        if not_found:
            lines.append(f"ไม่พบในลิสต์: {html.escape(' '.join(not_found))}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if len(tickers) == 1 and tickers[0].lower() in ("ลิสต์", "list", "watchlist"):
        await update.message.reply_text("⏳ กำลังสรุปลิสต์...")
        result = await asyncio.to_thread(
            build_watchlist_summary, update.effective_chat.id
        )
        await _reply_long(update.message, result, parse_mode="HTML")
        return

    if len(tickers) > MAX_TICKERS:
        await update.message.reply_text(f"⚠️ พิมพ์ได้สูงสุด {MAX_TICKERS} ตัวต่อครั้งนะครับ")
        return

    if len(tickers) > 1:
        await update.message.reply_text(f"⏳ กำลังดึงข้อมูล {len(tickers)} ตัว...")
    else:
        await update.message.reply_text("⏳ กำลังดึงข้อมูล...")

    for i, t in enumerate(tickers):
        # ดึงข้อมูลใน thread แยกเพื่อไม่บล็อกบอท และเว้นจังหวะเล็กน้อย
        # ระหว่างตัวเพื่อลดโอกาสโดน Yahoo rate limit
        result = await asyncio.to_thread(build_message, t)
        await update.message.reply_text(result, parse_mode="HTML")
        if i < len(tickers) - 1:
            await asyncio.sleep(0.5)


# main

def main():
    if not BOT_TOKEN:
        print("\n  ⚠️  ไม่พบ BOT_TOKEN — กรุณาใส่ในไฟล์ .env (บรรทัด BOT_TOKEN=...)\n")
        return

    print("  Bot กำลังทำงาน... (Ctrl+C เพื่อหยุด)")
    print(f"  สแกนอัตโนมัติทุกวันทำการ เวลา {SCAN_HOUR:02d}:{SCAN_MINUTE:02d} น.")
    print(f"  เตือนวันงบทุกวันทำการ เวลา {REMIND_HOUR:02d}:{REMIND_MINUTE:02d} น.")
    print(f"  เช็คลิสต์ติดตามทุก {MONITOR_INTERVAL_MIN} นาที ช่วงตลาดเปิด")
    print(f"  เฝ้าข่าวแจ้งงบ (เว็บ SET) ทุก {NEWS_POLL_MINUTES} นาที "
          "ช่วง 07:00-09:45 และ 17:00-21:45")
    print(f"  สรุปงบเช้าทุกวันทำการ เวลา {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} น. "
          "(หรือส่งตอนเปิดบอทถ้ายังไม่ได้ส่ง)")
    print(f"  ยืนยันรอบเช้าทุกวันทำการ เวลา {CONFIRM_HOUR:02d}:{CONFIRM_MINUTE:02d} น. "
          "(เฉพาะวันที่มีหุ้นงบโตแรง)\n")
    log.info("bot starting")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.job_queue.run_daily(
        daily_scan_job,
        time=datetime.time(SCAN_HOUR, SCAN_MINUTE, tzinfo=ZoneInfo("Asia/Bangkok")),
    )
    app.job_queue.run_daily(
        earnings_reminder_job,
        time=datetime.time(REMIND_HOUR, REMIND_MINUTE, tzinfo=ZoneInfo("Asia/Bangkok")),
    )
    app.job_queue.run_repeating(
        watchlist_monitor_job,
        interval=MONITOR_INTERVAL_MIN * 60,
        first=60,
    )
    app.job_queue.run_repeating(
        news_monitor_job,
        interval=NEWS_POLL_MINUTES * 60,
        first=120,
    )
    app.job_queue.run_daily(
        heartbeat_job,
        time=datetime.time(HEART_HOUR, HEART_MINUTE, tzinfo=ZoneInfo("Asia/Bangkok")),
    )
    app.job_queue.run_daily(
        morning_digest_job,
        time=datetime.time(DIGEST_HOUR, DIGEST_MINUTE, tzinfo=ZoneInfo("Asia/Bangkok")),
    )
    app.job_queue.run_daily(
        morning_confirm_job,
        time=datetime.time(CONFIRM_HOUR, CONFIRM_MINUTE,
                           tzinfo=ZoneInfo("Asia/Bangkok")),
    )
    # จดเวลาว่ายังทำงานทุก 5 นาที + เก็บตกข่าวช่วงที่ปิดไป (ครั้งเดียวตอนเปิด)
    app.job_queue.run_repeating(alive_job, interval=300, first=10)
    app.job_queue.run_once(startup_catchup_job, when=20)
    # startup digest หน่วง 300 วิ ให้ catch-up (วินาที 20) + news poll รอบแรก
    # (วินาที 120) ทำงานจบก่อน ข้อมูลเช้านี้จะได้อยู่ใน CSV แล้ว
    app.job_queue.run_once(startup_digest_job, when=300)
    # ยืนยันรอบเช้าตามหลัง digest (เผื่อเปิดคอมหลัง 10:30 — เงื่อนไขเวลาอยู่ใน job)
    app.job_queue.run_once(startup_confirm_job, when=330)
    # ย้าย watchlist.json format เดิม (list) → per-user dict ให้เจ้าของ (ครั้งเดียว)
    stock_core.migrate_legacy_watchlist(_load_chat_ids())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
