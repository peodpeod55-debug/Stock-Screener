import os
import re
import html
import json
import time
import asyncio
import datetime
import logging
import urllib.parse
from logging.handlers import RotatingFileHandler

from zoneinfo import ZoneInfo

from yfinance.exceptions import YFRateLimitError
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import stats
import shadow
import scanner
import set_news
import stock_core
import ta_prompt
import dashboard_feed
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

# ── คำสั่ง "วิเคราะห์" (ข้อมูล PHASE 0 สำหรับ Gem เทคนิค) — อ่าน payload ของ Trading_Dashboard ──
# ทั้งสามค่าตั้งใน .env (ดู .env.example) · GEM_TA_URL ผูกกับบัญชี Google รายคน ห้าม hardcode ลงโค้ด
DASHBOARD_SITE_DIR = scanner._env_value("DASHBOARD_SITE_DIR")           # โฟลเดอร์ site ในเครื่อง (ว่าง = ใช้เว็บ)
DASHBOARD_URL = scanner._env_value("DASHBOARD_URL", dashboard_feed.DEFAULT_URL)   # dashboard ที่ deploy (ต้นทางสำรอง)
GEM_TA_URL = scanner._env_value("GEM_TA_URL", "https://gemini.google.com/gems/view")
EARNINGS_RADAR_URL = "https://earningsradar.pages.dev/company/{sym}/"   # หน้างบรายบริษัท (เหมือนปุ่ม 📅 บน dashboard)

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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """error handler กลางของ PTB — เดิมไม่มีเลย ทำให้ (1) คำสั่งที่พังกลางทาง
    ผู้ใช้ค้างที่ "⏳..." เงียบๆ (2) log เต็มไปด้วย "No error handlers are
    registered" พร้อม traceback ยาวจากเน็ตสะดุดชั่วคราว"""
    err = getattr(context, "error", None)
    # เน็ตสะดุดระหว่าง polling เกิดเป็นระยะอยู่แล้ว — log สั้นๆ พอ ไม่ต้องเต็ม traceback
    if isinstance(err, NetworkError) and not isinstance(update, Update):
        log.warning("network hiccup: %s", err)
        return
    log.error("unhandled error", exc_info=err)
    msg = getattr(update, "effective_message", None)
    if msg is not None:
        try:
            await msg.reply_text("⚠️ เกิดข้อผิดพลาดระหว่างประมวลผล "
                                 "ลองใหม่อีกครั้งนะครับ")
        except Exception:
            pass


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
    # ยาวใกล้เพดาน 3,900 แล้ว → ส่งผ่าน _reply_long (ตัดแบ่งตามบรรทัดให้เองถ้าเกิน)
    await _reply_long(
        update.message,
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
        "• <code>งบ AOT 13/11/2569</code> — บันทึกวันงบเอง\n"
        "• <code>ปฏิทิน</code> — ใครงบออกใน 14 วันข้างหน้า "
        "(บอทส่งเองสัปดาห์ละครั้ง เย็นอาทิตย์)\n\n"
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
        "• มีไม้เปิด → บอทรายงาน R / ระยะถึง ⛔ / วันเงียบ ให้เองทุก 17:45\n"
        "• <code>สถิติ</code> เทียบผลไม้จริงกับผลระบบให้อัตโนมัติ\n\n"
        "<b>ไม้เงา (ระบบเทรดกระดาษเอง)</b>\n"
        "• <code>เงา</code> — ทุกตัวที่ติดสแกนถูกจำลองเป็นไม้: เข้าเปิด\n"
        "  วันถัดไป ตัดหลุด ⛔ ถือสูงสุด 20 วัน — พิสูจน์ว่าคะแนน\n"
        "  ยังทำนายได้จริงในตลาดตอนนี้ โดยไม่ใช้เงินจริง\n\n"
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
        "<b>📐 วิเคราะห์เทคนิคต่อด้วย Gem</b>\n"
        "• <code>วิเคราะห์ AOT</code> — ข้อมูล PHASE 0 (ราคา/SMA/RSI/MACD/ATR/RS/วันงบ)\n"
        "  จากระบบ dashboard เป็นบล็อกกดคัดลอกได้ + ปุ่มเปิด Gem เทคนิค\n"
        "  → วางในแชท Gem พร้อมรูป Weekly + Daily แล้ว Gem วิเคราะห์เลย ไม่ถามกลับ\n"
        "  (หรือกดปุ่ม 📐 ใต้ผลสแกน / สรุปงบ / ยืนยัน / ลิสต์)\n"
        "• <code>คำอธิบายงบ AOT</code> — ลิงก์หน้างบของหุ้นตัวนั้นที่ Earnings Radar (ปุ่ม 📅)\n\n"
        "ทุกคำสั่งพิมพ์เป็นอังกฤษได้ (ตัวเล็ก-ใหญ่ไม่สำคัญ):\n"
        "<code>scan</code> / <code>news</code> / <code>digest</code> / "
        "<code>confirm</code> / <code>stats</code> / <code>earn AOT</code> / "
        "<code>calendar</code> / <code>shadow</code> / "
        "<code>watch AOT</code> / <code>unwatch AOT</code> / <code>list</code> / "
        "<code>port 500000</code> / <code>size AOT</code> / "
        "<code>buy AOT 32.50</code> / <code>sell AOT 35</code> / <code>trades</code> / "
        "<code>ta AOT</code> / <code>mda AOT</code>\n\n"
        "หรือกดปุ่มเมนู ☰ (พิมพ์ <code>/</code>) — ทุกคำสั่งอยู่ในเมนูพร้อม"
        "คำอธิบาย เช่น <code>/scan</code> <code>/watch AOT</code> <code>/list</code>",
        parse_mode="HTML",
    )


MAX_TICKERS = 10

# ── ผู้รับผลสแกนอัตโนมัติ (จำ chat ทุกคนที่เคยคุยกับบอท) ────────

_CHAT_IDS_PATH = os.path.join(_BASE_DIR, "chat_ids.json")


def _load_chat_ids():
    # ไฟล์เสีย = ทุก job เงียบทั้งบอท (early-return หมด) — เก็บ .bak ไว้กู้
    ids = stock_core.load_json_or_backup(_CHAT_IDS_PATH, [])
    return ids if isinstance(ids, list) else []


def _register_chat(chat_id: int):
    ids = _load_chat_ids()
    if chat_id not in ids:
        ids.append(chat_id)
        try:
            stock_core.save_json_atomic(_CHAT_IDS_PATH, ids)
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
        stock_core.save_json_atomic(
            _ALIVE_PATH,
            {"ts": datetime.datetime.now(ZoneInfo("Asia/Bangkok")).isoformat()})
    except Exception:
        pass


