# คำสั่ง `วิเคราะห์` — ส่งข้อความ PHASE 0 ของ Gem เทคนิคเข้า Telegram

วันที่: 2026-08-12
สถานะ: อนุมัติดีไซน์แล้ว รอเขียน implementation plan

## ปัญหา

ทุกวันนี้บอทสแกนหาหุ้นตอบรับงบดีและส่งรายชื่อเข้า Telegram ได้แล้ว แต่ขั้นถัดไป —
เอาหุ้นตัวนั้นไปวิเคราะห์เทคนิคต่อ — ต้องทำด้วยมือทั้งหมด: เปิดเว็บ dashboard
(`https://tradingdashboard.sasukae00.workers.dev/ ` จากโปรเจกต์ `Trading view`) หาหุ้นให้เจอ
กดปุ่ม 📐 เพื่อ copy ข้อมูล PHASE 0 แล้วเปิด Gem เทคนิคไปวาง

ถ้าอยู่บนมือถือหรือไม่ได้เปิด dashboard ค้างไว้ ขั้นตอนนี้ขาดตอน

## สิ่งที่ทำ / ไม่ทำ

**ทำ:**

1. คำสั่ง `วิเคราะห์ XXX` / `ta XXX` — ตอบข้อความ PHASE 0 (ชุดเดียวกับปุ่ม 📐 บน dashboard)
   เป็นบล็อก `<pre>` ที่กดคัดลอกได้ พร้อมปุ่มลิงก์เปิด Gem เทคนิค
2. คำสั่ง `คำอธิบายงบ XXX` / `mda XXX` — ปุ่มลิงก์ไป `https://earningsradar.pages.dev/company/XXX/`
3. ปุ่ม 📐 / 📅 ต่อท้ายผลสแกน สรุปงบเช้า ยืนยันรอบเช้า และลิสต์ — ต่อจากสแกนได้โดยไม่ต้องพิมพ์ชื่อซ้ำ

**ไม่ทำ:**

- ไม่เปิดเบราว์เซอร์ ไม่ automate เว็บ SET/dashboard/Gemini — ดูหัวข้อ "ทางที่ไม่เลือก"
- ไม่เรียก Gemini API และไม่สรุปผลวิเคราะห์ให้เอง — บอทส่งข้อความให้ผู้ใช้ก๊อปเอง
- ไม่แตะโปรเจกต์ `Trading view` แม้แต่ไฟล์เดียว (อ่านผลลัพธ์ของมันอย่างเดียว)
- ไม่ทำ Gem วิเคราะห์พื้นฐาน (ปุ่ม 🧠 / prompt v2.1) — เอาเฉพาะเทคนิค
- ไม่คำนวณ indicator เอง ไม่ยิง Yahoo ตอนสั่งคำสั่งนี้

## ทางที่ไม่เลือก (และเหตุผล)

| ทาง | ทำไมไม่เอา |
|---|---|
| Playwright เปิด dashboard → กดปุ่ม 📐 → เปิดแท็บ Gem → paste → รอคำตอบ → ส่งกลับ | ปุ่ม 📐 คือ `taPrompt()` ฟังก์ชัน JS บริสุทธิ์ที่ประกอบข้อความจาก JSON ในหน้า — ไม่มีอะไรอยู่ฝั่งเซิร์ฟเวอร์ให้ไปเอา การเปิดเบราว์เซอร์จึงจ่ายราคาแพงเพื่อสิ่งที่อ่านไฟล์ตรง ๆ ได้ · ฝั่ง `gemini.google.com` ต้องล็อกอิน Google ค้าง เจอ bot detection และอ่านคำตอบที่สตรีมทีละตัวอักษรไม่นิ่ง — เปราะกว่า `set_news.py` ที่เป็นจุดเปราะที่สุดของระบบวันนี้ |
| เรียก Gemini API ด้วย prompt เดียวกับ Gem แล้วส่งผลวิเคราะห์กลับ | ทำได้จริง (Gem = ไฟล์ prompt + โมเดล และไฟล์ prompt อยู่ในเครื่อง) แต่ต้องมี API key + เสียโทเคนทุกครั้ง และ prompt v1.1 เป็น image-first — ต้องแนบรูป Weekly + Daily ที่ผู้ใช้ snip เอง จึงยังต้องมีคนอยู่ในลูปอยู่ดี |
| คำนวณ indicator เองใน Thai Trading จาก parquet ที่ `DASHBOARD_TH_CACHE` | ต้องเขียนสูตร SMA150/MACD signal/ATR/volume spike/RS ใหม่ทั้งชุดให้ตรงกับที่ dashboard คำนวณอยู่แล้ว และยังได้ไม่ครบ — RS Rating กับ MSI Stage คิดจากทั้งตลาดพร้อมกัน ไม่ใช่จากหุ้นตัวเดียว |

