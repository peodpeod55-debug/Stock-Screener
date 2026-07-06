# Per-User Watchlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ให้บอทตัวเดียวรองรับ watchlist ส่วนตัวแยกตาม chat_id ในขณะที่ข้อมูลตลาด (สแกน/สรุปงบเช้า/heartbeat) ยัง broadcast เหมือนเดิม

**Architecture:** เปลี่ยน `watchlist.json` จาก list แบนเป็น dict `{chat_id: [symbols]}`; `stock_core` watchlist funcs รับ `chat_id`; เพิ่ม `get_all_watched_symbols()`/`get_watchers()`/`migrate_legacy_watchlist()`; `telegram_bot` route การเตือน watchlist/งบเช้าไปเฉพาะเจ้าของ ส่วน `watch_state.json` คงเป็น global-per-symbol เดิม

**Tech Stack:** Python 3.11+, python-telegram-bot 22.7, ไม่มี pytest — verify ด้วยสคริปต์รันตรง (monkeypatch `stock_core._WATCHLIST_PATH` เป็นไฟล์ temp)

## Global Constraints

- ภาษาในโค้ด: comment/UI เป็นภาษาไทย เขียนสไตล์เดียวกับของเดิม
- ห้ามแตะ format ของ `watch_state.json` (คง `{symbol: state}`)
- ไฟล์ข้อมูลทั้งหมด gitignore อยู่แล้ว — ห้าม commit `watchlist.json`
- ทุก request Yahoo ผ่าน `stock_core` เดิม (ห้ามยิงเอง)
- chat_id เป็น key ของ JSON → normalize เป็น `str()` ทุกจุด
- Verify scripts เขียนในโฟลเดอร์ scratchpad ไม่ commit เข้า repo

---

### Task 1: `stock_core.py` — watchlist API แบบ per-user

**Files:**
- Modify: `stock_core.py:361-397` (ทั้ง block watchlist)

**Interfaces:**
- Consumes: `_base_symbol()` (มีอยู่แล้ว), `json` (import แล้ว), `_WATCHLIST_PATH`
- Produces:
  - `get_watchlist(chat_id) -> list[str]`
  - `add_to_watchlist(ticker_input: str, chat_id) -> tuple[str, list[str]]`
  - `remove_from_watchlist(ticker_input: str, chat_id) -> tuple[str|None, list[str]]`
  - `get_all_watched_symbols() -> list[str]`
  - `get_watchers(symbol: str) -> list[str]`  (คืน chat_id เป็น str)
  - `migrate_legacy_watchlist(chat_ids) -> bool`

- [ ] **Step 1: แทน block watchlist เดิม (บรรทัด 361-397) ด้วยโค้ดใหม่**

```python
# ── watchlist (หุ้นที่ติดตามหลังงบออก) — แยกตามผู้ใช้ (chat_id) ──
# โครงสร้างไฟล์: {"<chat_id>": ["AOT", ...]}  (key เป็น string ตาม JSON)

_WATCHLIST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "watchlist.json"
)


def _load_watchlists() -> dict:
    """คืน dict {chat_id_str: [symbols]} เสมอ
    ถ้าไฟล์ยังเป็น format เดิม (list) คืน {} — รอ migrate_legacy_watchlist()"""
    try:
        with open(_WATCHLIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_watchlists(data: dict):
    with open(_WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def get_watchlist(chat_id):
    """หุ้นที่ chat นี้ติดตาม (list ว่างถ้าไม่มี)"""
    return list(_load_watchlists().get(str(chat_id), []))


def add_to_watchlist(ticker_input: str, chat_id):
    base = _base_symbol(ticker_input)
    data = _load_watchlists()
    key = str(chat_id)
    symbols = data.get(key, [])
    if base not in symbols:
        symbols.append(base)
        data[key] = symbols
        _save_watchlists(data)
    return base, list(symbols)


def remove_from_watchlist(ticker_input: str, chat_id):
    base = _base_symbol(ticker_input)
    data = _load_watchlists()
    key = str(chat_id)
    symbols = data.get(key, [])
    if base in symbols:
        symbols.remove(base)
        data[key] = symbols
        _save_watchlists(data)
        return base, list(symbols)
    return None, list(symbols)


def get_all_watched_symbols():
    """union หุ้นของทุก chat (ให้ตัว monitor ดึงตัวละครั้ง)"""
    seen = []
    for syms in _load_watchlists().values():
        for s in syms:
            if s not in seen:
                seen.append(s)
    return seen


def get_watchers(symbol: str):
    """คืน list chat_id (str) ที่ติดตามหุ้นตัวนี้"""
    return [cid for cid, syms in _load_watchlists().items() if symbol in syms]


def migrate_legacy_watchlist(chat_ids) -> bool:
    """ไฟล์ format เดิม (list ไม่ว่าง) → ยกให้ทุก chat_id ที่ส่งมา
    ทำครั้งเดียว idempotent (dict อยู่แล้วไม่ทำอะไร) คืน True ถ้า migrate จริง"""
    try:
        with open(_WATCHLIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    if isinstance(data, list) and data and chat_ids:
        _save_watchlists({str(cid): list(data) for cid in chat_ids})
        return True
    return False
```

