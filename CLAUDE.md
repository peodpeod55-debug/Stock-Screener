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
python shadow.py              # ไม้เงา — จำลองเทรดทุกตัวที่ติดสแกนตามกติกา ⛔/20 วัน
python backtest.py            # backtest ระบบคะแนนกับข้อมูล ~2 ปี (ใช้เวลา 5-10 นาที)
```

- `เริ่ม Bot.bat` — รันบอทพร้อม auto-restart ใน 15 วินาทีถ้า crash (Ctrl+C = ปิดปกติ ไม่วนกลับ)
- `ติดตั้งเปิดเองตอนบูต.bat` — ลงทะเบียนให้บอทเปิดเองตอนบูตเครื่อง
- `python -m pytest` — เทสต์ใน `tests/` (ไม่แตะเน็ต) ครอบคลุมเฉพาะส่วน "วิเคราะห์"/PHASE 0 (`ta_prompt`, `dashboard_feed`, `next_earnings_date`, ปุ่ม/handler ใหม่) + เมนู "/" (`test_commands_menu.py`) — โมดูลเก่ายังไม่มีเทสต์ ตรวจด้วยการรันสคริปต์นั้นตรงๆ (ทุกโมดูลรันเดี่ยวได้) · `tests/test_ta_prompt_matches_dashboard.py` รัน `taPrompt()` ตัวจริงจาก `market_dashboard.js` ใน Node มาเทียบ (skip ถ้าไม่มี node/ไฟล์ dashboard)

## สถาปัตยกรรม

```
telegram_bot.py   ← entry point: handler + scheduled jobs ทั้งหมด
  ├── stock_core.py   ← โมดูลกลาง: ดึงข้อมูล Yahoo, คำนวณสัญญาณ, คะแนน, watchlist, คลังวันงบ
  ├── scanner.py      ← สแกนหุ้นตอบรับงบ (เกณฑ์: ตอบรับ ≥2%, วอลุ่ม ≥1.5 เท่า)
  ├── set_news.py     ← ดึงข่าวแจ้งงบจากเว็บ SET ผ่าน Playwright
  ├── stats.py        ← สถิติจาก scan_log.csv (+สรุปไม้เงาจาก cache)
  ├── shadow.py       ← ไม้เงา: replay ทุกแถวใน scan_log เป็นเทรดจำลอง
  ├── ta_prompt.py    ← ข้อความ PHASE 0 สำหรับ Gem เทคนิค (port ตัวต่อตัวจาก taPrompt() ของ dashboard)
  └── dashboard_feed.py ← อ่าน payload ของ Trading_Dashboard (site/data ในเครื่อง → HTTPS) คืนหุ้นทีละตัว