## ข้อเท็จจริงที่ตรวจแล้ว (2026-08-12)

- `Trading view/site/data/manifest.json` → `build_id` → `data/<build_id>/{core.json, stocks-TH.json}`
  ทั้งบนดิสก์และผ่าน HTTPS (worker route รองรับ `/data/manifest.json` และ `/data/<hash>/*`)
- `stocks-TH.json` = `{"market": "TH", "stocks": [ {...} × 882 ]}` — มีครบทุก field ที่ `taPrompt()` ใช้:
  `t n c sma20 sma50 sma150 sma200 e20v e50v h52 l52 sh30 sl30 vr50 vmax3 rsi macdv atrc atrp
  rs r1m r3m ytd r1y ed st bis stw scn`
- `core.json` มี `asof` ระดับ build และ `markets.TH.{asof, indexName, intraday, bench}`
  โดย `bench.close` ยาว 504 แท่ง (พอสำหรับ RS 21/63 แท่ง)
- `ed` (วันประกาศงบ) เป็น `null` ทุกตัวในตลาด TH — `earnings_cache.json` ของ dashboard มีแต่หุ้น US
- ทั้งสองไฟล์ต้องอ่านด้วย `encoding="utf-8"` (ค่า default ของ Windows คือ cp1252 → `UnicodeDecodeError`)
- ตาราง `COMMANDS` ปัจจุบันมี 16 คำสั่ง ไม่มีตัวไหนชน `วิเคราะห์` / `ta` / `คำอธิบายงบ` / `mda`

## สถาปัตยกรรม

โค้ดใหม่ทั้งหมดอยู่ในโปรเจกต์ `Thai Trading` โปรเจกต์เดียว
`Trading view` เป็นแหล่งอ่านอย่างเดียว (payload JSON + `market_dashboard.js` ตอนรันเทสต์)

```
telegram_bot.py
  ├── _cmd_ta        (คำสั่ง "วิเคราะห์" / "ta" + callback ta:SYM)
  ├── _cmd_mda       (คำสั่ง "คำอธิบายงบ" / "mda")
  └── _watch_buttons (เพิ่มปุ่ม 📐 📅 ในแถวเดียวกับ 👀)
        │
        ├── ta_prompt.py            ← (stock, market, next_earn) → ข้อความ PHASE 0
        ├── infra/dashboard_feed.py ← หา payload → (stock: dict, market: dict)
        └── stock_core.next_earnings_date()  ← วันงบจาก SQLite (ไม่ยิงเน็ต)
```

### หน่วยที่ 1 — `infra/dashboard_feed.py`

หน้าที่เดียว: คืนข้อมูลของหุ้นหนึ่งตัวจาก payload ของ dashboard — ไม่รู้จัก Telegram ไม่รู้จักการจัดข้อความ

```python
class DashboardUnavailable(Exception): ...   # อ่าน payload ไม่ได้เลย

def load_stock(symbol: str, market: str = "TH") -> tuple[dict, dict]:
    """คืน (stock, market_meta) — โยน DashboardUnavailable ถ้าอ่าน payload ไม่ได้
    คืน (None, market_meta) ถ้าอ่าน payload ได้แต่ไม่มีหุ้นตัวนั้น"""
```

**ลำดับการหา payload:**