- [ ] **Step 2: เขียน verify script**

สร้าง `<scratchpad>/t1_watchlist.py`:

```python
import json, os, tempfile, importlib
import stock_core

tmp = tempfile.mkdtemp()
stock_core._WATCHLIST_PATH = os.path.join(tmp, "watchlist.json")

# แยกลิสต์ตาม chat
stock_core.add_to_watchlist("AOT", 111)
stock_core.add_to_watchlist("PTT", 222)
stock_core.add_to_watchlist("aot", 111)  # ซ้ำ (case-insensitive ผ่าน _base_symbol)
assert stock_core.get_watchlist(111) == ["AOT"], stock_core.get_watchlist(111)
assert stock_core.get_watchlist(222) == ["PTT"], stock_core.get_watchlist(222)

# union + watchers
assert set(stock_core.get_all_watched_symbols()) == {"AOT", "PTT"}
assert stock_core.get_watchers("AOT") == ["111"], stock_core.get_watchers("AOT")

# remove ไม่ข้ามคน
removed, remaining = stock_core.remove_from_watchlist("AOT", 222)
assert removed is None and stock_core.get_watchlist(111) == ["AOT"]
removed, remaining = stock_core.remove_from_watchlist("AOT", 111)
assert removed == "AOT" and stock_core.get_watchlist(111) == []

# migration: list เดิม -> dict ยกให้เจ้าของ
with open(stock_core._WATCHLIST_PATH, "w", encoding="utf-8") as f:
    json.dump(["CPALL", "KBANK"], f)
assert stock_core.migrate_legacy_watchlist([808446026]) is True
assert stock_core.get_watchlist(808446026) == ["CPALL", "KBANK"]
assert stock_core.migrate_legacy_watchlist([808446026]) is False  # idempotent

print("T1 PASS")
```

- [ ] **Step 3: รัน verify**

Run: `python <scratchpad>/t1_watchlist.py`
Expected: `T1 PASS` (ไม่มี AssertionError)

- [ ] **Step 4: Commit**

```bash
git add stock_core.py
git commit -m "feat: per-user watchlist API in stock_core"
```

---

### Task 2: `telegram_bot.py` — คำสั่ง watchlist + ปุ่ม ➕ ผูก chat_id

**Files:**
- Modify: `telegram_bot.py:868` (on_button), `:982-984` (build_watchlist_summary), `:1125` (ติดตาม), `:1138` (เลิกติดตาม), `:1150` (ลิสต์)

**Interfaces:**
- Consumes จาก Task 1: `get_watchlist(chat_id)`, `add_to_watchlist(t, chat_id)`, `remove_from_watchlist(t, chat_id)`
- Produces: `build_watchlist_summary(chat_id) -> str`

- [ ] **Step 1: `on_button` (บรรทัด 868) ใส่ chat_id ของคนกด**

