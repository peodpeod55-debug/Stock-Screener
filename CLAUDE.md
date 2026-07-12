# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## โปรเจกต์นี้คืออะไร

ระบบเทรดหุ้นไทย (SET) แนว "ปฏิกิริยาราคาหลังประกาศงบ" — Telegram bot ที่สแกนหาหุ้นตอบรับงบดี ให้คะแนนสัญญาณ เฝ้า watchlist ช่วงตลาดเปิด และดึงข่าวแจ้งงบจากเว็บ SET อัตโนมัติ รันบน Windows เครื่องเดียว ไม่มี server

ภาษาในโค้ด: comment และข้อความ UI เป็นภาษาไทย — เขียนโค้ดใหม่ให้เป็นสไตล์เดียวกัน

## คำสั่งที่ใช้บ่อย

```
python -m pip install -r requirements.txt   # ติดตั้ง dependencies
python -m playwright install chromium       # จำเป็นสำหรับดึงข่าว SET (ครั้งแรกครั้งเดียว)

python telegram_bot.py        # รันบอท (entry point หลัก) — ต้องมี BOT_TOKEN ใน .env
python stock_lookup.py AOT    # ดูหุ้นรายตัวผ่าน CLI (ไม่ต้องมีบอท)
python scanner.py             # สแกนหาหุ้นตอบรับงบดีด้วยมือ
python set_news.py            # ดึงข่าวแจ้งงบจากเว็บ SET (ทดสอบ Playwright)
python stats.py               # สถิติย้อนหลังจาก scan_log.csv (คะแนนทำนายได้จริงไหม)
python backtest.py            # backtest ระบบคะแนนกับข้อมูล ~2 ปี (ใช้เวลา 5-10 นาที)
```

- `เริ่ม Bot.bat` — รันบอทพร้อม auto-restart ใน 15 วินาทีถ้า crash (Ctrl+C = ปิดปกติ ไม่วนกลับ)
- `ติดตั้งเปิดเองตอนบูต.bat` — ลงทะเบียนให้บอทเปิดเองตอนบูตเครื่อง
- ไม่มี test suite / linter — ตรวจด้วยการรันสคริปต์นั้นตรงๆ (ทุกโมดูลรันเดี่ยวได้)

## สถาปัตยกรรม

```
telegram_bot.py   ← entry point: handler + scheduled jobs ทั้งหมด
  ├── stock_core.py   ← โมดูลกลาง: ดึงข้อมูล Yahoo, คำนวณสัญญาณ, คะแนน, watchlist
  ├── scanner.py      ← สแกนหุ้นตอบรับงบ (เกณฑ์: ตอบรับ ≥2%, วอลุ่ม ≥1.5 เท่า)
  ├── set_news.py     ← ดึงข่าวแจ้งงบจากเว็บ SET ผ่าน Playwright
  └── stats.py        ← สถิติจาก scan_log.csv
stock_lookup.py       ← CLI ดูหุ้นรายตัว (ใช้ stock_core เหมือนบอท)
backtest.py           ← จำลองระบบคะแนนกับวันงบเก่าจาก Yahoo
```

### stock_core.py — หัวใจของระบบ

- **ทุก request ไป Yahoo ต้องผ่าน `_FETCH_LOCK` เดียว** (บอทรันหลาย job พร้อมกัน — lock กันทั้ง race และ rate limit) ใช้ session `curl_cffi` ปลอมเป็น Chrome + `_retry()` exponential backoff
- Ticker ไทยไม่ต้องใส่ `.BK` — ระบบลอง `.BK` ก่อนแล้วค่อยลองตรงๆ (รองรับหุ้น US ด้วย)
- ดึงราคา 1 ปีใน request เดียว แล้วคำนวณทุกอย่างจากชุดนั้น (5 วัน / 3 เดือน / 52 สัปดาห์ / สัญญาณหลังงบ) — ปรัชญาของระบบ: ตัวเลข "คำนวณย้อนหลังได้เสมอ" ปิดบอทไปนานแค่ไหนกลับมาก็ถูกต้อง
- Cache 3 ชั้น: ชื่อบริษัท (ถาวร ใน `.yf_cache/ticker_names.json`), วันงบ (refresh วันละครั้ง ใน `.yf_cache/earnings_dates.json`), ผลลัพธ์ทั้งชุด (TTL 60 วิ ช่วงตลาดเปิด / 180 วินอกเวลา)
- **วันงบที่บันทึกเอง (manual) ชนะวันจาก Yahoo เสมอ** — `set_manual_earnings_date()` คือช่องทางที่ set_news.py ป้อนวันงบแม่นๆ เข้าระบบ
- `signal_score()` ให้คะแนน 0-15 (`SCORE_MAX`) — **น้ำหนักมากสุดคือความต่อเนื่องของการปรับขึ้น (6 คะแนน)** ซึ่งต้องพิสูจน์ด้วยเวลา: วันแรกหลังงบได้สูงสุด 11, ⭐⭐⭐ (13+) เป็นไปได้เฉพาะหุ้นที่ทะลุไฮ 3 เดือนและทำไฮต่อเนื่องจริง — ถ้าจะแก้สูตรคะแนน ต้องปรับ `stats.py` / `backtest.py` (BANDS) และเอกสารให้ตรงกัน