1. `DASHBOARD_SITE_DIR` (env) → `<dir>/data/manifest.json` → `<dir>/data/<build_id>/…`
2. ถ้าข้อ 1 ล้มเหลว **หรือ** `core.json.markets.TH.asof` เก่ากว่า `DASHBOARD_MAX_AGE_DAYS`
   (default 5 — ค่าเดียวกับ `scanner.LOCAL_MAX_AGE_DAYS` ที่ `scanner.py:54` ใช้ตัดสินความสด
   ของ dashboard อยู่แล้ว)
   → `DASHBOARD_URL` (env, default `https://tradingdashboard.sasukae00.workers.dev`) path เดียวกัน
3. ล้มทั้งคู่ → `DashboardUnavailable` พร้อมข้อความที่บอก path/URL ที่ลองไปแล้ว

ผลจากข้อ 2 ถูกใช้แม้ยังเก่า — ตัดสินความสดที่ชั้นข้อความ (ดูหน่วยที่ 4) ไม่ใช่ที่ชั้นนี้

**cache:** `manifest.json` เล็ก (≈1KB) อ่านใหม่ทุกครั้ง · `core.json` (276KB) + `stocks-TH.json` (1.8MB)
เก็บใน dict ระดับโมดูลโดยใช้ `(source, build_id)` เป็นกุญแจ — build ใหม่ `build_id` เปลี่ยน
cache หลุดเอง ไม่ต้องมี TTL ให้เดา · เก็บ build เดียว (แทนที่ของเก่าทิ้ง) กันบอทกินแรมสะสม
· แปลง `stocks` เป็น dict `{t: stock}` ตอนโหลด ไม่ต้องไล่ list ทุกครั้ง

**กติกาความปลอดภัย:**

- `manifest.json` ที่ `schema_version != 1` → `DashboardUnavailable` พร้อมข้อความว่าต้องอัปเดตโค้ด
  (ดีกว่าอ่านมั่วแล้วได้ตัวเลขผิดโดยไม่มีใครรู้)
- `build_id` ต้องเข้า `^[a-f0-9]{8,64}$` ก่อนเอาไปต่อ path — กัน path traversal จาก manifest ที่แปลกปลอม
- HTTP: timeout 10 วินาที ไม่ retry (ผู้ใช้กดใหม่ได้) ใช้ `urllib.request` จาก stdlib —
  **ไม่เพิ่ม dependency ใหม่** (`requests` ไม่ได้อยู่ใน `requirements.txt` ส่วน `curl_cffi`
  ที่มีอยู่ตั้งใจใช้ปลอมเป็น Chrome ให้ Yahoo ซึ่ง workers.dev ไม่ต้องการ)
- ทุก `open()` / `resp.content.decode()` ระบุ `utf-8`

### หน่วยที่ 2 — `stock_core.next_earnings_date()`

```python
def next_earnings_date(ticker_input: str) -> datetime.date | None:
    """วันประกาศงบถัดไปจากฐานข้อมูล — ไม่ยิงเน็ต

    วันที่บันทึกเอง/จากข่าว SET ชนะวันจาก Yahoo (กติกาเดียวกับ get_stock_data)
    """
    base = _base_symbol(ticker_input)
    all_dates, manual = _sqlite_earnings_dates(base)
    today = today_bkk()
    return (min((d for d in manual if d > today), default=None)
            or min((d for d in all_dates if d > today), default=None))
```

เป็นทางอ่านอย่างเดียวของตรรกะที่ `stock_core.py:883-884` ทำอยู่แล้ว ต่างกันที่ไม่เรียก
`_get_earnings_dates()` (ซึ่งยิง Yahoo เมื่อ cache เกิน 24 ชม.) — คำสั่ง `วิเคราะห์` จึงตอบทันที
และทำงานได้แม้เน็ตล่ม · ราคาที่จ่าย: หุ้นที่ยังไม่เคยถูกดูเลยจะไม่มีวันงบใน SQLite → ขึ้น `ไม่มีข้อมูล`
ซึ่งถูกต้องตามกติกาข้อ 1 ของ prompt (ห้ามแต่งตัวเลข)

ใช้ `today_bkk()` ไม่ใช่ `date.today()` ตามกติกาของโปรเจกต์

### หน่วยที่ 3 — `ta_prompt.py`