แทน:
```python
        base, symbols = stock_core.add_to_watchlist(data.split(":", 1)[1])
```
ด้วย:
```python
        base, symbols = stock_core.add_to_watchlist(
            data.split(":", 1)[1], update.effective_chat.id
        )
```

- [ ] **Step 2: `build_watchlist_summary` (บรรทัด 982-984) รับ chat_id**

แทนหัวฟังก์ชันและบรรทัดโหลด symbols:
```python
def build_watchlist_summary(chat_id) -> str:
    """สรุปหุ้นที่ติดตาม เรียงตามแรงตอบรับวันงบ (มาก → น้อย)"""
    symbols = stock_core.get_watchlist(chat_id)
```

- [ ] **Step 3: handler `ติดตาม` (บรรทัด 1124-1126) ใส่ chat_id**

แทน:
```python
        for t in tickers[1:]:
            base, _ = stock_core.add_to_watchlist(t)
            added.append(base)
```
ด้วย:
```python
        for t in tickers[1:]:
            base, _ = stock_core.add_to_watchlist(t, update.effective_chat.id)
            added.append(base)
```

- [ ] **Step 4: handler `เลิกติดตาม` (บรรทัด 1138) ใส่ chat_id**

แทน:
```python
        removed, remaining = stock_core.remove_from_watchlist(tickers[1])
```
ด้วย:
```python
        removed, remaining = stock_core.remove_from_watchlist(
            tickers[1], update.effective_chat.id
        )
```

- [ ] **Step 5: handler `ลิสต์` (บรรทัด 1150) ส่ง chat_id เข้า summary**

แทน:
```python
        result = await asyncio.to_thread(build_watchlist_summary)
```
ด้วย:
```python
        result = await asyncio.to_thread(
            build_watchlist_summary, update.effective_chat.id
        )
```

- [ ] **Step 6: Verify import + summary ว่าง**

Run:
```bash
python -c "import os; os.environ['PYTHONIOENCODING']='utf-8'; import telegram_bot as t; print('TOKEN OK' if t.BOT_TOKEN else 'NO TOKEN'); print(t.build_watchlist_summary(999)[:40])"
```
Expected: พิมพ์ `TOKEN OK` แล้วตามด้วยข้อความ "📋 ยังไม่มีหุ้นในลิสต์" (chat 999 ลิสต์ว่าง) ไม่มี TypeError

- [ ] **Step 7: Commit**

```bash
git add telegram_bot.py
git commit -m "feat: route watchlist commands and add-button per chat_id"
```

---

### Task 3: `telegram_bot.py` — monitor เตือน watchlist route แยกคน

**Files:**
- Modify: `telegram_bot.py:724-778` (check_watchlist_changes), `:781-802` (watchlist_monitor_job)

**Interfaces:**
- Consumes จาก Task 1: `get_all_watched_symbols()`, `get_watchlist(chat_id)`
- Produces: `check_watchlist_changes() -> dict[str, list[str]]`  (`{symbol: [ข้อความ]}`)

- [ ] **Step 1: `check_watchlist_changes` — เดิน union, คืน dict, prune state**

แทนทั้งฟังก์ชัน (บรรทัด 724-778) ด้วย:

```python
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
```

หมายเหตุ logic: เดิม `fired` flag คุมว่าเตือน "ไฮใหม่" เฉพาะตอนไม่มีเตือนอื่น — แทนด้วย `not msgs`
ให้ผลเหมือนเดิม (⛔ กับ 🔥/✅ ยัง append ได้พร้อมกัน, 📈 เฉพาะตอนยังไม่มีอะไรใน msgs)

- [ ] **Step 2: `watchlist_monitor_job` — route ต่อ chat**

แทนทั้งฟังก์ชัน (บรรทัด 781-802) ด้วย:

```python
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
```

- [ ] **Step 3: Verify routing ด้วย state จำลอง (ไม่ยิง Yahoo)**

สร้าง `<scratchpad>/t3_route.py`:

```python
import os, tempfile
import stock_core

tmp = tempfile.mkdtemp()
stock_core._WATCHLIST_PATH = os.path.join(tmp, "watchlist.json")
stock_core.add_to_watchlist("AOT", 111)
stock_core.add_to_watchlist("PTT", 222)

# จำลองผลลัพธ์จาก check_watchlist_changes (AOT เท่านั้นที่มีเตือน)
alerts_by_symbol = {"AOT": ["🔥 AOT ทะลุไฮ"]}

def route(cid):
    msgs = []
    for sym in stock_core.get_watchlist(cid):
        msgs.extend(alerts_by_symbol.get(sym, []))
    return msgs

assert route(111) == ["🔥 AOT ทะลุไฮ"], route(111)   # คนติดตาม AOT ได้
assert route(222) == [], route(222)                    # คนติดตาม PTT ไม่ได้
print("T3 PASS")
```

Run: `python <scratchpad>/t3_route.py`
Expected: `T3 PASS`

- [ ] **Step 4: Verify import ไม่พัง**

Run: `python -c "import telegram_bot; print('import ok')"`
Expected: `import ok`

- [ ] **Step 5: Commit**

```bash
git add telegram_bot.py
git commit -m "feat: route watchlist monitor alerts per chat, prune stale state"
```

---

### Task 4: `telegram_bot.py` — เตือนงบเช้า/heartbeat per-user, cosmetic union, startup migration

**Files:**
- Modify: `telegram_bot.py:807-822` (build_earnings_reminder), `:825-843` (earnings_reminder_job), `:393-400` (heartbeat_job), `:416` (build_news_alert_text), `:482` (build_earnings_news_summary), `:1225` (main → migration)

**Interfaces:**
- Consumes จาก Task 1: `get_watchlist(chat_id)`, `get_all_watched_symbols()`, `migrate_legacy_watchlist(chat_ids)`

- [ ] **Step 1: `build_earnings_reminder` (บรรทัด 807-809) รับ chat_id**

แทน:
```python
def build_earnings_reminder():
    soon = []
    for sym in stock_core.get_watchlist():
```
ด้วย:
```python
def build_earnings_reminder(chat_id):
    soon = []
    for sym in stock_core.get_watchlist(chat_id):
```

- [ ] **Step 2: `earnings_reminder_job` (บรรทัด 832-843) วนต่อ chat**

แทน:
```python
    try:
        text = await asyncio.to_thread(build_earnings_reminder)
    except Exception:
        log.exception("earnings reminder failed")
        return
    if not text:
        return
    for cid in chat_ids:
        try:
            await context.bot.send_message(cid, text, parse_mode="HTML")
        except Exception:
            log.exception("send reminder failed (chat %s)", cid)
```
ด้วย:
```python
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
```

- [ ] **Step 3: `heartbeat_job` (บรรทัด 393-400) นับลิสต์ต่อคน**

แทน:
```python
    n_watch = len(stock_core.get_watchlist())
    text = (f"✅ บอททำงานปกติ • ลิสต์ติดตาม {n_watch} ตัว • "
            f"สแกนอัตโนมัติ {SCAN_HOUR:02d}:{SCAN_MINUTE:02d} น.")
    for cid in _load_chat_ids():
        try:
            await context.bot.send_message(cid, text)
        except Exception:
            pass
```
ด้วย:
```python
    for cid in _load_chat_ids():
        n_watch = len(stock_core.get_watchlist(cid))
        text = (f"✅ บอททำงานปกติ • ลิสต์ติดตาม {n_watch} ตัว • "
                f"สแกนอัตโนมัติ {SCAN_HOUR:02d}:{SCAN_MINUTE:02d} น.")
        try:
            await context.bot.send_message(cid, text)
        except Exception:
            pass
```

- [ ] **Step 4: cosmetic union ในข้อความ broadcast (บรรทัด 416 และ 482)**

`build_news_alert_text` บรรทัด 416 แทน:
```python
    watch = set(stock_core.get_watchlist())
```
ด้วย:
```python
    watch = set(stock_core.get_all_watched_symbols())  # union: มีคนติดตามอยู่
```