### telegram_bot.py — jobs ตามเวลา (ทั้งหมด Asia/Bangkok)

| เวลา | job |
|---|---|
| 07:00-09:45 และ 17:00-21:45 ทุก 10 นาที | poll ข่าวแจ้งงบจากเว็บ SET |
| 08:30 | heartbeat "✅ บอททำงานปกติ" |
| 08:45 | เตือนหุ้นใน watchlist ที่งบออกวันนี้/พรุ่งนี้ — มี startup fallback (หน่วง 360 วิ, ก่อน 16:30) แบบเดียวกับสรุปงบเช้า |
| 08:55 | สรุปงบเช้า — รวบหุ้นแจ้งงบตั้งแต่เย็นวาน+เช้านี้เป็นข้อความเดียว |
| 10:30 | ยืนยันรอบเช้า — หุ้นงบโตแรงเมื่อคืน แบ่ง ✅ ตลาดยืนยัน / 😐 / ❌ ตัดทิ้ง ตาม %วันนี้+วอลุ่มสะสม (ไม่มีตัวโตแรง = ไม่ส่ง) |
| 10:00-17:00 ทุก 15 นาที | เช็ค watchlist — เด้งเฉพาะตอนสถานะเปลี่ยน (🔥 ทะลุไฮ 3 ด. / 📈 ไฮใหม่ / ⛔ หลุด Low ก่อนงบ) |
| 17:30 | สแกนอัตโนมัติหลังปิดตลาด |
| ตอนเปิดบอท | startup catch-up: ดูจากอายุข้อมูลข่าว (mtime ของ `news_seen.json` — ห้ามใช้ `last_alive.json` เพราะโดน alive_job เขียนทับก่อน catch-up อ่าน) ถ้าเก่าเกิน 20 ชม. ดึงข่าวย้อนหลัง (สูงสุด 7 วัน) มาเก็บตกวันงบ |
| ตอนเปิดบอท (หน่วง 300 วิ) | startup digest: ส่งสรุปงบเช้าถ้าวันนี้ยังไม่ได้ส่ง (รองรับผู้ใช้เปิดคอมสาย ~09:30 เกินเวลา job 08:55) — ยืนยันรอบเช้าก็มี fallback แบบเดียวกัน (หน่วง 330 วิ, เฉพาะช่วง 10:30-12:00) |