`build_ta_prompt(stock: dict, market: dict, next_earn: date | None) -> str`
รับ dict ล้วน ๆ ไม่แตะดิสก์ ไม่แตะเน็ต — เทสต์ได้ตรง ๆ

**เป็นการ port `taPrompt()` (`Trading view/market_dashboard.js:58`) มาเป็น Python แบบตัวต่อตัว**
ข้อความที่ออกต้องเท่ากับปุ่ม 📐 ทุกบรรทัด ยกเว้นบรรทัด ข.8 บรรทัดเดียว

ค่าคงที่ที่ต้อง port มาด้วย (ทั้งหมดอยู่ใน `market_dashboard.js`):

| ชื่อ | บรรทัด | ค่า |
|---|---|---|
| `STAGE_DEF` | 7-10 | `{5:'SpecialBull', 4:'ExtraBull', 3:'Bull', 2:'Accum', 1:'Recovery', 6:'Warning', 7:'Distribution', 8:'Bear', 0:'None'}` (ฝั่ง Python เก็บแค่ชื่อ ไม่เอาสี) |
| `TREND_W` | 18 | `w in 2..5 → 'Bull'` · `1 → 'Recovery'` · `0 หรือ 6 → 'Sideway'` · `None → 'ไม่ทราบ'` · อื่น → `'Bear'` |
| `MKT_LABEL` | 48 | `TH → 'SET (หุ้นไทย)'` (พอร์ตมาทั้ง dict เผื่ออนาคต) |
| `SCAN_LABEL` | 49 | `['VDU (volume dry-up)', 'Pocket Pivot', 'Buyable Gap-Up', 'ใกล้ 52WH ≤5%']` |
| `idxRetBars` | 51-58 | ผลตอบแทนดัชนีย้อน n แท่งจาก `market['bench']['close']` — ต้องมี `len > bars` ไม่งั้น `None` |

**จุดที่ JS กับ Python ต่างกันจนพลาดได้ (ต้องระวังตอน implement):**

1. **formatter `f(x, d)`** — JS ใช้ `Number(x).toLocaleString('en-US', {maximumFractionDigits: d})`
   คือ มีคอมมาคั่นหลักพัน ทศนิยม **ไม่เกิน** d ตำแหน่ง และตัดศูนย์ท้ายทิ้ง
   ค่า default ของ `Intl` คือ roundingMode `halfExpand` (ปัดครึ่งขึ้น) ต่างจาก `round()` ของ Python
   ที่ปัดครึ่งไปเลขคู่ → ใช้ `decimal.Decimal(str(x)).quantize(..., rounding=ROUND_HALF_UP)`
   แล้วค่อย format ด้วย `,` และตัด `0` ท้าย · `x is None` → `'ไม่มีข้อมูล'`
2. **`s.vmax3` เช็คด้วย truthiness ใน JS** — `[]` เป็น truthy ใน JS แต่ falsy ใน Python
   ต้องเขียน `if vmax3 is not None` ไม่ใช่ `if vmax3`
3. **`s.rs ?? 'ไม่มีข้อมูล'` และ `s.bis ?? '—'`** เป็น nullish coalescing — ค่า `0` ต้องยังแสดงเป็น `0`
   ไม่ใช่ตกไปเป็นข้อความ default (Python `or` จะพลาดตรงนี้)
4. **`s.n ? ' (' + s.n + ')' : ''`** — ชื่อบริษัทเป็นสตริงว่างต้องไม่ได้วงเล็บเปล่า
5. **`asof`** = `market['asof'] or core['asof'] or 'ไม่ทราบวันที่'` (ตามลำดับนั้น)

**เทมเพลตข้อความ** (คัดจาก `taPrompt()` — บรรทัด ข.8 คือจุดเดียวที่ต่าง):

