# คำสั่ง `วิเคราะห์` — บันทึกการปรับดีไซน์มาใช้กับโปรเจกต์นี้ (Set screnner ↔ Trading_Dashboard)

วันที่: 2026-08-15
สถานะ: ทำแล้ว (เทสต์ 63 รายการเขียว)
ต้นฉบับ: [`2026-08-12-ta-prompt-telegram-design.md`](2026-08-12-ta-prompt-telegram-design.md) — ดีไซน์ของโปรเจกต์คู่แฝด
(`Thai Trading` ↔ `Trading view`) เอามาทำซ้ำบนเครื่องนี้ที่คู่โปรเจกต์คือ **`Set screnner` (บอทนี้) ↔ `Trading_Dashboard`**
(โฟลเดอร์ `..\Dashboard\Trading_Dashboard`, deploy ที่ `https://sakura.peodbot.com`)

สิ่งที่ทำ/ไม่ทำ, ทางที่ไม่เลือก, กติกาความปลอดภัย, error handling และเทมเพลตข้อความ **ยกมาจากต้นฉบับทั้งหมด** —
เอกสารนี้บันทึกเฉพาะ "จุดที่ต้องปรับ" เพราะโค้ดบอทเราต่างจากของเพื่อน และข้อเท็จจริงที่ตรวจบนเครื่องนี้

## ข้อเท็จจริงที่ตรวจแล้วบนเครื่องนี้ (2026-08-15)

- payload โครงเดียวกับต้นฉบับทุกประการ: `site/data/manifest.json` (schema_version 1) → `data/<build_id>/{core.json, stocks-TH.json}`
  882 ตัว field ครบทุกตัวที่ `taPrompt()` ใช้ · `ed` **ไม่มี key** ในหุ้น TH (ไม่ใช่แค่ null) → ต้อง `.get("ed")`
- `taPrompt()` อยู่ `market_dashboard.js:58` ตำแหน่ง/ค่าคงที่เดียวกับที่ต้นฉบับอ้าง (STAGE_DEF :7-10, TREND_W :18, MKT_LABEL :48, SCAN_LABEL :49, idxRetBars :51-57)
- build บนเว็บ (`sakura.peodbot.com/data/manifest.json`) ตรงกับในเครื่อง
- **Cloudflare ตอบ `403 error 1010` (Browser Integrity Check) กับ User-Agent เริ่มต้นของ `urllib`** — ต้องส่ง UA แบบเบราว์เซอร์
  (ต้นฉบับใช้ workers.dev ไม่เจอเรื่องนี้)
- ไม่มีหุ้น TH ชื่อ `TA` / `MDA` → alias ไม่ชนชื่อหุ้น · ชื่อบริษัทยาวสุด 48 ตัวอักษร
- Node v24 มีในเครื่อง → รัน `taPrompt()` ตัวจริงจาก JS ได้ (ใช้ทำ drift test)
- pytest 9.1.1 ติดตั้งอยู่แล้วใน Python 3.14 ที่บอทใช้

## ส่วนต่างจากต้นฉบับ → ทำแบบนี้