stock_lookup.py       ← CLI ดูหุ้นรายตัว (ใช้ stock_core เหมือนบอท)
backtest.py           ← จำลองระบบคะแนนกับวันงบเก่าจาก Yahoo
tests/                ← pytest (ดู "คำสั่งที่ใช้บ่อย")
```

### ta_prompt.py + dashboard_feed.py — คำสั่ง "วิเคราะห์" (ปุ่ม 📐)

- โปรเจกต์ `Trading_Dashboard` (โฟลเดอร์ข้างเคียง / deploy ที่ `DASHBOARD_URL`) เป็น**แหล่งอ่านอย่างเดียว** — ห้ามแก้ไฟล์ของมันจากที่นี่ · โครง payload: `site/data/manifest.json` (`schema_version` 1, `build_id` hex) → `data/<build_id>/core.json` (`asof`, `markets.TH.{asof,indexName,intraday,bench.close}`) + `stocks-TH.json` (`{"stocks":[…]}` ~882 ตัว)
- `dashboard_feed.load_stock(sym, "TH", site_dir=…, url=…, max_age_days=…)` → `(stock | None, market_meta)` — ลำดับ: `DASHBOARD_SITE_DIR` ในเครื่อง → ถ้าไม่มี/เก่ากว่า `scanner.LOCAL_MAX_AGE_DAYS` (5 วัน) → HTTPS `DASHBOARD_URL` (ต้องส่ง User-Agent แบบเบราว์เซอร์ — Cloudflare error 1010 บล็อก `Python-urllib`) → HTTPS ล้มแต่ในเครื่องมีของเก่า → ใช้ของเก่าพร้อม `meta["stale"]=True` → ไม่มีอะไรเลย → โยน `DashboardUnavailable` (exception ชนิดเดียวที่ handler จับ) · `schema_version` ≠ 1 หรือ `build_id` ไม่ใช่ `^[a-f0-9]{8,64}$` → ปฏิเสธทันที ไม่ลองทางอื่น (กัน path traversal) · cache core+stocks ตาม `(source, build_id)` เก็บ build เดียว, manifest อ่านใหม่ทุกครั้ง · stdlib ล้วน ไม่อ่าน env เอง (telegram_bot ส่งค่ามาให้)
- `ta_prompt.build_ta_prompt(stock, meta, next_earn)` = `taPrompt()` ของ `market_dashboard.js` **ทุกบรรทัด ยกเว้น ข.8** (วันงบ — payload TH ไม่มี `ed` จึงใช้ `stock_core.next_earnings_date()` อ่านคลังวันงบในเครื่อง manual ชนะ auto ไม่ยิง Yahoo) — จุดที่ JS กับ Python ต่างจนพลาดได้: `f()` ต้องเท่ากับ `toLocaleString('en-US',{maximumFractionDigits:d})` (Decimal + ROUND_HALF_UP, คอมมา, ตัดศูนย์ท้าย) · `vmax3` เช็ค `is not None` ไม่ใช่ truthiness · `rs`/`bis` = 0 ต้องโชว์ 0 · ทุกค่า None → "ไม่มีข้อมูล" ห้ามใส่ 0/เว้นว่าง · **แก้เทมเพลตฝั่ง dashboard เมื่อไหร่ต้องแก้ที่นี่ให้ตรง** (drift test จะแดง)
- ฝั่งบอท: `_ta_reply(message, sym)` ใช้ร่วมระหว่างคำสั่ง `วิเคราะห์`/`ta` และ callback `ta:SYM` — ส่ง `<pre>` (html.escape แล้ว) ให้กดคัดลอกทั้งก้อน + ปุ่ม URL `GEM_TA_URL` · **ส่งด้วย `reply_text` ตรง ไม่ผ่าน `_reply_long`** (ตัดตามบรรทัดจะผ่ากลาง `<pre>`) — ยาว ≈1,400 ตัวอักษร มีเทสต์ขอบบน · ไม่มีหุ้นใน payload → บอกตรงๆ ไม่ fallback ไปคำนวณเอง · ข้อมูลเก่า → ส่งให้พร้อม ⚠️ ไม่ปฏิเสธ · บอทไม่วิเคราะห์เอง ไม่เรียก Gemini API — ผู้ใช้เอาบล็อกไปวางใน Gem พร้อมรูป Weekly+Daily
- env: `DASHBOARD_SITE_DIR` (โฟลเดอร์ `site` ของ Trading_Dashboard; ว่าง = ใช้เว็บ), `DASHBOARD_URL` (default `https://sakura.peodbot.com`), `GEM_TA_URL` (Gem ผูกบัญชี Google รายคน — **ห้าม hardcode ลงโค้ด/เอกสาร** อยู่ใน `.env` เท่านั้น; ว่าง = หน้ารายการ Gems) — อ่านผ่าน `scanner._env_value` · คำสั่ง `คำอธิบายงบ`/`mda XXX` แค่ประกอบลิงก์ `https://earningsradar.pages.dev/company/XXX/` ไม่ยิงเว็บ

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
| 08:30 | heartbeat "✅ บอททำงานปกติ" — ครั้งเดียวต่อวัน (key `alive_last_sent`) + startup fallback (หน่วง 25 วิ): เครื่องบูตสาย/รีสตาร์ทก็ยังได้ ✅ ของวันนั้น |
| 08:45 | เตือนหุ้นใน watchlist ที่งบออกวันนี้/พรุ่งนี้ — มี startup fallback (หน่วง 360 วิ, ก่อน 16:30) แบบเดียวกับสรุปงบเช้า |
| 08:55 | สรุปงบเช้า — รวบหุ้นแจ้งงบตั้งแต่เย็นวาน+เช้านี้เป็นข้อความเดียว |
| 10:30 | ยืนยันรอบเช้า — หุ้นงบโตแรงเมื่อคืน แบ่ง ✅ ตลาดยืนยัน / 😐 / ❌ ตัดทิ้ง ตาม %วันนี้+วอลุ่มสะสม (ไม่มีตัวโตแรง = ไม่ส่ง) |
| 10:00-17:00 ทุก 15 นาที | เช็ค watchlist — เด้งเฉพาะตอนสถานะเปลี่ยน (🔥 ทะลุไฮ 3 ด. / 📈 ไฮใหม่ / ⛔ หลุด Low ก่อนงบ) |
| 17:30 | สแกนอัตโนมัติหลังปิดตลาด — จบแล้วอัปเดต cache ไม้เงา (`shadow.update_shadow`) ต่อท้าย — ครั้งเดียวต่อวัน (key `scan_last_run`) + startup fallback (หน่วง 450 วิ, เฉพาะหลัง 17:30): เปิดคอมค่ำก็ไม่เกิดรูใน scan_log/ไม้เงา |
| 17:45 | รายงานไม้เปิด (เฉพาะ chat ที่มีไม้จริงเปิด): R ปัจจุบัน / % ถึงเส้น ⛔ / วันเงียบ + ⚠️ ตามกติกา workflow — มี startup fallback (หน่วง 390 วิ, เฉพาะหลัง 17:45) |
| อาทิตย์ 19:00 (job รายวัน) | ปฏิทินงบ 14 วันข้างหน้า จาก `stock_core.upcoming_earnings()` — กันซ้ำรายสัปดาห์ด้วย anchor "วันอาทิตย์ล่าสุด" (key `calendar_last_sent`) จึงลงเป็น job รายวัน + startup fallback (หน่วง 420 วิ): คอมปิดวันอาทิตย์ก็ส่งชดเชยครั้งแรกที่เปิดในสัปดาห์นั้น |
| ตอนเปิดบอท | startup catch-up: ดูจากอายุข้อมูลข่าว (mtime ของ `news_seen.json` — ห้ามใช้ `last_alive.json` เพราะโดน alive_job เขียนทับก่อน catch-up อ่าน) ถ้าเก่าเกิน 20 ชม. ดึงข่าวย้อนหลัง (สูงสุด 7 วัน) มาเก็บตกวันงบ |
| ตอนเปิดบอท (หน่วง 300 วิ) | startup digest: ส่งสรุปงบเช้าถ้าวันนี้ยังไม่ได้ส่ง (รองรับผู้ใช้เปิดคอมสาย ~09:30 เกินเวลา job 08:55) — ยืนยันรอบเช้าก็มี fallback แบบเดียวกัน (หน่วง 330 วิ, เฉพาะช่วง 10:30-12:00) |