```
วิเคราะห์เทคนิค {t}{ (n)} ตาม prompt — แนบรูป Weekly + Daily มาด้วย

=== PHASE 0 (ตอบครบแล้ว ไม่ต้องถามซ้ำ) ===
ก.1 ตลาด: SET (หุ้นไทย)
ก.2 TF ที่แนบ: [Weekly + Daily — แก้บรรทัดนี้ถ้าแนบไม่ครบ]
ก.3 ระดับราคา (ข้อมูล {asof} · {ระหว่างวัน แท่งยังไม่ปิด|ปิดตลาดแล้ว}):
  ราคาปัจจุบัน {c}
  Swing High/Low 30 แท่ง: {sh30} / {sl30}
  52W High/Low: {h52} / {l52}
  SMA20 {sma20} · SMA50 {sma50} · SMA150 {sma150} · SMA200 {sma200}
  EMA20 {e20v} · EMA50 {e50v}
ก.4 Volume: วันล่าสุด {vr50}× ของค่าเฉลี่ย 50 วัน · spike แรงสุดใน 3 เดือน {vmax3[1]}× เมื่อ {vmax3[0]} แท่งก่อน
ข.5 RSI(14) {rsi} · MACD {macdv[0]} / signal {macdv[1]} / histogram {macdv[2]}
ข.6 ATR(14) {atrc} ({atrp}% ของราคา)
ข.7 Relative Strength: 1 เดือน {alpha(r1m,21)} · 3 เดือน {alpha(r3m,63)} · RS Rating {rs}/99
ข.8 วันประกาศงบถัดไป: {next_earn จาก SQLite|ไม่มีข้อมูล}      ← จุดเดียวที่ต่างจากปุ่ม 📐
ค.9 ขนาดพอร์ต: [กรอกเอง] · risk ต่อ trade: 1%
ค.10 สถานะ: [มีของอยู่แล้ว / กำลังหาจุดเข้า — เลือกอย่างใดอย่างหนึ่ง]

=== ข้อมูลเสริมจากระบบ screener (นอก prompt) ===
Stage MSI: {st ชื่อ} (อยู่มา {bis} แท่ง) · Trend Weekly: {TREND_W(stw)}
สแกนที่ติด: {รายการจาก scn|ไม่ติดสแกนใด}
ผลตอบแทน: 1 เดือน {r1m}% · YTD {ytd}% · 1 ปี {r1y}%

หมายเหตุ: ตัวเลขทั้งหมดมาจากระบบ screener ณ {asof} ไม่ได้อ่านจากภาพ — ถ้ารูปกราฟสดกว่านี้ให้ยึดรูปและบอกความต่าง
```

**ค.9 / ค.10 ยังเป็น placeholder ให้ผู้ใช้แก้เอง** เหมือนฝั่งเว็บ — บอทรู้ขนาดพอร์ต
(`get_port_size`) และสถานะไม้ (trade journal) ก็จริง แต่ตัดสินใจไม่เอาเข้ามาในรอบนี้
เพื่อให้ข้อความสองฝั่งต่างกันแค่บรรทัดเดียว ทำให้เทสต์กัน drift อ่านง่ายและงานเล็กพอ

### หน่วยที่ 4 — handler และปุ่มใน `telegram_bot.py`

**`@_command("วิเคราะห์", "ta")`** (ไม่ใช่ `word_only` — ต้องมีชื่อหุ้นตามหลัง)

1. ไม่ระบุหุ้น → ตอบวิธีใช้
2. `dashboard_feed.load_stock(sym)` ใน `asyncio.to_thread` (อาจอ่านไฟล์ 1.8MB หรือยิง HTTP)
3. `stock_core.next_earnings_date(sym)`
4. `ta_prompt.build_ta_prompt(...)` → ประกอบเป็นข้อความ Telegram:

```
📐 ข้อมูล PHASE 0 · <b>AOT</b> (ข้อมูล 2026-08-11 · ระหว่างวัน)
กดค้างที่บล็อกด้านล่างเพื่อคัดลอก แล้ววางในแชท Gem พร้อมรูป Weekly + Daily

<pre>…ข้อความ PHASE 0 เต็ม (html.escape แล้ว)…</pre>
```
พร้อม `InlineKeyboardMarkup` ปุ่มเดียว: `📐 เปิด Gem เทคนิค` → `GEM_TA_URL`

- `<pre>` คือสิ่งที่ทำให้กดคัดลอกทั้งก้อนได้บน Telegram ทั้งมือถือและเดสก์ท็อป
  → เนื้อในต้อง `html.escape()` เสมอ
