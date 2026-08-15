# SET Earnings-Reaction Screener + Telegram Bot

ระบบเทรดหุ้นไทย (SET) แนว **"ปฏิกิริยาราคาหลังประกาศงบ"** — Telegram bot ที่สแกนหาหุ้น
ตอบรับงบดี ให้คะแนนสัญญาณ เฝ้า watchlist ช่วงตลาดเปิด และดึงข่าวแจ้งงบจากเว็บ SET อัตโนมัติ

> A Thai stock (SET) post-earnings price-reaction screener. It scans for stocks reacting
> well to earnings, scores the signal, watches a watchlist during market hours, and auto-pulls
> earnings filings from set.or.th. Runs on a single machine — no server required.

---

## ความต้องการของระบบ (Requirements)

- **Python 3.11+**
- Telegram bot token (สร้างเองจาก [@BotFather](https://t.me/BotFather))
- เชื่อมต่ออินเทอร์เน็ต (ดึงข้อมูลจาก Yahoo Finance + เว็บ SET)

## ติดตั้ง (Setup)

```bash
# 1) ติดตั้ง dependencies
python -m pip install -r requirements.txt

# 2) ติดตั้ง Chromium สำหรับ Playwright (จำเป็นสำหรับดึงข่าว SET — ครั้งแรกครั้งเดียว)
python -m playwright install chromium
#    บน Linux/VPS อาจต้องติดตั้ง system deps เพิ่ม:
#    python -m playwright install-deps chromium

# 3) สร้างไฟล์ตั้งค่า แล้วเติมค่าจริง
copy .env.example .env      # Windows
# cp .env.example .env      # Linux/macOS
```

จากนั้นเปิด `.env` แล้วใส่:

```
BOT_TOKEN=<token ของคุณจาก @BotFather>
DASHBOARD_TH_CACHE=<ไม่บังคับ — path ไปยัง parquet cache ของ Trading_Dashboard>
```

> ⚠️ **สำคัญ:** แต่ละคน/แต่ละเครื่องต้องใช้ `BOT_TOKEN` **ของตัวเอง** — ห้ามใช้ token
> เดียวกันพร้อมกันสองเครื่อง ไม่งั้นบอทจะแย่ง polling กันแล้วรวน `.env` เป็นความลับ
> ถูก gitignore ไว้ — **อย่า commit และอย่าแชร์**

## การรัน (Usage)

```bash
python telegram_bot.py        # รันบอท (entry point หลัก) — ต้องมี BOT_TOKEN ใน .env
python stock_lookup.py AOT    # ดูหุ้นรายตัวผ่าน CLI (ไม่ต้องมีบอท)
python scanner.py             # สแกนหาหุ้นตอบรับงบดีด้วยมือ
python set_news.py            # ดึงข่าวแจ้งงบจากเว็บ SET (ทดสอบ Playwright)
python stats.py               # สถิติย้อนหลังจาก scan_log.csv
python backtest.py            # backtest ระบบคะแนนกับข้อมูล ~2 ปี (ใช้เวลา 5-10 นาที)
```

ทุกโมดูลรันเดี่ยวได้ ไม่มี test suite — ตรวจด้วยการรันสคริปต์นั้นตรงๆ

### Windows (แนะนำสำหรับเครื่องส่วนตัว)

- `เริ่ม Bot.bat` — รันบอทพร้อม auto-restart ใน 15 วินาทีถ้า crash (Ctrl+C = ปิดปกติ)
- `ติดตั้งเปิดเองตอนบูต.bat` — ลงทะเบียนให้บอทเปิดเองตอนบูตเครื่อง

### Linux / VPS

ไฟล์ `.bat` ใช้ได้เฉพาะ Windows — บน VPS ให้รัน `python telegram_bot.py` โดยตรง
แนะนำครอบด้วย `systemd` service หรือ `screen`/`tmux` เพื่อให้รันค้างไว้และ restart อัตโนมัติ
(โค้ด Python เองพกพาข้ามระบบปฏิบัติการได้ — ไม่มีการเรียก API เฉพาะ Windows)

## การใช้งานผ่าน Telegram

คำสั่งเป็น**ข้อความภาษาไทยธรรมดา** (ไม่ใช่ slash command) เช่น:

```
สแกน                 # สแกนหาหุ้นตอบรับงบดีตอนนี้
ติดตาม AOT           # เก็บหุ้นเข้า watchlist ให้บอทเฝ้าและเด้งเตือน
ลิสต์                # ดูรายการที่ติดตามอยู่
งบ AOT 13/11/2569    # บันทึกวันงบเอง (แม่นกว่า Yahoo)
สถิติ                # ดูสถิติย้อนหลังว่าคะแนนทำนายได้จริงไหม
วิเคราะห์ AOT         # ข้อมูล PHASE 0 สำหรับ Gem เทคนิค (บล็อกคัดลอก + ปุ่มเปิด Gem)
คำอธิบายงบ AOT        # ลิงก์หน้างบของหุ้นที่ Earnings Radar
```

ใต้ผลสแกน / สรุปงบ / ยืนยัน / ลิสต์ มีปุ่ม `➕` (ติดตาม) `📐` (PHASE 0) `📅` (หน้างบ) ต่อหุ้น
กดต่อได้เลยไม่ต้องพิมพ์ชื่อซ้ำ

ดูคู่มือเต็มใน [`คู่มือการใช้งาน.txt`](คู่มือการใช้งาน.txt)

## การเชื่อมกับ Trading_Dashboard

โหมด **"สแกนทั้งตลาด"** ของ `scanner.py` อ่าน parquet cache จากโปรเจกต์
[Trading_Dashboard](https://github.com/arthittue/Trading_Dashboard) (~883 ตัว ไม่ต้องยิง Yahoo)
โดยชี้ path ผ่าน env var `DASHBOARD_TH_CACHE` ใน `.env`

- **ตั้งค่า path** → ใช้โหมดทั้งตลาด (ถ้าข้อมูลไม่เก่าเกิน 5 วัน)
- **เว้นว่าง** → ระบบถอยไปใช้โหมดรายชื่อหลัก (`scan_universe.txt` ~94 ตัว) อัตโนมัติ

ตอนนำสองโปรเจกต์ไปรวมบน VPS เดียวกัน แค่ตั้ง `DASHBOARD_TH_CACHE` ให้ชี้ไปที่โฟลเดอร์
`data_cache/TH` ของ Trading_Dashboard ก็เชื่อมกันได้ทันที

**คำสั่ง `วิเคราะห์ XXX` / ปุ่ม 📐** อ่านคนละส่วนของ Trading_Dashboard: payload JSON ที่หน้าเว็บใช้
(`site/data/manifest.json` → `data/<build>/stocks-TH.json` + `core.json`) แล้วประกอบข้อความ PHASE 0
ชุดเดียวกับปุ่ม 📐 บน dashboard ให้กดคัดลอกจาก Telegram ไปวางใน Gem เทคนิคได้ (`ta_prompt.py` port
`taPrompt()` ของ dashboard มาตัวต่อตัว — เทสต์กัน drift ใน `tests/`) — ตั้งค่าใน `.env`:

| คีย์ | ค่า |
|---|---|
| `DASHBOARD_SITE_DIR` | โฟลเดอร์ `site` ของ Trading_Dashboard ในเครื่อง (อ่านไฟล์ตรง) — เว้นว่าง = ใช้เว็บ |
| `DASHBOARD_URL` | dashboard ที่ deploy (ต้นทางสำรอง เมื่อไม่มีโฟลเดอร์/ข้อมูลเก่ากว่า 5 วัน) — default `https://sakura.peodbot.com` |
| `GEM_TA_URL` | ลิงก์ Gem เทคนิคของคุณ (`https://gemini.google.com/gem/…`) สำหรับปุ่ม "📐 เปิด Gem เทคนิค" — เว้นว่าง = หน้ารายการ Gems |

บอทไม่วิเคราะห์เอง ไม่เรียก Gemini API และไม่แก้ไฟล์ของ Trading_Dashboard (อ่านอย่างเดียว)

## โครงสร้างโปรเจกต์

| ไฟล์ | หน้าที่ |
|---|---|
| `telegram_bot.py` | entry point หลัก: handler + scheduled jobs |
| `stock_core.py` | โมดูลกลาง: ดึงข้อมูล Yahoo, คำนวณสัญญาณ, คะแนน, watchlist |
| `scanner.py` | สแกนหุ้นตอบรับงบ (2 โหมด: universe / ทั้งตลาด) |
| `set_news.py` | ดึงข่าวแจ้งงบจากเว็บ SET ผ่าน Playwright |
| `stats.py` / `backtest.py` | วิเคราะห์ย้อนหลัง / backtest ระบบคะแนน |
| `ta_prompt.py` / `dashboard_feed.py` | ข้อความ PHASE 0 สำหรับ Gem เทคนิค / อ่าน payload ของ Trading_Dashboard |
| `tests/` | `python -m pytest` — เทสต์ส่วน "วิเคราะห์" (ไม่แตะเน็ต; drift test เทียบกับ JS ของ dashboard ผ่าน Node) |

ดูรายละเอียดสถาปัตยกรรมใน [`CLAUDE.md`](CLAUDE.md) และเอกสารประกอบ:
[`วงจรระบบ.txt`](วงจรระบบ.txt), [`workflow เทรดหลังงบ.txt`](workflow%20เทรดหลังงบ.txt)

## ข้อมูล/สถานะที่ไม่เข้า git (gitignored)

ไฟล์เหล่านี้เป็นความลับหรือข้อมูลเฉพาะเครื่อง — สร้างเองตอนรัน ไม่ถูก commit:
`.env`, `chat_ids.json`, `watchlist.json`, `watch_state.json`, `news_seen.json`,
`last_alive.json`, `digest_state.json`, `scan_log.csv`, `lookup_log.csv`,
`earnings_results.csv`, `filings_log.csv`, `backtest_results.csv`, `bot_log.txt`, `.yf_cache/`