- ถ้าดึงข่าว SET ล้มเหลวติดต่อกัน ≥3 รอบ (~30 นาที) บอทเด้ง ⚠️ เตือนผู้ใช้เองครั้งเดียวต่อการล่มหนึ่งช่วง และแจ้ง ✅ เมื่อกลับมาปกติ (ตัวนับใน memory ของ `news_monitor_job`)
- คำสั่งผู้ใช้เป็น**ข้อความภาษาไทยธรรมดา** (สแกน / ติดตาม XXX / ลิสต์ / สถิติ / เงา / งบ XXX วันที่ / ปฏิทิน / ข่าวงบ / สรุปงบ N / ยืนยัน / พอร์ต N / ไม้ XXX / ซื้อ-ขาย XXX ราคา / เทรด / วิเคราะห์ XXX / คำอธิบายงบ XXX) ผ่าน `handle_text()` → `_dispatch_text()` และทุกคำสั่งมี alias อังกฤษ (scan/watch/unwatch/list/stats/shadow/earn/calendar/news/digest/confirm/port/size/buy/sell/trades/ta/mda) เช็คแบบ case-insensitive
- เมนู "/" ใน Telegram (port จากบอท HK): `BOT_COMMANDS` ลงทะเบียนผ่าน `register_commands` (post_init — ล้มแค่ log ไม่ให้บอทตาย) และ slash ทุกตัวเป็นแค่ alias ผ่าน `cmd_alias` → ประกอบข้อความแล้วส่งเข้า `_dispatch_text` เดิม ไม่มี logic ซ้ำ (`SLASH_ALIASES` ชื่อ slash = alias อังกฤษ ยกเว้น `/price XXX` ตัดคำสั่งทิ้งเหลือ ticker เข้าโหมดดูหุ้นรายตัว) — **เพิ่มคำสั่งใหม่ต้องมี alias อังกฤษ + รายการใน `BOT_COMMANDS` ด้วย** (เทสต์ `tests/test_commands_menu.py` บังคับให้เมนูกับ alias ตรงกัน)
- position sizing (`ไม้ XXX`): เสี่ยง `RISK_PCT_PER_TRADE` (1%) ของพอร์ตต่อไม้ ÷ ระยะราคาเข้า→เส้น ⛔ (pre_earn_low) ปัดลง lot ละ 100 — ขนาดพอร์ตเก็บต่อ chat ใน `port_settings.json`
- trade journal (`ซื้อ/ขาย/เทรด`): เก็บลง `trades_log.csv` แบบ append-only ผ่าน `stock_core.log_trade` — แถวซื้อเก็บบริบท ณ ตอนเข้า (วันงบ/เส้น ⛔/คะแนน) เพราะย้อนหลังไม่ได้ ส่วนกำไร/R คำนวณตอนอ่านจาก `pair_trades` (หนึ่งไม้เปิดต่อหุ้นต่อ chat) — `stats.build_stats_report(html, chat_id)` ต่อท้ายส่วน "ไม้เงา" (จาก cache) และ "ผลไม้จริง" เทียบตามช่วงคะแนน
- ไม้เงา (`เงา` / shadow.py): ทุกแถวใน `scan_log.csv` = ไม้จำลอง (ครั้งแรกต่อ ticker+วันงบ) เข้า open วันถัดจากวันสแกน / ตัดปิดหลุด `pre_earn_low` / ครบ 20 วันทำการ — replay จากราคาย้อนหลังตามปรัชญา "คำนวณย้อนหลังได้เสมอ" ไม่มี job เฝ้า ไม้ปิดแล้ว cache ลง `shadow_log.csv` เรียกซ้ำยิง Yahoo เฉพาะไม้ที่ยังเปิด — BANDS ใช้ของ stats (lazy import ใน `_bands()` กัน import วนกับ stats ที่เรียก shadow)
- ข้อความ Telegram เป็น HTML, จำกัด 3900 ตัวอักษร/ข้อความ — ส่งข้อความยาวต้องใช้ `_reply_long` / `_send_long`
- แนวคิดสามคำสั่งหลัก: **สแกนหา → ติดตามเฝ้า → ลิสต์ดู** — เฉพาะ "ติดตาม" เท่านั้นที่ทำให้บอทเฝ้าและเด้งเตือน
- สุขอนามัยลิสต์: `build_watchlist_summary` คืน `(text, stale)` — ตัวที่โดน ⛔ / งบเกิน 60 วัน / ไม่รู้วันงบ ติดป้าย 🗑 พร้อมปุ่มลบ (callback `unwatch:SYM`) ยกเว้นตัวที่งบรอบใหม่ ≤14 วัน (ติดตามรองบ) — คีย์บอร์ดใต้ลิสต์ = แถว 🗑 ของตัวหมดสภาพ ต่อด้วยแถว 📐 (callback `ta:SYM`) ของทุกตัวในลิสต์ 15 ตัวแรก (`_list_buttons`)
- ปุ่มใต้ผลสแกน / สรุปงบ / ยืนยัน (`_watch_buttons`, เรียก 6 จุด): **หุ้นละแถว** `[➕ SYM watch:SYM] [📐 SYM ta:SYM] [📅 SYM url Earnings Radar]` — เพิ่มปุ่มต่อหุ้นให้แก้ที่ `_stock_row` จุดเดียว