- **ส่งด้วย `message.reply_text()` ตรง ๆ ไม่ผ่าน `_reply_long`** เพราะ `_reply_long` ตัดข้อความ
  ตามบรรทัด ซึ่งจะผ่ากลาง `<pre>` แล้ว HTML พัง — ความยาวถูกจำกัดด้วยโครงสร้าง (ดูข้อถัดไป)
  จึงไม่มีทางถึงเพดาน
- ความยาว: เทมเพลตตายตัว + ตัวเลขมีขอบเขต + ตัดชื่อบริษัทที่ 60 ตัวอักษร → ≈1,400 ตัวอักษร
  เทียบเพดาน 3,900 · มีเทสต์ยืนยันขอบบนด้วยค่าสุดขั้ว
- ไม่ผ่าน outbox — เป็นการตอบคำสั่งผู้ใช้แบบทันทีเหมือน `ดูหุ้น` / `ไม้` ส่วน outbox
  มีไว้ให้ job ตามตารางที่ต้องทนบอทดับ

**`@_command("คำอธิบายงบ", "mda")`** — ประกอบ URL จากชื่อหุ้นที่ normalize แล้ว
(`https://earningsradar.pages.dev/company/{sym}/`, ผ่าน `urllib.parse.quote`) ตอบข้อความสั้น
พร้อมปุ่ม `📅 เปิด Earnings Radar — {sym}` · **ไม่ยิงเว็บ ไม่เช็คว่ามีหน้านั้นจริง** —
เป็นแค่การประกอบลิงก์ จึงไม่มีอะไรพังได้และตอบทันที (แบบเดียวกับปุ่ม 📅 `pbBtn` บน dashboard)

ชื่อไทยจำเป็นเพราะ `tests/bot/test_command_registry.py` บังคับว่าทุกคำสั่งต้องมีทั้ง alias ไทยและอังกฤษ

**ปุ่ม:**

- `_watch_buttons()` (นิยามที่ `telegram_bot.py:1366` ถูกเรียก **9 จุด**: สแกนสั่งเอง `:1925`,
  สแกนอัตโนมัติ 17:30 `:1451`, สรุปงบเช้า `:911`, ยืนยันรอบเช้า `:1069`, คำสั่ง `สรุปงบ`/`ยืนยัน`
  `:1962` `:1980`, และ startup fallback ทั้งสามตัว `:2361` `:2370` `:2381`)
  เปลี่ยนจากแถวละ 3 หุ้น เป็น **หุ้นละหนึ่งแถว 3 ปุ่ม**: `[👀 AOT] [📐 AOT] [📅 AOT]`
  — แก้จุดเดียวได้ครบทั้ง 9 จุด
- `ลิสต์` เพิ่มชุดปุ่ม 📐 ของหุ้นในลิสต์ (15 ตัวแรก เท่ากับ `_unwatch_buttons`) ต่อจากปุ่ม 🗑 เดิม
- callback `ta:SYM` เข้า router เดิมข้าง `unwatch:` (`telegram_bot.py:1414`) เรียก handler
  ตัวเดียวกับคำสั่ง · ปุ่ม 📅 เป็น URL button ไม่มี callback

### หน่วยที่ 5 — ตัวแปร env ใหม่

| ชื่อ | default | ความหมาย |
|---|---|---|
| `DASHBOARD_SITE_DIR` | ว่าง (= ข้ามไปใช้ HTTPS) | โฟลเดอร์ `site` ของ `Trading view` — ค่าที่จะตั้งบนเครื่องนี้: `C:\Users\arthi\Desktop\Trading view\site` |
| `DASHBOARD_URL` | `https://tradingdashboard.sasukae00.workers.dev` | ต้นทางสำรอง |
| `GEM_TA_URL` | `https://gemini.google.com/gems/view` | Gem เทคนิคของผู้ใช้ |