| เรื่อง | ต้นฉบับ (Thai Trading) | โปรเจกต์นี้ (Set screnner) |
|---|---|---|
| โครงไฟล์ | `infra/dashboard_feed.py` | โปรเจกต์แบน → `dashboard_feed.py` ที่ root ข้าง `scanner.py` |
| ลงทะเบียนคำสั่ง | `@_command("วิเคราะห์","ta")` + ตาราง `COMMANDS` + เทสต์บังคับ alias | if-chain ใน `handle_text()` — แทรก 2 บล็อกหลัง `ลิสต์` ก่อน fallthrough "ดูหุ้น" (สไตล์เดิมของไฟล์) |
| อ่าน env | ใน `infra/dashboard_feed` | `dashboard_feed` **ไม่อ่าน env เอง** รับ `site_dir/url/max_age_days` เป็นพารามิเตอร์ (เทสต์ง่าย) · `telegram_bot` อ่านผ่าน `scanner._env_value` ที่มีอยู่แล้ว และใช้ `scanner.LOCAL_MAX_AGE_DAYS` (5) เป็นเกณฑ์เก่า — แหล่งเดียวกับ scanner จริง ๆ |
| วันงบ | SQLite `_sqlite_earnings_dates()` | JSON `stock_core._EARN_STORE` (`.yf_cache/earnings_dates.json`) → `stock_core.next_earnings_date()` manual ชนะ auto ไม่ยิง Yahoo (กติกาเดียวกับ `next_earn` ใน `_fetch_stock_data`) |
| `today_bkk()` | มีอยู่แล้ว | เพิ่มใน `stock_core` (`datetime.now(_BKK).date()`) ใช้เฉพาะโค้ดใหม่ ไม่ refactor ที่อื่น |
| ปุ่มใต้ผลสแกน | `_watch_buttons` 9 จุด ปุ่ม 👀 | 6 จุด ปุ่ม `➕ watch:` → **หุ้นละแถว** `[➕ S] [📐 S ta:S] [📅 S url]` แก้ `_stock_row` จุดเดียว |
| ปุ่มใต้ `ลิสต์` | เพิ่มชุด 📐 ต่อจาก 🗑 | `_unwatch_buttons(stale)` ครอง reply_markup อยู่ → รวมเป็น `_list_buttons(stale, symbols)` = แถว 🗑 ของตัวหมดสภาพ + แถว 📐 ของทั้งลิสต์ (`stock_core.get_watchlist`, 15 ตัวแรก) |
| callback | router `_command` | `elif data.startswith("ta:")` ใน `on_button` — `await q.answer("⏳…")` **ก่อน** ทำงาน (Telegram หมดอายุปุ่ม ~15 วิ) แล้ว `_ta_reply` ใน try/except |
| HTTPS fallback | `urllib` เปล่า | `urllib` + header `User-Agent` แบบเบราว์เซอร์ (CF 1010) · timeout 10 วิ ไม่ retry |
| local เก่า + HTTPS ล้ม | ล้มทั้งคู่ → `DashboardUnavailable` | **ใช้ของเก่าในเครื่องพร้อม `stale=True`** (ข้อความขึ้น ⚠️ อายุข้อมูล) — ตามเจตนาของตาราง error handling ต้นฉบับ "ข้อมูลเก่ายังส่งให้ ผู้ใช้ตัดสินเอง" · โยน exception เฉพาะไม่มีอะไรเลย |
| default URL | `https://tradingdashboard.sasukae00.workers.dev` | `https://sakura.peodbot.com` |
| ตัดชื่อบริษัท 60 ตัว | ตัด | **ไม่ตัด** — รักษา "เท่ากับปุ่ม 📐 ทุกบรรทัด" (ยาวสุดจริง 48; เทสต์ยืนยันชื่อ 200 ตัวยัง < 3,000) |
| `vmax3 = []` | ไม่ crash | ไม่ crash และเขียน "ไม่มีข้อมูล" (JS จะได้ `undefined` — ตั้งใจให้ดีกว่า JS; payload จริงไม่มีเคสนี้ drift test จึงไม่ชน) |
| drift test | ดึงหัวบรรทัดจาก template ใน JS มาเทียบ | **รัน `taPrompt()` ตัวจริงใน Node** กับหุ้นจริง ~36 ตัว (หัว/ท้าย/เคสสุดขั้ว/สุ่ม seed คงที่) เทียบทั้งก้อนยกเว้น ข.8 + ชั้นสองเทียบหัวบรรทัด · skip ถ้าไม่มี node/ไฟล์ dashboard · มี `test_comparator_detects_drift` กันเทสต์หลอก |
| help | สร้างจาก `COMMANDS` | แก้ `cmd_help` (+หัวข้อ 📐 +alias `ta`/`mda`) และเปลี่ยนส่งผ่าน `_reply_long` (ใกล้เพดาน 3,900) |
| เอกสาร | CLAUDE.md + AGENTS.md | CLAUDE.md, README.md, `.env.example`, `คู่มือการใช้งาน.txt` (ไม่มี AGENTS.md) |
| pytest | มีอยู่แล้ว | เพิ่ม `pytest==9.1.1` ใน `requirements.txt` (dev) + สร้าง `tests/` — convention ใหม่ของโปรเจกต์ (เดิม "ไม่มี test suite") |

## ไฟล์ที่เพิ่ม/แก้

ใหม่: `ta_prompt.py`, `dashboard_feed.py`, `tests/test_ta_prompt.py`, `tests/test_ta_prompt_matches_dashboard.py`,
`tests/test_dashboard_feed.py`, `tests/test_stock_core_earnings.py`, `tests/test_bot_ta.py`, เอกสาร 2 ไฟล์นี้
แก้: `stock_core.py` (`today_bkk`, `next_earnings_date`), `telegram_bot.py` (config env, `_ta_reply`, `_stock_row`/`_ta_rows`/`_list_buttons`,
2 คำสั่ง, `ta:` callback, `cmd_help`), `.env.example`, `requirements.txt`, `CLAUDE.md`, `README.md`, `คู่มือการใช้งาน.txt`
ลบ: `_unwatch_buttons` (ไม่มีผู้เรียกแล้ว — แทนด้วย `_unwatch_rows` + `_list_buttons`)

## env บนเครื่องนี้ (`.env` — gitignore)

| คีย์ | ค่า |
|---|---|
| `DASHBOARD_SITE_DIR` | `C:\Users\LEVEL51PC\Desktop\Dashboard\Trading_Dashboard\site` |
| `DASHBOARD_URL` | `https://sakura.peodbot.com` |
| `GEM_TA_URL` | ลิงก์ Gem เทคนิคของผู้ใช้ (ไม่บันทึกในเอกสาร) |

## ตรวจด้วยมือหลังเทสต์ผ่าน (ยังไม่ได้ทำ ณ วันที่เขียน — ต้องรีสตาร์ทบอทก่อน)

`วิเคราะห์ AOT` → บล็อกคัดลอกได้ + ปุ่ม Gem · `ta` เปล่า → วิธีใช้ · `ta ZZZZ` → "ไม่มีในระบบ" · `mda ACE` → ปุ่ม Earnings Radar ·
`สแกน` / `สรุปงบ` / `ลิสต์` → ปุ่ม 📐 กดแล้วได้บล็อกเดียวกัน · วางใน Gem พร้อมรูป Weekly + Daily → Gem ข้าม PHASE 0 ไม่ถามกลับ