### set_news.py — จุดเปราะบางที่สุดของระบบ

เว็บ SET มีระบบกันบอท (Incapsula) — ต้องเปิด Chromium จริงผ่าน Playwright แล้ว**ดัก request ของ SPA เองมา rewrite query** (fromDate/toDate/perPage) เพราะยิง API ตรงๆ โดน 401 ถ้าเว็บ SET เปลี่ยนโครงสร้าง โมดูลนี้คือที่แรกที่พัง ข้อผิดพลาดต้องไม่ทำให้บอทหลักล้ม (โยน exception ให้ผู้เรียกจัดการ)

- `check_new_earnings_news()` นอกจากบันทึกวันงบ/ตัวเลข F45 แล้ว ยังต่อท้าย `filings_log.csv` (ทุกข่าวงบที่ยังไม่เคยเห็น — ดิบกว่า `earnings_results.csv` เพราะเก็บแม้ตัวที่อ่านตัวเลขไม่ได้) ใช้เป็นแหล่งข้อมูลของ "สรุปงบเช้า"
- บริษัทยื่นแก้ไขงบได้ (headline มี "(แก้ไข") — `_log_f45_result` กันซ้ำด้วย (หุ้น, งวด, ปี) แบบเทียบตัวเลข/summary กับแถวล่าสุดของงวดนั้น: เหมือนเดิม = skip (อ่านฉบับเดิมซ้ำ), ต่าง = append แถวใหม่ (ไฟล์ยัง append-only) แล้วฝั่งอ่าน (digest/ยืนยันรอบเช้า) หยิบแถว news_datetime ล่าสุดต่อ symbol เอง — ป้าย " (📝 ฉบับแก้ไข)" ฝังใน summary (ไม่เพิ่มคอลัมน์ CSV) และ flag `corrected` ติดไปกับคิว `f45_backlog.json` ด้วย
- อ่านรายละเอียด F45 ได้สูงสุด `F45_DETAIL_MAX` (15) ฉบับต่อรอบ — ตัวที่เกินโควตา/โหลดหน้าไม่สำเร็จเข้าคิว `f45_backlog.json` แล้วทยอยอ่านด้วยโควตาที่เหลือของรอบถัดๆ ไปจนหมด (ตัวเลขจากคิวลง `earnings_results.csv` อย่างเดียว ไม่แจ้งเตือนซ้ำ — คิวเก็บ 7 วัน ลองซ้ำได้ 3 ครั้ง)
- `load_results_since(dt)` / `load_filings_since(dt)` อ่านย้อนหลังจาก CSV ล้วนๆ (ไม่ยิง Playwright) ให้ `build_morning_digest` ใน telegram_bot.py — ไฟล์ไม่มี/แถวเสียต้องคืนค่าว่าง ห้ามโยน exception