- ถ้าดึงข่าว SET ล้มเหลวติดต่อกัน ≥3 รอบ (~30 นาที) บอทเด้ง ⚠️ เตือนผู้ใช้เองครั้งเดียวต่อการล่มหนึ่งช่วง และแจ้ง ✅ เมื่อกลับมาปกติ (ตัวนับใน memory ของ `news_monitor_job`)
- คำสั่งผู้ใช้เป็น**ข้อความภาษาไทยธรรมดา** (สแกน / ติดตาม XXX / ลิสต์ / สถิติ / งบ XXX วันที่ / ข่าวงบ / สรุปงบ N / ยืนยัน / พอร์ต N / ไม้ XXX / ซื้อ-ขาย XXX ราคา / เทรด) ผ่าน `handle_text()` — ไม่ใช่ slash command และทุกคำสั่งมี alias อังกฤษ (scan/watch/unwatch/list/stats/earn/news/digest/confirm/port/size/buy/sell/trades) เช็คแบบ case-insensitive — เพิ่มคำสั่งใหม่ต้องมี alias อังกฤษด้วย
- position sizing (`ไม้ XXX`): เสี่ยง `RISK_PCT_PER_TRADE` (1%) ของพอร์ตต่อไม้ ÷ ระยะราคาเข้า→เส้น ⛔ (pre_earn_low) ปัดลง lot ละ 100 — ขนาดพอร์ตเก็บต่อ chat ใน `port_settings.json`
- trade journal (`ซื้อ/ขาย/เทรด`): เก็บลง `trades_log.csv` แบบ append-only ผ่าน `stock_core.log_trade` — แถวซื้อเก็บบริบท ณ ตอนเข้า (วันงบ/เส้น ⛔/คะแนน) เพราะย้อนหลังไม่ได้ ส่วนกำไร/R คำนวณตอนอ่านจาก `pair_trades` (หนึ่งไม้เปิดต่อหุ้นต่อ chat) — `stats.build_stats_report(html, chat_id)` ต่อท้ายส่วน "ผลไม้จริง" เทียบตามช่วงคะแนน
- ข้อความ Telegram เป็น HTML, จำกัด 3900 ตัวอักษร/ข้อความ — ส่งข้อความยาวต้องใช้ `_reply_long` / `_send_long`
- แนวคิดสามคำสั่งหลัก: **สแกนหา → ติดตามเฝ้า → ลิสต์ดู** — เฉพาะ "ติดตาม" เท่านั้นที่ทำให้บอทเฝ้าและเด้งเตือน
- สุขอนามัยลิสต์: `build_watchlist_summary` คืน `(text, stale)` — ตัวที่โดน ⛔ / งบเกิน 60 วัน / ไม่รู้วันงบ ติดป้าย 🗑 พร้อมปุ่มลบ (callback `unwatch:SYM`) ยกเว้นตัวที่งบรอบใหม่ ≤14 วัน (ติดตามรองบ)

### set_news.py — จุดเปราะบางที่สุดของระบบ

เว็บ SET มีระบบกันบอท (Incapsula) — ต้องเปิด Chromium จริงผ่าน Playwright แล้ว**ดัก request ของ SPA เองมา rewrite query** (fromDate/toDate/perPage) เพราะยิง API ตรงๆ โดน 401 ถ้าเว็บ SET เปลี่ยนโครงสร้าง โมดูลนี้คือที่แรกที่พัง ข้อผิดพลาดต้องไม่ทำให้บอทหลักล้ม (โยน exception ให้ผู้เรียกจัดการ)

- `check_new_earnings_news()` นอกจากบันทึกวันงบ/ตัวเลข F45 แล้ว ยังต่อท้าย `filings_log.csv` (ทุกข่าวงบที่ยังไม่เคยเห็น — ดิบกว่า `earnings_results.csv` เพราะเก็บแม้ตัวที่อ่านตัวเลขไม่ได้) ใช้เป็นแหล่งข้อมูลของ "สรุปงบเช้า"
- `load_results_since(dt)` / `load_filings_since(dt)` อ่านย้อนหลังจาก CSV ล้วนๆ (ไม่ยิง Playwright) ให้ `build_morning_digest` ใน telegram_bot.py — ไฟล์ไม่มี/แถวเสียต้องคืนค่าว่าง ห้ามโยน exception

### scanner.py — สามโหมด (เลือกอัตโนมัติผ่าน `run_best_scan` ใน telegram_bot)