`GEM_TA_URL` **ห้าม hardcode ลงโค้ด** — Gem ผูกกับบัญชี Google รายคนและ repo นี้ขึ้น git
(เหตุผลเดียวกับที่ฝั่ง dashboard เก็บไว้ใน localStorage ไม่ใช่ในไฟล์ JS)
ค่าจริงของผู้ใช้ (`https://gemini.google.com/gem/<id ของ Gem ตัวเอง>`) ไปอยู่ใน `.env` ซึ่ง gitignore แล้ว
· `.env.example` ใส่แค่ชื่อคีย์กับคำอธิบาย

## Error handling

| กรณี | พฤติกรรม |
|---|---|
| อ่าน payload ไม่ได้ทั้งไฟล์และ HTTPS | ตอบข้อความบอกว่าอ่าน dashboard ไม่ได้ + path/URL ที่ลองไปแล้ว |
| `schema_version` ไม่ใช่ 1 | ปฏิเสธพร้อมบอกว่าต้องอัปเดตโค้ด — ไม่พยายามอ่านต่อ |
| `build_id` รูปแบบผิด | ปฏิเสธแบบเดียวกัน (กัน path traversal) |
| payload เก่ากว่า 5 วัน (HTTPS แล้วยังเก่า) | **ส่งให้** แต่ขึ้น `⚠️ ข้อมูลเก่า N วัน` ที่หัวข้อความ — swing ใช้ EOD ข้อมูลเก่า 1-2 วันยังใช้ได้ ให้ผู้ใช้ตัดสินเอง |
| ไม่มีหุ้นตัวนั้นใน payload | บอกตรง ๆ ว่าไม่มีในระบบ dashboard พร้อมวันที่ของ build ที่ใช้อยู่ — **ไม่ fallback ไปคำนวณเอง** เพราะจะได้ข้อความครึ่ง ๆ กลาง ๆ ผิดกติกาข้อ 1 ของ prompt |
| field ใด ๆ เป็น `null` / ไม่มีวันงบ | เขียน `ไม่มีข้อมูล` — ห้ามใส่ `0` หรือเว้นว่าง (Gem จะบอกเองว่าโมเดลไหนวิเคราะห์ไม่ได้) |
| HTTP ช้า/ล่ม | timeout 10 วิ ไม่ retry · รันใน `asyncio.to_thread` ไม่บล็อก event loop ของบอท |
| `bench` หายจาก `core.json` | ข.7 เขียน `ไม่มีข้อมูล` ไม่ throw |

ไม่มีกรณีไหนที่ทำให้ handler ตายหรือกระทบ job อื่น — `dashboard_feed` โยน exception ชนิดเดียว
(`DashboardUnavailable`) และ handler จับตัวนั้นตัวเดียว

## การทดสอบ

รันด้วย `python -m pytest` เหมือนชุดเดิม ไม่แตะเน็ตจริง ไม่ใช้ Playwright

1. **`tests/test_ta_prompt.py`**
   - golden: dict ของ `2S` (ค่าจริงจาก build 2026-08-12) → เทียบข้อความทั้งก้อนแบบตรงตัว
   - ทุก field เป็น `None` → ต้องขึ้น `ไม่มีข้อมูล` ทุกช่อง ไม่มี `None` / `nan` / `undefined` โผล่
   - เคสดักความต่าง JS↔Python: `rs=0` และ `bis=0` ต้องแสดง `0` · `vmax3=[]` ต้องไม่ crash ·
     `n=""` ต้องไม่ได้วงเล็บเปล่า · formatter ปัดครึ่ง (เช่น `2.345` ที่ d=2 → `2.35`)
   - `bench` หาย → ข.7 เป็น `ไม่มีข้อมูล`
   - ความยาวข้อความ Telegram สุดท้าย < 3,900 ด้วยค่าสุดขั้ว (ชื่อบริษัทยาวสุด ตัวเลขหลายหลัก)