def _catchup_days(news_age_h):
    """ควรเก็บตกข่าวย้อนกี่วัน — None ถ้าไม่ต้อง (ข้อมูลข่าวยังสดพอ)

    ตัดสินจากอายุข้อมูลข่าว (news_seen.json) โดยตรง — เดิมดูจาก last_alive
    แต่โดน alive_job เขียนทับก่อน catch-up อ่าน (heartbeat รันวินาทีที่ 10
    catch-up รันวินาทีที่ 20) ทำให้ catch-up ไม่เคยทำงานจริง อีกทั้งสิ่งที่
    catch-up ต้องเติมคือ "ข่าวที่ขาด" อายุข่าวจึงเป็นตัววัดที่ตรงกว่า
    (โบนัส: รอบก่อน catch-up ล้มเหลว รีสตาร์ทใหม่จะลองซ้ำให้เอง)
    """
    if news_age_h is None:
        return None  # เพิ่งใช้ครั้งแรก ยังไม่มีฐานข่าวให้เติม
    if news_age_h < CATCHUP_MIN_GAP_HOURS:
        return None
    return min(CATCHUP_MAX_DAYS, int(news_age_h // 24) + 1)


async def alive_job(context: ContextTypes.DEFAULT_TYPE):
    _write_alive()


async def startup_catchup_job(context: ContextTypes.DEFAULT_TYPE):
    """รันครั้งเดียวตอนบอทเปิด: ถ้าหายไปนาน ให้ดึงข่าวย้อนช่วงที่หาย
    มาบันทึกวันงบ/ตัวเลข F45 ให้ครบ (ข่าวเก่าบันทึกเงียบๆ ไม่สแปม)"""
    days = _catchup_days(set_news.news_data_age_hours())
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


async def _run_heartbeat(context: ContextTypes.DEFAULT_TYPE):
    """ส่ง "✅ บอททำงานปกติ" ครั้งเดียวต่อวันทำการ (key ใน digest_state)

    เดิมมีแต่ job 08:30 ซึ่งเครื่องผู้ใช้บูต ~09:30 จึงแทบไม่เคยยิง —
    วันปกติเลยไม่มีข้อความทั้งที่บอทดีๆ ทำสัญญาณ "ไม่เห็น ✅ = บอทตาย"
    กลับด้านพอดี → เพิ่มทางยิงตอนเปิดบอท: เปิดเครื่องแล้วต้องเห็น ✅ เสมอ"""
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5:
        return
    if _load_digest_state().get("alive_last_sent") == now.date().isoformat():
        return
    sent_any = False
    for cid in _load_chat_ids():
        n_watch = len(stock_core.get_watchlist(cid))
        text = (f"✅ บอททำงานปกติ • ลิสต์ติดตาม {n_watch} ตัว • "
                f"สแกนอัตโนมัติ {SCAN_HOUR:02d}:{SCAN_MINUTE:02d} น.")
        try:
            await context.bot.send_message(cid, text)
            sent_any = True
        except Exception:
            pass
    if sent_any:
        state = _load_digest_state()
        state["alive_last_sent"] = now.date().isoformat()
        _save_digest_state(state)


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    """job รายวัน 08:30 (ยิงจริงเฉพาะวันที่เครื่องเปิดอยู่ก่อนเวลานั้น)"""
    await _run_heartbeat(context)


async def startup_heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    """ตอนเปิดบอท: วันทำการไหนยังไม่ได้ส่ง ✅ ส่งทันที (เครื่องบูตสาย/รีสตาร์ท)"""
    await _run_heartbeat(context)


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
    # อุดช่องที่ขาด: ถ้าดึงไม่สำเร็จมานาน (เว็บล่ม/เน็ตหลุดข้ามวัน) ให้ดึง
    # ย้อนคลุมช่วงที่หายไปด้วย ไม่ใช่แค่วันเดียว — สแกนโหมดข่าวแจ้งงบ
    # พึ่งความครบของ filings_log.csv จึงห้ามมีรูช่วงที่บอทเปิดอยู่
    age_h = set_news.news_data_age_hours() or 0
    days_back = min(7, max(1, int(age_h // 24) + 1))
    try:
        hits = await asyncio.to_thread(
            set_news.check_new_earnings_news, 14, days_back)
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
        stock_core.save_json_atomic(_DIGEST_STATE_PATH, state)
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
        sent_any = False
        for cid in chat_ids:
            try:
                await _send_long(context.bot, cid, text,
                                 reply_markup=buttons, parse_mode="HTML")
                sent_any = True
            except Exception:
                log.exception("send %s failed (chat %s)", label, cid)
        # บันทึก "ส่งแล้ว" เฉพาะเมื่อส่งถึงจริงอย่างน้อยหนึ่ง chat —
        # ไม่งั้นเน็ตล่มตอน 08:55 = วันนั้นถูกนับว่าส่งแล้วทั้งที่ไม่มีใครได้
        # และ startup fallback จะไม่ยอมส่งซ้ำ
        if sent_any:
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
        sent_any = False
        for cid in chat_ids:
            try:
                await _send_long(context.bot, cid, text,
                                 reply_markup=buttons, parse_mode="HTML")
                sent_any = True
            except Exception:
                log.exception("send %s failed (chat %s)", label, cid)
        # ส่งถึงจริงอย่างน้อยหนึ่ง chat ค่อยนับว่าส่งแล้ว (เหตุผลเดียวกับ digest)
        if sent_any:
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
        stock_core.save_json_atomic(_WATCH_STATE_PATH, state)
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
            sym_h = html.escape(sym)  # กันอักขระแปลกในลิสต์เก่าพังทั้งข้อความรวม
            if prev.get("above_pre_low", True) and not cur["above_pre_low"]:
                msgs.append(
                    f"⛔ <b>{sym_h}</b> หลุด Low ก่อนงบ "
                    f"(<code>{s['pre_earn_low']:.2f}</code>) — สัญญาณเสีย\n{price_txt}"
                )
            if not prev.get("broke_pre3m_high") and cur["broke_pre3m_high"]:
                msgs.append(
                    f"🔥 <b>{sym_h}</b> ทะลุไฮ 3 เดือนก่อนงบ "
                    f"(<code>{s['pre3m_high']:.2f}</code>) แล้ว\n{price_txt}"
                )
            elif not prev.get("broke_pre5d_high") and cur["broke_pre5d_high"]:
                msgs.append(
                    f"✅ <b>{sym_h}</b> ผ่านไฮ 5 วันก่อนงบ "
                    f"(<code>{s['pre5d_high']:.2f}</code>) แล้ว\n{price_txt}"
                )
            if (not msgs and s["days_since_new_high"] == 0
                    and prev.get("new_high_date") != today):
                msgs.append(f"📈 <b>{sym_h}</b> ทำไฮใหม่หลังงบวันนี้\n{price_txt}")
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
            soon.append(f"• <b>{html.escape(sym)}</b> งบออก{when} "
                        f"({d['next_earnings']:%d/%m/%Y})")
    if not soon:
        return None
    return "🗓 <b>เตือนวันประกาศงบ (หุ้นในลิสต์)</b>\n" + "\n".join(soon)


# กันส่งซ้ำแบบเดียวกับสรุปงบเช้า: job 08:45 กับ startup fallback
# ใช้ digest_state.json ร่วมกัน (key "remind_last_sent") + ธง in-flight
_remind_in_flight = False


async def _run_earnings_reminder(context: ContextTypes.DEFAULT_TYPE, label: str):
    """ตัวส่งเตือนวันงบกลาง: ส่งครั้งเดียวต่อวัน — ไม่มีตัวงบใกล้ = ไม่ส่ง
    ไม่บันทึกสถานะ (ให้ trigger ถัดไปของวันลองใหม่ได้ ไม่มีผลข้างเคียง)"""
    global _remind_in_flight
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5:
        return
    if _remind_in_flight:
        return
    if _load_digest_state().get("remind_last_sent") == now.date().isoformat():
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    _remind_in_flight = True
    try:
        sent_any = False
        for cid in chat_ids:
            try:
                text = await asyncio.to_thread(build_earnings_reminder, cid)
            except Exception:
                log.exception("%s failed (chat %s)", label, cid)
                continue
            if not text:
                continue
            try:
                await context.bot.send_message(cid, text, parse_mode="HTML")
                sent_any = True
            except Exception:
                log.exception("send %s failed (chat %s)", label, cid)
        if sent_any:
            state = _load_digest_state()
            state["remind_last_sent"] = now.date().isoformat()
            _save_digest_state(state)
    finally:
        _remind_in_flight = False


async def earnings_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """job รายวัน 08:45: เตือนหุ้นในลิสต์ที่งบออกวันนี้/พรุ่งนี้"""
    await _run_earnings_reminder(context, "earnings reminder")


async def startup_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """ตอนเปิดบอท: ถ้าวันนี้ยังไม่ได้ส่งเตือนวันงบ และยังไม่สายเกิน 16:30
    → ส่งเลย (รองรับเปิดคอมสาย ~09:30 เกินเวลา job 08:45 ไปแล้ว)"""
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.time() >= datetime.time(16, 30):
        return
    await _run_earnings_reminder(context, "startup earnings reminder")


# ── ปฏิทินงบล่วงหน้า (อาทิตย์ 19:00 หรือเรียกเองด้วย "ปฏิทิน") ──
# มองไปข้างหน้า 14 วัน — เติมช่องว่างของเตือน 08:45 ที่เห็นแค่วันนี้/พรุ่งนี้
# ให้วางแผนก่อนฤดูงบได้ทั้งสัปดาห์

CALENDAR_HOUR, CALENDAR_MINUTE = 19, 0   # เวลาส่งปฏิทิน (เย็นวันอาทิตย์)
CALENDAR_DAYS_AHEAD = 14                 # มองล่วงหน้ากี่วัน


def build_earnings_calendar(chat_id=None) -> str:
    """ปฏิทินงบจากคลังวันงบในเครื่อง (ไม่ยิงเครือข่าย — ตอบได้ทันที)
    คืน "" ถ้าไม่รู้วันงบของตัวไหนเลยในช่วงนั้น"""
    upcoming = stock_core.upcoming_earnings(CALENDAR_DAYS_AHEAD)
    if not upcoming:
        return ""
    watch = set(stock_core.get_watchlist(chat_id)) if chat_id is not None else set()
    today = datetime.date.today()
    by_day = {}
    for sym, d in upcoming:
        star = " ⭐" if sym in watch else ""
        by_day.setdefault(d, []).append(html.escape(sym) + star)
    lines = [f"📅 <b>ปฏิทินงบ {CALENDAR_DAYS_AHEAD} วันข้างหน้า</b> "
             f"({len(upcoming)} ตัวที่รู้วันงบ)"]
    for d, syms in sorted(by_day.items()):
        rel = (d - today).days
        rel_txt = "วันนี้" if rel == 0 else ("พรุ่งนี้" if rel == 1 else f"อีก {rel} วัน")
        lines.append(f"• {_THAI_WEEKDAY[d.weekday()]} {d:%d/%m} ({rel_txt}): "
                     + ", ".join(syms))
    lines.append("")
    lines.append("⭐ = อยู่ในลิสต์ติดตาม • คลังวันงบมีเฉพาะตัวที่บอทเคยเห็น "
                 "(ข่าว SET / เคยกดดู / บันทึกเอง) อาจไม่ครบทุกบริษัท")
    lines.append("บันทึกวันงบเพิ่ม: <code>งบ XXXX 14/08/2569</code>")
    return "\n".join(lines)


# กันส่งซ้ำรายสัปดาห์: anchor = วันอาทิตย์ล่าสุด — ส่งอาทิตย์ 19:00 ถ้าคอมเปิด
# คอมปิดทั้งวันอาทิตย์ก็ไม่พลาด: job 19:00 วันถัดๆ ไป / startup fallback ส่งชดเชย
# (เทียบ anchor เดียวกันจึงไม่ส่งซ้ำถ้าสัปดาห์นี้ส่งไปแล้ว)
_calendar_in_flight = False


def _last_sunday(day: datetime.date) -> datetime.date:
    return day - datetime.timedelta(days=(day.weekday() + 1) % 7)


async def _run_calendar(context: ContextTypes.DEFAULT_TYPE, label: str):
    """ตัวส่งปฏิทินงบกลาง: สัปดาห์ละครั้ง (นับสัปดาห์แบบ อา-ส)
    ไม่มีตัวไหนรู้วันงบ = ไม่ส่ง ไม่บันทึกสถานะ (trigger ถัดไปลองใหม่ได้)"""
    global _calendar_in_flight
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    anchor = _last_sunday(now.date()).isoformat()
    if _calendar_in_flight:
        return
    if _load_digest_state().get("calendar_last_sent") == anchor:
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    _calendar_in_flight = True
    try:
        sent_any = False
        for cid in chat_ids:
            text = build_earnings_calendar(cid)
            if not text:
                continue
            try:
                await _send_long(context.bot, cid, text, parse_mode="HTML")
                sent_any = True
            except Exception:
                log.exception("send %s failed (chat %s)", label, cid)
        if sent_any:
            state = _load_digest_state()
            state["calendar_last_sent"] = anchor
            _save_digest_state(state)
    finally:
        _calendar_in_flight = False


async def calendar_job(context: ContextTypes.DEFAULT_TYPE):
    """job รายวัน 19:00 — anchor รายสัปดาห์ทำให้ส่งจริงแค่ครั้งแรกของสัปดาห์
    ที่คอมเปิดตอน 19:00 (ปกติคือเย็นวันอาทิตย์)"""
    await _run_calendar(context, "earnings calendar")


async def startup_calendar_job(context: ContextTypes.DEFAULT_TYPE):
    """ตอนเปิดบอท: สัปดาห์นี้ยังไม่เคยส่งปฏิทิน → ส่งเลย (เปิดคอมวันไหนก็ได้)"""
    await _run_calendar(context, "startup earnings calendar")


# ── ปุ่มใต้ผลสแกน/สรุปงบ/ยืนยัน: หุ้นละแถว [➕ ติดตาม] [📐 PHASE 0] [📅 หน้างบ] ─────
# รับได้ทั้ง dict ผลสแกน ({"ticker": ...}) และชื่อหุ้นเปล่าๆ (จากสรุปงบ)
# 📐 = callback ta:SYM (คำสั่ง "วิเคราะห์" ตัวเดียวกัน) · 📅 = ลิงก์ Earnings Radar ไม่มี callback

def _earnings_url(sym: str) -> str:
    return EARNINGS_RADAR_URL.format(sym=urllib.parse.quote(sym))


def _stock_row(base: str):
    return [
        InlineKeyboardButton(f"➕ {base}", callback_data=f"watch:{base}"),
        InlineKeyboardButton(f"📐 {base}", callback_data=f"ta:{base}"),
        InlineKeyboardButton(f"📅 {base}", url=_earnings_url(base)),
    ]


def _watch_buttons(hits, limit=15):
    if not hits:
        return None
    rows = []
    for d in hits[:limit]:
        base = (d["ticker"] if isinstance(d, dict) else d).replace(".BK", "")
        rows.append(_stock_row(base))
    return InlineKeyboardMarkup(rows)


def _rows_of(symbols, limit, make):
    """จัดปุ่ม 3 ปุ่มต่อแถว → list ของแถว (ไม่ห่อ InlineKeyboardMarkup ให้เอาไปต่อกันได้)"""
    rows, row = [], []
    for s in (symbols or [])[:limit]:
        row.append(make(s))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _unwatch_rows(symbols, limit=15):
    """แถวปุ่ม 🗑 เอาออกจากลิสต์ (ใต้สรุป "ลิสต์" — เฉพาะตัวที่หมดสภาพ)"""
    return _rows_of(symbols, limit,
                    lambda s: InlineKeyboardButton(f"🗑 {s}", callback_data=f"unwatch:{s}"))


def _ta_rows(symbols, limit=15):
    """แถวปุ่ม 📐 ข้อมูล PHASE 0 ของหุ้นในลิสต์ (กดต่อได้โดยไม่ต้องพิมพ์ชื่อ)"""
    return _rows_of(symbols, limit,
                    lambda s: InlineKeyboardButton(f"📐 {s}", callback_data=f"ta:{s}"))


def _list_buttons(stale, symbols, limit=15):
    """คีย์บอร์ดใต้ "ลิสต์": 🗑 ของตัวหมดสภาพ ต่อด้วย 📐 ของทุกตัวในลิสต์ (15 ตัวแรก)"""
    rows = _unwatch_rows(stale, limit) + _ta_rows(symbols, limit)
    return InlineKeyboardMarkup(rows) if rows else None


# ── คำสั่ง "วิเคราะห์ XXX" / ปุ่ม 📐 → ข้อความ PHASE 0 สำหรับ Gem เทคนิค ─────
# อ่าน payload ของ Trading_Dashboard (dashboard_feed) → ประกอบข้อความเท่ากับปุ่ม 📐 บนเว็บ (ta_prompt)
# ส่งเป็น <pre> ให้กดคัดลอกทั้งก้อนได้ + ปุ่มลิงก์เปิด Gem — บอทไม่วิเคราะห์เอง ผู้ใช้เอาไปวางใน Gem พร้อมรูป

_TA_USAGE = (
    "📐 <b>วิเคราะห์เทคนิคต่อด้วย Gem</b>\n"
    "พิมพ์ <code>วิเคราะห์ AOT</code> (หรือ <code>ta AOT</code>) — บอทส่งข้อมูล PHASE 0 ของหุ้นตัวนั้น\n"
    "เป็นบล็อกกดคัดลอกได้ แล้วเปิด Gem จากปุ่ม → วางพร้อมรูป Weekly + Daily\n"
    "หรือกดปุ่ม 📐 ใต้ผลสแกน / สรุปงบ / ยืนยัน / ลิสต์ ได้เลยไม่ต้องพิมพ์ชื่อ"
)
_MDA_USAGE = (
    "📅 พิมพ์ <code>คำอธิบายงบ AOT</code> (หรือ <code>mda AOT</code>) — "
    "ลิงก์หน้างบ/คำอธิบายผลประกอบการของหุ้นตัวนั้นที่ Earnings Radar"
)
_SYMBOL_RE = re.compile(r"[A-Z0-9.&-]{1,15}")


async def _ta_reply(message, raw_symbol: str):
    """ตอบข้อความ PHASE 0 ของหุ้นหนึ่งตัว (ใช้ร่วมระหว่างคำสั่ง "วิเคราะห์" และ callback ta:SYM)"""
    sym = stock_core._base_symbol(raw_symbol or "")
    if not sym or not _SYMBOL_RE.fullmatch(sym):
        await message.reply_text(_TA_USAGE, parse_mode="HTML")
        return
    try:
        stock, meta = await asyncio.to_thread(
            dashboard_feed.load_stock, sym, "TH",
            site_dir=DASHBOARD_SITE_DIR, url=DASHBOARD_URL,
            max_age_days=scanner.LOCAL_MAX_AGE_DAYS,
        )
    except dashboard_feed.DashboardUnavailable as e:
        await message.reply_text(
            f"⚠️ {html.escape(str(e))}\n"
            "ตรวจ DASHBOARD_SITE_DIR / DASHBOARD_URL ใน .env หรือรอ dashboard สร้างข้อมูลรอบใหม่",
            parse_mode="HTML",
        )
        return
    asof = meta.get("asof") or meta.get("core_asof") or "ไม่ทราบวันที่"
    if stock is None:
        await message.reply_text(
            f"ไม่มี <b>{html.escape(sym)}</b> ในระบบ dashboard (ข้อมูล build {html.escape(str(asof))}) — "
            "หุ้นนอก universe ของ screener ต้องหาข้อมูล PHASE 0 เอง",
            parse_mode="HTML",
        )
        return
    body = ta_prompt.build_ta_prompt(stock, meta, stock_core.next_earnings_date(sym))
    session = "ระหว่างวัน" if meta.get("intraday") else "ปิดตลาดแล้ว"
    lines = [f"📐 ข้อมูล PHASE 0 · <b>{html.escape(sym)}</b> (ข้อมูล {html.escape(str(asof))} · {session})"]
    if meta.get("stale"):
        lines.append(f"⚠️ ข้อมูลเก่า {meta.get('age_days')} วัน — dashboard ยังไม่ได้อัปเดต "
                     "ตัดสินเองว่ายังใช้ได้ไหม (swing ใช้ EOD เก่า 1-2 วันยังพอได้)")
    lines.append("กดค้างที่บล็อกด้านล่างเพื่อคัดลอก แล้ววางในแชท Gem พร้อมรูป Weekly + Daily")
    text = "\n".join(lines) + f"\n\n<pre>{html.escape(body)}</pre>"
    # ส่งตรง ไม่ผ่าน _reply_long — ตัวนั้นตัดตามบรรทัดจะผ่ากลาง <pre> แล้ว HTML พัง
    # (ความยาวถูกจำกัดด้วยโครงเทมเพลต ≈1,400 ตัวอักษร มีเทสต์ยืนยันขอบบน)
    await message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📐 เปิด Gem เทคนิค", url=GEM_TA_URL)]]),
    )


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
    elif data.startswith("unwatch:"):
        base, symbols = stock_core.remove_from_watchlist(
            data.split(":", 1)[1], update.effective_chat.id
        )
        if base:
            await q.answer(f"เอา {base} ออกแล้ว (เหลือ {len(symbols)} ตัว)")
            try:
                await q.message.reply_text(
                    f"🗑 เอา <b>{html.escape(base)}</b> ออกจากลิสต์แล้ว "
                    f"(เหลือ {len(symbols)} ตัว)",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        else:
            await q.answer("ตัวนี้ไม่อยู่ในลิสต์แล้ว (กดซ้ำ?)")
    elif data.startswith("ta:"):
        # ตอบ callback ก่อนทำงาน (Telegram หมดอายุปุ่มใน ~15 วิ) แล้วค่อยส่งบล็อก PHASE 0
        # เป็นข้อความใหม่ — handler เดียวกับคำสั่ง "วิเคราะห์"
        await q.answer("⏳ กำลังดึงข้อมูล PHASE 0...")
        try:
            await _ta_reply(q.message, data.split(":", 1)[1])
        except Exception:
            log.exception("ta callback failed")
    else:
        await q.answer()


def run_best_scan():
    """เลือกโหมดสแกนที่ดีที่สุดที่ใช้ได้ตอนนี้ (ลำดับ: ข่าวแจ้งงบ →
    ทั้งตลาดจาก Trading_Dashboard → รายชื่อหลัก 94 ตัว)

    โหมดหลักคือข่าวแจ้งงบ: candidate มาจากรายชื่อบริษัทที่แจ้งงบจริง
    (แม่น + ไม่พึ่งความสดของ parquet) — ถอยโหมดเดิมเฉพาะตอนข้อมูลข่าว
    ไม่สด เช่นดึงข่าวเว็บ SET ล้มต่อเนื่อง (fail open: ข่าวล่มยังสแกนได้)

    คืน (hits, scanned_count, source_note, unknowns, unknowns_title,
    rate_limited)"""
    filings = scanner.run_filings_scan()
    if filings is not None:
        hits, n, no_data, dropped, rate_limited = filings
        return (hits, n, scanner.filings_source_note(dropped),
                no_data, scanner.FILINGS_UNKNOWNS_TITLE, rate_limited)
    full = scanner.run_full_scan()
    if full is not None:
        hits, n, last_date, unknowns, rate_limited = full
        note = ("โหมดทั้งตลาด (สำรอง — ข้อมูลข่าวแจ้งงบไม่สด) • "
                f"ข้อมูล Trading_Dashboard ถึง {last_date:%d/%m/%Y}")
        return hits, n, note, unknowns, None, rate_limited
    hits, n, rate_limited = scanner.run_scan()
    note = ("โหมดรายชื่อหลัก (ข้อมูลข่าวแจ้งงบและ Trading_Dashboard ไม่สด — "
            "เช็คข่าวด้วย python set_news.py หรือรัน bt_fetch.py ที่โปรเจคนั้น)")
    return hits, n, note, None, None, rate_limited


# กันซ้ำแบบเดียวกับรายงานเช้า: job 17:30 กับ startup fallback ใช้
# digest_state ร่วมกัน (key "scan_last_run") + ธง in-flight (สแกนกินเวลา
# หลายนาที — เปิดเครื่อง 17:29 อาจชนกับ job 17:30 พอดี)
_scan_in_flight = False


async def _run_daily_scan(context: ContextTypes.DEFAULT_TYPE, label: str):
    """ตัวสแกนอัตโนมัติกลาง: ครั้งเดียวต่อวันทำการ + อัปเดตไม้เงาต่อท้าย

    เดิมสแกน 17:30 เป็น job ใหญ่ตัวเดียวที่ไม่มี startup fallback —
    ปิดเครื่องก่อนเวลาวันไหน วันนั้นไม่มีแถวใน scan_log และไม้เงาของวันนั้น
    หายถาวร (จุดเข้าไม้เงา = เปิดวันถัดจากวันสแกน ย้อนสร้างไม่ได้)"""
    global _scan_in_flight
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5 or _is_market_holiday(now.date()):
        return
    if _scan_in_flight:
        return
    if _load_digest_state().get("scan_last_run") == now.date().isoformat():
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    _scan_in_flight = True
    try:
        try:
            hits, n, note, unknowns, unk_title, rate_limited = \
                await asyncio.to_thread(run_best_scan)
        except Exception:
            log.exception("%s failed", label)
            return
        report = scanner.format_report(
            hits, n, source_note=note, unknowns=unknowns,
            unknowns_title=unk_title, unknowns_hint="" if unk_title else None,
            rate_limited=rate_limited)
        buttons = _watch_buttons(hits)
        sent_any = False
        for cid in chat_ids:
            try:
                await _send_long(context.bot, cid, report,
                                 reply_markup=buttons, parse_mode="HTML")
                sent_any = True
            except Exception:
                log.exception("send scan report failed (chat %s)", cid)
        if sent_any:
            state = _load_digest_state()
            state["scan_last_run"] = now.date().isoformat()
            _save_digest_state(state)
        # อัปเดต cache ไม้เงาต่อท้ายสแกน — ให้ส่วนไม้เงาใน "สถิติ" สดวันต่อวัน
        # โดยผู้ใช้ไม่ต้องพิมพ์ "เงา" เอง (ราคาที่ต้องใช้ส่วนใหญ่เพิ่งถูกดึงไปแล้ว)
        try:
            await asyncio.to_thread(shadow.update_shadow)
        except Exception:
            log.exception("shadow update after scan failed")
    finally:
        _scan_in_flight = False


async def daily_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """job รายวัน 17:30: สแกนอัตโนมัติหลังปิดตลาด"""
    await _run_daily_scan(context, "daily scan")


async def startup_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """ตอนเปิดบอท: เปิดเครื่องค่ำเลย 17:30 และวันนี้ยังไม่ได้สแกน → สแกนชดเชย
    (ก่อนเวลานั้นไม่ทำ — วอลุ่มวันยังไม่ครบ สแกนไปตัวเลขต่ำกว่าจริง)"""
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.time() < datetime.time(SCAN_HOUR, SCAN_MINUTE):
        return
    await _run_daily_scan(context, "startup scan")


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
        ), []

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
    stale = []  # ตัวหมดสภาพ — ไว้ทำปุ่ม 🗑 ให้กดเอาออก
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

        # ── สุขอนามัยลิสต์: ตัวไหนหมดสภาพ ป้ายเหตุผล + เข้าคิวปุ่ม 🗑 ──
        # ยกเว้นตัวที่กำลังรองบรอบใหม่ (ผู้ใช้ติดตามล่วงหน้าเพื่อรอเตือน
        # 08:45 — อย่าชวนลบทิ้ง)
        waiting = (d["days_to_earnings"] is not None
                   and d["days_to_earnings"] <= 14)
        reason = None
        if waiting:
            pass
        elif s is None:
            reason = "ไม่รู้วันงบ (บอทเฝ้าสัญญาณให้ไม่ได้)"
        elif not s["above_pre_low"]:
            reason = "โดน ⛔ ไปแล้ว — ตามกติกาต้องออก"
        elif (d["days_since_earnings"] or 0) > 60:
            reason = f"งบผ่านมา {d['days_since_earnings']} วัน — สัญญาณหมดอายุ (60 วัน)"
        if reason:
            stale.append(base)
            lines.append(f"    🗑 {reason}")
    if errors:
        lines.append("")
        lines.append("⚠️ ดึงไม่ได้: " + ", ".join(html.escape(e) for e in errors))
    lines.append("")
    if stale:
        lines.append("🗑 ตัวที่หมดสภาพ กดปุ่มด้านล่างเอาออกได้เลย "
                     "(กันลิสต์บวมจนบังตัวที่ยังมีสัญญาณ)")
    lines.append("ลบเอง: <code>เลิกติดตาม AOT</code>")
    return "\n".join(lines), stale


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
        stock_core.save_json_atomic(_PORT_SETTINGS_PATH, data)
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


# ── รายงานไม้เปิดประจำวัน (17:45 หลังสแกน) ──────────────────────
# ทำส่วน "ถือต่อ/ลด" ของ workflow ให้อัตโนมัติ: R ปัจจุบัน + ระยะถึงเส้น ⛔
# + นับวันเงียบ (กติกา: เงียบเกิน 5 วันทำการ = เริ่มพิจารณาลด) — เมื่อก่อน
# ต้องเปิด "ลิสต์" แล้วนับเอง

OPENPOS_HOUR, OPENPOS_MINUTE = 17, 45
OPENPOS_QUIET_DAYS = 5       # ไม่ทำไฮใหม่เกินกี่วันทำการ = ⚠️ พิจารณาลด
OPENPOS_NEAR_STOP_PCT = 2.0  # เหลือระยะถึงเส้น ⛔ ไม่เกินกี่ % = ⚠️ ใกล้โดนตัด


def _open_trade_metrics(t):
    """สถานะไม้เปิดหนึ่งไม้จากราคาย้อนหลังตั้งแต่วันเข้า (ยิง Yahoo ตัวละครั้ง)
    คืน dict หรือ None ถ้าดึงราคาไม่ได้"""
    try:
        fetched = stock_core.fetch_history(t["symbol"], period="1y")
    except Exception:
        fetched = None
    if fetched is None:
        return None
    _, hist = fetched
    entry_day = t["datetime"].date()
    since = hist[[d >= entry_day for d in hist.index.date]]
    if since.empty:
        return None
    cur = float(since["Close"].iloc[-1])
    # วันเงียบ = กี่วันทำการแล้วที่ไม่ทำไฮใหม่ (นับจากวันเข้า แบบเดียวกับ
    # days_since_new_high ใน stock_core แต่ยึดช่วงถือจริงของไม้นี้)
    highs = since["High"]
    new_high_pos = (highs >= highs.cummax()).values.nonzero()[0]
    quiet = len(highs) - 1 - int(new_high_pos[-1])
    m = {"cur": cur, "pct": (cur - t["price"]) / t["price"] * 100,
         "quiet": quiet, "r": None, "to_stop_pct": None, "hit_stop": False}
    if t["stop"] is not None:
        m["to_stop_pct"] = (cur - t["stop"]) / cur * 100  # ลงอีกกี่ % ถึงเส้น ⛔
        m["hit_stop"] = cur <= t["stop"]
        if t["price"] > t["stop"]:
            m["r"] = (cur - t["price"]) / (t["price"] - t["stop"])
    return m


def _open_trade_lines(open_):
    """บรรทัดสถานะไม้เปิด (ใช้ร่วมกันระหว่างรายงาน 17:45 กับคำสั่ง "เทรด")"""
    lines = []
    for t in sorted(open_, key=lambda x: x["datetime"]):
        m = _open_trade_metrics(t)
        sym = html.escape(t["symbol"])
        if m is None:
            lines.append(f"• <b>{sym}</b> ซื้อ {t['price']:,.2f} "
                         f"({t['datetime']:%d/%m}) — ดึงราคาล่าสุดไม่ได้")
            continue
        r_txt = f"  <b>{m['r']:+.2f}R</b>" if m["r"] is not None else ""
        lines.append(f"• <b>{sym}</b> {t['price']:,.2f} ({t['datetime']:%d/%m}) "
                     f"→ {m['cur']:,.2f} ({m['pct']:+.1f}%){r_txt}")
        detail = []
        if m["hit_stop"]:
            detail.append(f"⛔ หลุดเส้นตัด {t['stop']:,.2f} แล้ว — ทำตามเครื่อง ไม่ต่อรอง")
        elif m["to_stop_pct"] is not None:
            if m["to_stop_pct"] <= OPENPOS_NEAR_STOP_PCT:
                detail.append(f"⚠️ เหลือ {m['to_stop_pct']:.1f}% ถึงเส้น ⛔ {t['stop']:,.2f}")
            else:
                detail.append(f"ห่างเส้น ⛔ {m['to_stop_pct']:.1f}%")
        if m["quiet"] >= OPENPOS_QUIET_DAYS:
            detail.append(f"⚠️ เงียบ {m['quiet']} วันทำการ "
                          f"(เกิน {OPENPOS_QUIET_DAYS} = พิจารณาลด)")
        elif m["quiet"] == 0:
            detail.append("ทำไฮใหม่วันนี้ 🔥")
        else:
            detail.append(f"ไฮใหม่ล่าสุด {m['quiet']} วันก่อน")
        if detail:
            lines.append("   " + " • ".join(detail))
    return lines


def build_open_positions_report(chat_id) -> str:
    """รายงานไม้เปิดประจำวันของ chat หนึ่ง — "" ถ้าไม่มีไม้เปิด (ไม่ส่ง)"""
    _, open_ = stock_core.pair_trades(stock_core.load_trades(chat_id))
    if not open_:
        return ""
    lines = [f"📂 <b>ไม้ที่เปิดอยู่ ({len(open_)})</b> — เช็คประจำวันหลังปิดตลาด"]
    lines += _open_trade_lines(open_)
    lines.append("")
    lines.append("ปิดไม้เมื่อไหร่จด: <code>ขาย XXX ราคา</code> • ดูไม้ปิดแล้ว: <code>เทรด</code>")
    return "\n".join(lines)


# กันส่งซ้ำแบบเดียวกับสรุปงบเช้า (key "openpos_last_sent") + ธง in-flight
_openpos_in_flight = False


async def _run_open_positions(context: ContextTypes.DEFAULT_TYPE, label: str):
    """ตัวส่งรายงานไม้เปิดกลาง: ครั้งเดียวต่อวัน — ไม่มีไม้เปิด = ไม่ส่ง"""
    global _openpos_in_flight
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.weekday() >= 5 or _is_market_holiday(now.date()):
        return
    if _openpos_in_flight:
        return
    if _load_digest_state().get("openpos_last_sent") == now.date().isoformat():
        return
    chat_ids = _load_chat_ids()
    if not chat_ids:
        return
    _openpos_in_flight = True
    try:
        sent_any = False
        for cid in chat_ids:
            try:
                text = await asyncio.to_thread(build_open_positions_report, cid)
            except Exception:
                log.exception("%s failed (chat %s)", label, cid)
                continue
            if not text:
                continue
            try:
                await _send_long(context.bot, cid, text, parse_mode="HTML")
                sent_any = True
            except Exception:
                log.exception("send %s failed (chat %s)", label, cid)
        if sent_any:
            state = _load_digest_state()
            state["openpos_last_sent"] = now.date().isoformat()
            _save_digest_state(state)
    finally:
        _openpos_in_flight = False


async def open_positions_job(context: ContextTypes.DEFAULT_TYPE):
    """job รายวัน 17:45: สถานะไม้เปิดหลังปิดตลาด (ต่อจากสแกน 17:30)"""
    await _run_open_positions(context, "open positions")


async def startup_openpos_job(context: ContextTypes.DEFAULT_TYPE):
    """ตอนเปิดบอท: ถ้าเลย 17:45 มาแล้ววันนี้ยังไม่ได้ส่ง (เปิดคอมตอนค่ำ)
    → ส่งชดเชย (ก่อน 17:45 ปล่อยให้ job ปกติทำงาน)"""
    now = datetime.datetime.now(ZoneInfo("Asia/Bangkok"))
    if now.time() < datetime.time(OPENPOS_HOUR, OPENPOS_MINUTE):
        return
    await _run_open_positions(context, "startup open positions")


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
        lines += _open_trade_lines(open_)
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
    await _dispatch_text(update, context, update.message.text)


async def _dispatch_text(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         raw_text: str):
    text = raw_text.strip()
    tickers = text.split()

    # สแกนทั้งกระดานเดี๋ยวนี้: "สแกน"
    if len(tickers) == 1 and tickers[0].lower() in ("สแกน", "scan"):
        await update.message.reply_text(
            "🔍 กำลังสแกน... ใช้เวลาประมาณ 1-3 นาที รอสักครู่ครับ"
        )
        hits, n, note, unknowns, unk_title, rate_limited = \
            await asyncio.to_thread(run_best_scan)
        await _reply_long(
            update.message,
            scanner.format_report(
                hits, n, source_note=note, unknowns=unknowns,
                unknowns_title=unk_title, unknowns_hint="" if unk_title else None,
                rate_limited=rate_limited),
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

    # ไม้เงา: "เงา" — ระบบเทรดกระดาษเองจากผลสแกน (พิสูจน์คะแนนด้วยข้อมูล forward)
    if len(tickers) == 1 and tickers[0].lower() in ("เงา", "shadow"):
        await update.message.reply_text(
            "⏳ กำลังอัปเดตไม้เงาจาก scan_log.csv (ยิงราคาเฉพาะไม้ที่ยังเปิด)..."
        )
        result = await asyncio.to_thread(shadow.build_shadow_report)
        await _reply_long(update.message, result, parse_mode="HTML")
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

    # ปฏิทินงบล่วงหน้า: "ปฏิทิน" — อ่านจากคลังในเครื่อง ตอบได้ทันที
    if len(tickers) == 1 and tickers[0].lower() in ("ปฏิทิน", "calendar"):
        result = build_earnings_calendar(update.effective_chat.id)
        if not result:
            result = (
                f"📭 ไม่รู้วันงบของตัวไหนใน {CALENDAR_DAYS_AHEAD} วันข้างหน้าครับ\n"
                "คลังวันงบมีเฉพาะตัวที่บอทเคยเห็น — บันทึกเองได้: "
                "<code>งบ XXXX 14/08/2569</code>"
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
        added, rejected, unchecked = [], [], []
        for t in tickers[1:]:
            base = t.upper().strip().replace(".BK", "")
            # กันชื่อที่ไม่ใช่ ticker (คำไทย/อักขระแปลก) — ของเสียตัวเดียว
            # ทำข้อความแจ้งเตือนรวม (HTML) ของทั้งลิสต์ส่งไม่ออก
            if not re.fullmatch(r"[A-Z0-9.&-]{1,15}", base):
                rejected.append(t)
                continue
            # เช็คว่าดึงข้อมูลได้จริงก่อนรับ — กันพิมพ์ผิด (เช่น TSTJ แทน
            # TSTH) ค้างในลิสต์ให้บอทไล่ดึงทุก 15 นาทีโดยไม่มีวันสำเร็จ
            try:
                d = await asyncio.to_thread(stock_core.get_stock_data, base)
            except Exception:
                d = False  # Yahoo ล่ม/rate limit — อย่าขวางการเก็บเข้าลิสต์
            if d is None:
                rejected.append(base)
                continue
            b, _ = stock_core.add_to_watchlist(base, update.effective_chat.id)
            added.append(b)
            if d is False:
                unchecked.append(b)
        lines = []
        if added:
            lines.append(f"✅ เพิ่มเข้าลิสต์แล้ว: <b>{html.escape(' '.join(added))}</b>")
        if unchecked:
            lines.append(f"(เช็คข้อมูล {html.escape(' '.join(unchecked))} "
                         "ไม่ได้ชั่วคราว — รับไว้ก่อน ลองเปิดดูภายหลังอีกครั้ง)")
        if rejected:
            lines.append("❌ ไม่พบข้อมูล: " + html.escape(" ".join(rejected))
                         + " — เช็คชื่ออีกครั้ง (ยังไม่เพิ่มเข้าลิสต์)")
        if added:
            lines.append("ดูทั้งหมด: <code>ลิสต์</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
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
        result, stale = await asyncio.to_thread(
            build_watchlist_summary, update.effective_chat.id
        )
        symbols = stock_core.get_watchlist(update.effective_chat.id)
        await _reply_long(update.message, result,
                          reply_markup=_list_buttons(stale, symbols), parse_mode="HTML")
        return

    # ข้อมูล PHASE 0 สำหรับ Gem เทคนิค: "วิเคราะห์ AOT" (ตัวเดียวกับปุ่ม 📐)
    if tickers and tickers[0].lower() in ("วิเคราะห์", "ta"):
        if len(tickers) < 2:
            await update.message.reply_text(_TA_USAGE, parse_mode="HTML")
            return
        await _ta_reply(update.message, tickers[1])
        return

    # ลิงก์หน้างบที่ Earnings Radar: "คำอธิบายงบ AOT" — แค่ประกอบลิงก์ ไม่ยิงเว็บ ไม่เช็คว่ามีหน้านั้นจริง
    if tickers and tickers[0].lower() in ("คำอธิบายงบ", "mda"):
        if len(tickers) < 2:
            await update.message.reply_text(_MDA_USAGE, parse_mode="HTML")
            return
        sym = stock_core._base_symbol(tickers[1])
        if not _SYMBOL_RE.fullmatch(sym):
            await update.message.reply_text(_MDA_USAGE, parse_mode="HTML")
            return
        await update.message.reply_text(
            f"📅 คำอธิบายงบ / ผลประกอบการของ <b>{html.escape(sym)}</b> ที่ Earnings Radar",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"📅 เปิด Earnings Radar — {sym}", url=_earnings_url(sym))]]),
        )
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


# ── เมนู "/" ใน Telegram (setMyCommands ตอน start) — ชื่อ a-z0-9_ ≤32, คำอธิบาย ≤256 · ลำดับ = ลำดับในเมนู ──

BOT_COMMANDS = [
    ("scan", "สแกนหุ้นตอบรับงบดีทั้งกระดานเดี๋ยวนี้ (~1-3 นาที) — เกณฑ์ ≥2% + วอลุ่ม ≥1.5 เท่า"),
    ("list", "ลิสต์หุ้นที่ติดตาม เรียงตามคะแนนสัญญาณหลังงบ (+ปุ่ม 📐 / 🗑 ตัวหมดสภาพ)"),
    ("watch", "ติดตามหุ้น: /watch AOT — บอทเฝ้าช่วงตลาดเปิด เด้งเมื่อ 🔥 ทะลุไฮ / ⛔ หลุด Low ก่อนงบ"),
    ("unwatch", "เลิกติดตาม: /unwatch AOT (หลายตัวพร้อมกันได้)"),
    ("price", "ดูหุ้นรายตัว: /price AOT — ราคา วอลุ่ม วันงบ ไฮ-โลว์ คะแนน (หรือพิมพ์ ticker ตรงๆ)"),
    ("ta", "ข้อมูล PHASE 0 สำหรับ Gem เทคนิค: /ta AOT — บล็อกกดคัดลอก + ปุ่มเปิด Gem (ปุ่ม 📐)"),
    ("digest", "สรุปงบเช้า: ใครแจ้งงบเย็นวาน+เช้านี้ เรียงตามกำไรโต (ใส่เลข = ย้อน N วัน)"),
    ("confirm", "ยืนยันรอบเช้า: ตลาดตอบรับหุ้นงบโตแรงเมื่อคืนยังไง (gap + วอลุ่มสะสม)"),
    ("news", "เช็คข่าวแจ้งงบจากเว็บ SET เดี๋ยวนี้ (~1 นาที ถ้ามี F45 ให้อ่านตัวเลข)"),
    ("calendar", "ปฏิทินงบ 14 วันข้างหน้า จากคลังวันงบในเครื่อง"),
    ("earn", "วันงบรายตัว: /earn AOT — ดู · /earn AOT 13/11/2569 — บันทึกเอง (manual ชนะ Yahoo)"),
    ("stats", "สถิติย้อนหลัง: หุ้นติดสแกนแต่ละช่วงคะแนน ผ่านไป 5/10/20 วัน ชนะกี่ % + ไม้เงา/ไม้จริง"),
    ("shadow", "ไม้เงา: จำลองเทรดทุกตัวที่ติดสแกน เข้าเปิดวันถัดไป ตัดหลุด ⛔ ถือสูงสุด 20 วัน"),
    ("port", "ตั้ง/ดูขนาดพอร์ต: /port 500000 — ใช้คำนวณขนาดไม้ (เสี่ยง 1% ต่อไม้)"),
    ("size", "คำนวณขนาดไม้: /size AOT — ซื้อกี่หุ้นให้หลุด ⛔ แล้วเสียแค่ 1% ของพอร์ต"),
    ("buy", "จดไม้เข้า: /buy AOT 32.50 พร้อมเก็บคะแนน/เส้น ⛔ ณ ตอนเข้า (ใส่จำนวนหุ้นต่อท้ายได้)"),
    ("sell", "ปิดไม้: /sell AOT 35.00 — สรุป % กำไรและ R ทันที"),
    ("trades", "ไม้ที่เปิดอยู่ + ผลไม้ที่ปิดแล้ว"),
    ("mda", "ลิงก์คำอธิบายงบของหุ้นตัวนั้นที่ Earnings Radar: /mda AOT (ปุ่ม 📅)"),
    ("help", "วิธีใช้ทั้งหมด (คำสั่งพิมพ์ไทย + ตารางเวลาอัตโนมัติ)"),
]
# slash → ข้อความที่ส่งเข้า dispatcher เดิม — ชื่อ slash ตรงกับ alias อังกฤษของทุกคำสั่งอยู่แล้ว
# ยกเว้น "price": ตัดคำสั่งทิ้ง เหลือ ticker เพียวๆ เข้าโหมดดูหุ้นรายตัว (help มี handler ตรงอยู่แล้ว)
SLASH_ALIASES = {c: c for c, _ in BOT_COMMANDS if c != "help"}
SLASH_ALIASES["price"] = ""


async def cmd_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """slash alias ของคำสั่งพิมพ์ไทย (เมนู "/" ของ Telegram รับได้เฉพาะ slash):
    /watch AOT → "watch AOT" · /price AOT → "AOT" เข้า _dispatch_text เดิม — ไม่มี logic ซ้ำ"""
    _register_chat(update.effective_chat.id)
    cmd = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
    word = SLASH_ALIASES.get(cmd)
    if word is None:
        return
    text = " ".join([word, *(context.args or [])]).strip()
    if not text:  # /price เปล่าๆ — ปล่อยเข้า dispatcher จะกลายเป็นข้อความว่าง
        await update.message.reply_text(
            "พิมพ์ <code>/price AOT</code> หรือพิมพ์ ticker ตรงๆ เช่น "
            "<code>AOT PTT</code>", parse_mode="HTML")
        return
    await _dispatch_text(update, context, text)


async def register_commands(app):
    """post_init: ลงทะเบียนเมนูคำสั่ง "/" กับ Telegram — ล้ม (เน็ตล่ม) แค่ log ไม่ให้บอทตาย"""
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
        log.info("เมนูคำสั่ง Telegram ลงทะเบียนแล้ว %d รายการ", len(BOT_COMMANDS))
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)


# main

def main():
    if not BOT_TOKEN:
        print("\n  ⚠️  ไม่พบ BOT_TOKEN — กรุณาใส่ในไฟล์ .env (บรรทัด BOT_TOKEN=...)\n")
        return

    print("  Bot กำลังทำงาน... (Ctrl+C เพื่อหยุด)")
    print(f"  สแกนอัตโนมัติทุกวันทำการ เวลา {SCAN_HOUR:02d}:{SCAN_MINUTE:02d} น. "
          "(เปิดบอทช้ากว่านั้น สแกนชดเชยให้เอง)")
    print(f"  เตือนวันงบทุกวันทำการ เวลา {REMIND_HOUR:02d}:{REMIND_MINUTE:02d} น.")
    print(f"  เช็คลิสต์ติดตามทุก {MONITOR_INTERVAL_MIN} นาที ช่วงตลาดเปิด")
    print(f"  เฝ้าข่าวแจ้งงบ (เว็บ SET) ทุก {NEWS_POLL_MINUTES} นาที "
          "ช่วง 07:00-09:45 และ 17:00-21:45")
    print(f"  สรุปงบเช้าทุกวันทำการ เวลา {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} น. "
          "(หรือส่งตอนเปิดบอทถ้ายังไม่ได้ส่ง)")
    print(f"  ยืนยันรอบเช้าทุกวันทำการ เวลา {CONFIRM_HOUR:02d}:{CONFIRM_MINUTE:02d} น. "
          "(เฉพาะวันที่มีหุ้นงบโตแรง)")
    print(f"  รายงานไม้เปิดทุกวันทำการ เวลา {OPENPOS_HOUR:02d}:{OPENPOS_MINUTE:02d} น. "
          "(เฉพาะวันที่มีไม้เปิด)")
    print(f"  ปฏิทินงบสัปดาห์ละครั้ง เวลา {CALENDAR_HOUR:02d}:{CALENDAR_MINUTE:02d} น. "
          "(ปกติเย็นวันอาทิตย์)\n")
    log.info("bot starting")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(register_commands).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(list(SLASH_ALIASES), cmd_alias))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
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
    app.job_queue.run_daily(
        open_positions_job,
        time=datetime.time(OPENPOS_HOUR, OPENPOS_MINUTE,
                           tzinfo=ZoneInfo("Asia/Bangkok")),
    )
    # ปฏิทินงบเป็นรายสัปดาห์ แต่ลง job รายวัน 19:00 — anchor วันอาทิตย์ล่าสุด
    # ใน digest_state ทำให้ส่งจริงครั้งเดียวต่อสัปดาห์ วันไหนก็ได้ที่คอมเปิดก่อน
    app.job_queue.run_daily(
        calendar_job,
        time=datetime.time(CALENDAR_HOUR, CALENDAR_MINUTE,
                           tzinfo=ZoneInfo("Asia/Bangkok")),
    )
    # จดเวลาว่ายังทำงานทุก 5 นาที + เก็บตกข่าวช่วงที่ปิดไป (ครั้งเดียวตอนเปิด)
    app.job_queue.run_repeating(alive_job, interval=300, first=10)
    app.job_queue.run_once(startup_catchup_job, when=20)
    # heartbeat ✅ ตอนเปิดบอท (เครื่องบูต ~09:30 เลย job 08:30 เสมอ —
    # กันซ้ำรายวันด้วย key alive_last_sent ใน digest_state)
    app.job_queue.run_once(startup_heartbeat_job, when=25)
    # startup digest หน่วง 300 วิ ให้ catch-up (วินาที 20) + news poll รอบแรก
    # (วินาที 120) ทำงานจบก่อน ข้อมูลเช้านี้จะได้อยู่ใน CSV แล้ว
    app.job_queue.run_once(startup_digest_job, when=300)
    # ยืนยันรอบเช้าตามหลัง digest (เผื่อเปิดคอมหลัง 10:30 — เงื่อนไขเวลาอยู่ใน job)
    app.job_queue.run_once(startup_confirm_job, when=330)
    # เตือนวันงบตามหลังอีกขั้น (เผื่อเปิดคอมหลัง 08:45 — ยิง Yahoo ทีละตัวในลิสต์
    # จึงให้ job อื่นที่รีบกว่าไปก่อน)
    app.job_queue.run_once(startup_reminder_job, when=360)
    # ไม้เปิดชดเชยตอนเปิดคอมค่ำ (เงื่อนไขหลัง 17:45 อยู่ใน job)
    app.job_queue.run_once(startup_openpos_job, when=390)
    # ปฏิทินงบชดเชย — อ่านจากคลังในเครื่อง ไม่ยิงเครือข่าย วางท้ายสุดพอ
    app.job_queue.run_once(startup_calendar_job, when=420)
    # สแกนชดเชยตอนเปิดคอมค่ำเลย 17:30 (หนักสุด — ยิง Yahoo หลายสิบตัว
    # จึงวางท้ายสุด เงื่อนไขเวลา+กันซ้ำรายวันอยู่ใน job)
    app.job_queue.run_once(startup_scan_job, when=450)
    # ย้าย watchlist.json format เดิม (list) → per-user dict ให้เจ้าของ (ครั้งเดียว)
    stock_core.migrate_legacy_watchlist(_load_chat_ids())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