1. **โหมดข่าวแจ้งงบ (`run_filings_scan` — โหมดหลัก ตั้งแต่ 2026-07-12):** candidate = บริษัทที่แจ้งงบใน 7 วัน จาก `filings_log.csv` + `stock_core.symbols_with_recent_earnings()` (คลังวันงบ รวมที่ผู้ใช้บันทึกเองผ่านคำสั่ง "งบ") แล้วยืนยันทุกตัวด้วยข้อมูลสดจาก Yahoo ตามเกณฑ์เดิม — ครอบคลุมทุกบริษัทที่แจ้งงบจริง ไม่พึ่งความสดของ parquet และประหยัด request มหาศาล (แนวเดิมไล่หาราคาพุ่งทั้งตลาดแล้วยิง Yahoo พิสูจน์ว่า "ไม่เกี่ยวกับงบ" ทีละตัว ~83% ของ candidate ช่วงนอกฤดูงบ) ถ้ามี parquet จะใช้ `_parquet_prefilter` ช่วยคัดหยาบ — กติกาความปลอดภัย: ตัด candidate ได้เฉพาะเมื่อ parquet ครอบ **2 วันซื้อขายแรกตั้งแต่วันแจ้งงบครบ** (วันตอบรับจริงคือ 1 ใน 2 วันนั้น ดู `_earnings_day_reaction`) และใช้เช็คแบบ superset เท่านั้น **ใช้ได้เมื่อข้อมูลข่าวสด** (`set_news.news_data_age_hours()` ≤ 36 ชม.) ไม่สด = คืน None ให้ถอยโหมดถัดไป (fail open — เว็บ SET ล่มต้องยังสแกนได้)
2. โหมดทั้งตลาด (`run_full_scan` — สำรอง): คัดหยาบจาก parquet cache ของโปรเจกต์ Trading_Dashboard (~883 ตัว ไม่ยิง Yahoo) แล้วยืนยันเฉพาะตัวที่เข้าเกณฑ์ด้วยข้อมูลสด — ข้อมูล dashboard เก่าเกิน 5 วันจะ fallback ต่อ path ของ cache ตั้งผ่าน env var `DASHBOARD_TH_CACHE` ใน `.env` (เว้นว่าง = ข้ามโหมดนี้)
3. โหมด universe (`run_scan` — สำรองสุดท้าย): รายชื่อใน `scan_universe.txt` (~94 ตัว) ยิง Yahoo ทีละตัว

ทุกโหมดเขียน `scan_log.csv` schema เดียวกัน — เทียบสถิติก่อน/หลังเปลี่ยนวิธีได้จากคอลัมน์ `scan_date` (จุดตัด 2026-07-12) ฝั่ง `news_monitor_job` มี self-healing: คำนวณ `days_back` จากอายุข้อมูลข่าว เพื่อไม่ให้ `filings_log.csv` มีรูช่วงเว็บล่มข้ามวันขณะบอทเปิดอยู่ (สแกนโหมดหลักพึ่งความครบของไฟล์นี้)

## ไฟล์ข้อมูล/สถานะ (gitignore ทั้งหมด — อย่า commit)

`.env` (BOT_TOKEN), `watchlist.json`, `chat_ids.json`, `news_seen.json`, `last_alive.json`, `watch_state.json`, `scan_log.csv`, `lookup_log.csv`, `backtest_results.csv`, `bot_log.txt`, `.yf_cache/`, `filings_log.csv` (ใครแจ้งงบเมื่อไหร่ — ดิบกว่า earnings_results.csv รวมตัวที่อ่านตัวเลขไม่ได้ด้วย), `digest_state.json` (กันส่งสรุปงบเช้า/ยืนยันรอบเช้า/เตือนวันงบซ้ำในวันเดียว — key `last_sent` / `confirm_last_sent` / `remind_last_sent`), `port_settings.json` (ขนาดพอร์ตต่อ chat สำหรับคำนวณขนาดไม้), `trades_log.csv` (บันทึกไม้จริง — append-only เหมือน scan_log)

ไฟล์ log แบบ append (scan_log, lookup_log) คือข้อมูลสะสมที่ใช้วิเคราะห์ย้อนหลัง — เปลี่ยน schema คอลัมน์ต้องระวังไฟล์เก่าที่มีอยู่

## เอกสารประกอบ (ต้องอัปเดตให้ตรงเมื่อแก้พฤติกรรมระบบ)

- `คู่มือการใช้งาน.txt` — คู่มือผู้ใช้ฉบับเต็ม
- `วงจรระบบ.txt` — อะไรทำงานเมื่อไหร่ บอทต้องเปิดตอนไหน ข้อมูลประเภทไหนหายได้/ไม่หาย
- `workflow เทรดหลังงบ.txt` — กติกาการตัดสินใจเทรด (จุดเข้า/จุดตัดขาดทุน) ที่ระบบนี้ออกแบบมารองรับ