2. **`tests/test_ta_prompt_matches_dashboard.py`** — เทสต์กัน drift ข้ามโปรเจกต์
   อ่าน `market_dashboard.js` (path จาก `DASHBOARD_JS` หรือเดาจาก `DASHBOARD_SITE_DIR`;
   ไม่มีไฟล์ → `pytest.skip` เพื่อให้ CI บน GitHub ผ่าน) ดึงหัวบรรทัด `ก.1 … ค.10` และบรรทัดหัวข้อ
   `=== … ===` ออกจาก template ใน `taPrompt()` แล้วเทียบกับผลของฝั่ง Python —
   ฝั่ง dashboard เพิ่ม/แก้บรรทัดเมื่อไหร่ เทสต์แดงทันที
   (ยกเว้นบรรทัด ข.8 ที่ตั้งใจให้ต่าง — เทียบเฉพาะหัวบรรทัด `ข.8 วันประกาศงบถัดไป:`)
   นี่คือราคาที่ต้องจ่ายของการ port และเป็นสิ่งที่ทำให้จ่ายแล้วคุ้ม
3. **`tests/test_dashboard_feed.py`** — manifest/payload ปลอมใน `tmp_path`
   - เจอในเครื่องและสด → ไม่แตะ HTTP เลย (mock `requests.get` แล้ว assert ว่าไม่ถูกเรียก)
   - ในเครื่องไม่มี / เก่าเกิน 5 วัน → ตกไป HTTP (mock) และได้ผล
   - ล้มทั้งคู่ → `DashboardUnavailable` และข้อความมี path กับ URL ที่ลอง
   - `schema_version = 2` และ `build_id = "../evil"` → ปฏิเสธทั้งคู่
   - `build_id` เดิม → อ่านไฟล์ครั้งเดียว (นับจำนวนครั้งที่เปิดไฟล์) · `build_id` ใหม่ → cache หลุด
   - ไฟล์มีอักษรไทย → อ่านผ่านบนเครื่องที่ default encoding ไม่ใช่ utf-8
4. **`tests/bot/`** — คำสั่งใหม่ 2 ตัวถูก `test_command_registry.py` เดิมบังคับ alias ไทย/อังกฤษ
   และห้าม alias ซ้ำโดยอัตโนมัติ · เพิ่มเทสต์ว่า `_watch_buttons` คืน 3 ปุ่มต่อหุ้นด้วย
   `callback_data` / `url` ที่ถูกต้อง และ handler ตอบข้อความที่มี `<pre>` เมื่อ payload ปกติ
   / ตอบข้อความอธิบายเมื่อ `DashboardUnavailable`

**ตรวจด้วยมือหลังเทสต์ผ่าน:** เปิดบอทจริง → `วิเคราะห์ AOT` → กดคัดลอกจากบล็อก →
วางในแชท Gem พร้อมรูป Weekly + Daily → Gem ต้องข้าม PHASE 0 ไปเริ่มวิเคราะห์เลย ไม่ถามกลับ
→ `mda ACE` → ปุ่มเปิดหน้า Earnings Radar ของ ACE ได้จริง

## เอกสารที่ต้องแก้ให้ตรง

- `CLAUDE.md` และ `AGENTS.md` (ไฟล์เดียวกัน ต่างแค่หัวไฟล์ — แก้คู่กันเสมอ):
  รายการคำสั่ง, ผังสถาปัตยกรรม (`ta_prompt.py`, `infra/dashboard_feed.py`), env ใหม่ 3 ตัว
- `.env.example` — คีย์ใหม่ 3 ตัวพร้อมคำอธิบาย (ไม่ใส่ค่าจริงของ Gem)
- `README.md` — หัวข้อ env และการต่อกับโปรเจกต์ `Trading view`
- `คู่มือการใช้งาน.txt` — วิธีใช้คำสั่ง `วิเคราะห์` / `คำอธิบายงบ` และ workflow กับ Gem

## ผลลัพธ์ที่คาดหวัง

สแกนเจอหุ้นตอบรับงบดี → กดปุ่ม 📐 ใต้ผลสแกนได้เลยโดยไม่ต้องพิมพ์ชื่อซ้ำ →
ได้ข้อความ PHASE 0 ที่กดคัดลอกได้จากในแชท Telegram → เปิด Gem จากปุ่มข้าง ๆ →
วางพร้อมรูปกราฟ → Gem วิเคราะห์ครบ 7 โมเดลโดยไม่ถามกลับ

ทำได้จากมือถือโดยไม่ต้องเปิดคอม และไม่ต้องเปิดหน้า dashboard ค้างไว้