### scanner.py — สามโหมด (เลือกอัตโนมัติผ่าน `run_best_scan` ใน telegram_bot)

1. **โหมดข่าวแจ้งงบ (`run_filings_scan` — โหมดหลัก ตั้งแต่ 2026-07-12):** candidate = บริษัทที่แจ้งงบใน 7 วัน จาก `filings_log.csv` + `stock_core.symbols_with_recent_earnings()` (คลังวันงบ รวมที่ผู้ใช้บันทึกเองผ่านคำสั่ง "งบ") แล้วยืนยันทุกตัวด้วยข้อมูลสดจาก Yahoo ตามเกณฑ์เดิม — ครอบคลุมทุกบริษัทที่แจ้งงบจริง ไม่พึ่งความสดของ parquet และประหยัด request มหาศาล (แนวเดิมไล่หาราคาพุ่งทั้งตลาดแล้วยิง Yahoo พิสูจน์ว่า "ไม่เกี่ยวกับงบ" ทีละตัว ~83% ของ candidate ช่วงนอกฤดูงบ) ถ้ามี parquet จะใช้ `_parquet_prefilter` ช่วยคัดหยาบ — กติกาความปลอดภัย: ตัด candidate ได้เฉพาะเมื่อ parquet ครอบ **2 วันซื้อขายแรกตั้งแต่วันแจ้งงบครบ** (วันตอบรับจริงคือ 1 ใน 2 วันนั้น ดู `_earnings_day_reaction`) และใช้เช็คแบบ superset เท่านั้น **ใช้ได้เมื่อข้อมูลข่าวสด** (`set_news.news_data_age_hours()` ≤ 36 ชม.) ไม่สด = คืน None ให้ถอยโหมดถัดไป (fail open — เว็บ SET ล่มต้องยังสแกนได้)
2. โหมดทั้งตลาด (`run_full_scan` — สำรอง): คัดหยาบจาก parquet cache ของโปรเจกต์ Trading_Dashboard (~883 ตัว ไม่ยิง Yahoo) แล้วยืนยันเฉพาะตัวที่เข้าเกณฑ์ด้วยข้อมูลสด — ข้อมูล dashboard เก่าเกิน 5 วันจะ fallback ต่อ path ของ cache ตั้งผ่าน env var `DASHBOARD_TH_CACHE` ใน `.env` (เว้นว่าง = ข้ามโหมดนี้)
3. โหมด universe (`run_scan` — สำรองสุดท้าย): รายชื่อใน `scan_universe.txt` (~94 ตัว) ยิง Yahoo ทีละตัว