`build_earnings_news_summary` บรรทัด 482 แทน:
```python
    watch = set(stock_core.get_watchlist())
```
ด้วย:
```python
    watch = set(stock_core.get_all_watched_symbols())  # union: มีคนติดตามอยู่
```

- [ ] **Step 5: startup migration ใน `main` (ก่อน `app.run_polling`, บรรทัด 1225)**

แทรกก่อนบรรทัด `app.run_polling(drop_pending_updates=True)`:
```python
    # ย้าย watchlist.json format เดิม (list) → per-user dict ให้เจ้าของ (ครั้งเดียว)
    stock_core.migrate_legacy_watchlist(_load_chat_ids())
    app.run_polling(drop_pending_updates=True)
```

- [ ] **Step 6: Full smoke test**

Run:
```bash
python -c "import os; os.environ['PYTHONIOENCODING']='utf-8'; import telegram_bot as t; import inspect; assert 'chat_id' in inspect.signature(t.build_earnings_reminder).parameters; assert 'chat_id' in inspect.signature(t.build_watchlist_summary).parameters; print('signatures ok'); print('reminder empty ->', t.build_earnings_reminder(999)); print(t.build_message('PTT')[:60])"
```
Expected: `signatures ok`, reminder ว่าง (`None`) สำหรับ chat 999, แล้วข้อมูล PTT ตัวจริง — ไม่มี error

- [ ] **Step 7: ตรวจว่าไม่มี `get_watchlist()` ที่ยังไม่ใส่ arg หลงเหลือ**

Run: `grep -n "get_watchlist()" telegram_bot.py`
Expected: ไม่มีผลลัพธ์ (ทุกจุดใส่ chat_id หรือเปลี่ยนเป็น get_all_watched_symbols แล้ว)

- [ ] **Step 8: Commit**

```bash
git add telegram_bot.py
git commit -m "feat: per-user earnings reminder + heartbeat, startup migration, union markers"
```

---

### Task 5: ตรวจสอบด้วยหลายโมเดล + รวมเข้า master

**Files:** (ไม่มีการแก้โค้ดใหม่ — verification เท่านั้น)

- [ ] **Step 1: ให้ code-reviewer + security-auditor รีวิว diff `master..per-user-watchlist`**
  โฟกัส: correctness ของ routing, ไม่มี regression flow broadcast, watch_state prune ไม่ทำเตือนหาย, migration ปลอดภัย, ไม่มี secret หลุด

- [ ] **Step 2: แก้ตาม finding ที่ยืนยันแล้ว (ถ้ามี) แล้ว commit**

- [ ] **Step 3: รวมเข้า master + push**

```bash
git checkout master
git merge --ff-only per-user-watchlist
git push origin main
```

---

## Self-Review

**Spec coverage:**
- watchlist per-user (get/add/remove) → Task 1 ✓
- get_all_watched_symbols / get_watchers → Task 1 ✓
- migration → Task 1 (funcs) + Task 4 Step 5 (เรียกตอน startup) ✓
- คำสั่ง ติดตาม/เลิกติดตาม/ลิสต์/ปุ่ม ➕ per-user → Task 2 ✓
- monitor route + watch_state global + prune → Task 3 ✓
- earnings reminder per-user → Task 4 ✓
- heartbeat per-user + cosmetic union broadcast → Task 4 ✓
- ยัง broadcast: สแกน/สรุปงบเช้า (ไม่แตะ) ✓
- multi-model review → Task 5 ✓

**Placeholder scan:** ไม่มี TBD/TODO — โค้ดครบทุก step

**Type consistency:** `chat_id` ส่งเป็น int จาก `update.effective_chat.id` / `_load_chat_ids()`; ภายใน `stock_core` normalize เป็น `str()` ก่อนใช้เป็น key เสมอ; `get_watchers` คืน str; `check_watchlist_changes` คืน `dict[str, list[str]]` และ `watchlist_monitor_job` อ่านด้วย `.get(sym, [])` — ตรงกัน