ทุกโหมดเขียน `scan_log.csv` schema เดียวกัน — เทียบสถิติก่อน/หลังเปลี่ยนวิธีได้จากคอลัมน์ `scan_date` (จุดตัด 2026-07-12) ฝั่ง `news_monitor_job` มี self-healing: คำนวณ `days_back` จากอายุข้อมูลข่าว เพื่อไม่ให้ `filings_log.csv` มีรูช่วงเว็บล่มข้ามวันขณะบอทเปิดอยู่ (สแกนโหมดหลักพึ่งความครบของไฟล์นี้)

ทุกโหมดดึงข้อมูลผ่าน `_fetch_all`: โดน Yahoo rate limit กลางคันพักครั้งเดียว (`RATE_LIMIT_PAUSE_S`) แล้วไล่ต่อจากตัวเดิม — โดนซ้ำถือว่าโควตาหมด ตัวที่เหลือคืนเป็น `rate_limited` แสดงแยกท้ายรายงาน (ไม่ปนกับ "ไม่มีข้อมูล" และไม่หายเงียบ)

## ไฟล์ข้อมูล/สถานะ (gitignore ทั้งหมด — อย่า commit)

`.env` (BOT_TOKEN, DASHBOARD_TH_CACHE, DASHBOARD_SITE_DIR, DASHBOARD_URL, GEM_TA_URL), `watchlist.json`, `chat_ids.json`, `news_seen.json`, `last_alive.json`, `watch_state.json`, `scan_log.csv`, `lookup_log.csv`, `backtest_results.csv`, `bot_log.txt`, `.yf_cache/`, `filings_log.csv` (ใครแจ้งงบเมื่อไหร่ — ดิบกว่า earnings_results.csv รวมตัวที่อ่านตัวเลขไม่ได้ด้วย), `digest_state.json` (กันส่งรายงานอัตโนมัติซ้ำ — key รายวัน `last_sent` / `confirm_last_sent` / `remind_last_sent` / `openpos_last_sent` / `alive_last_sent` / `scan_last_run` + รายสัปดาห์ `calendar_last_sent` เก็บ anchor วันอาทิตย์ล่าสุด), `port_settings.json` (ขนาดพอร์ตต่อ chat สำหรับคำนวณขนาดไม้), `trades_log.csv` (บันทึกไม้จริง — append-only เหมือน scan_log), `shadow_log.csv` (ไม้เงาที่ปิดแล้ว — cache สร้างใหม่ได้เสมอจาก scan_log), `f45_backlog.json` (คิว F45 ที่ยังไม่ได้อ่านตัวเลข — วันพีคเกินโควตาต่อรอบ)

ไฟล์ log แบบ append (scan_log, lookup_log) คือข้อมูลสะสมที่ใช้วิเคราะห์ย้อนหลัง — เปลี่ยน schema คอลัมน์ต้องระวังไฟล์เก่าที่มีอยู่

ไฟล์สถานะ JSON ทุกไฟล์เขียนผ่าน `stock_core.save_json_atomic` (เขียน .tmp แล้ว os.replace — ไฟดับกลางคันไฟล์เดิมยังอยู่) และไฟล์สำคัญ (chat_ids, watchlist, คลังวันงบ) อ่านผ่าน `stock_core.load_json_or_backup` (ไฟล์เสียถูกเก็บเป็น `.bak` ไว้กู้ ไม่ทับทิ้งเงียบๆ) — เขียนโค้ดใหม่ที่แตะไฟล์สถานะให้ใช้คู่นี้เสมอ

## เอกสารประกอบ (ต้องอัปเดตให้ตรงเมื่อแก้พฤติกรรมระบบ)

- `คู่มือการใช้งาน.txt` — คู่มือผู้ใช้ฉบับเต็ม
- `วงจรระบบ.txt` — อะไรทำงานเมื่อไหร่ บอทต้องเปิดตอนไหน ข้อมูลประเภทไหนหายได้/ไม่หาย
- `workflow เทรดหลังงบ.txt` — กติกาการตัดสินใจเทรด (จุดเข้า/จุดตัดขาดทุน) ที่ระบบนี้ออกแบบมารองรับ
