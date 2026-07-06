# Design: แยก watchlist per-user (บอทตัวเดียวรองรับพอร์ตส่วนตัวหลายคน)

วันที่: 2026-07-06

## ปัญหา / เป้าหมาย

บอทรัน 1 instance (บน VPS) แล้วหลายคน chat ด้วยได้อยู่แล้ว — job ทุกตัว broadcast
หา `_load_chat_ids()` ครบ แต่ **watchlist เป็นของกลาง** (`watchlist.json` เป็น list แบน
ไฟล์เดียว) ใครพิมพ์ `ติดตาม/เลิกติดตาม` กระทบทุกคน

เป้าหมาย: ให้ **watchlist และการเตือนที่ผูกกับ watchlist แยกตามผู้ใช้ (chat_id)** โดยที่
ข้อมูลระดับตลาด (สแกน / สรุปงบเช้า / heartbeat) ยัง broadcast เหมือนเดิม

## ขอบเขต

**แยก per-user:** คำสั่ง `ติดตาม` / `เลิกติดตาม` / `ลิสต์`, ปุ่ม ➕ ใต้ผลสแกน/ดิจสต์,
เตือน watchlist ช่วงตลาดเปิด (10:00–17:00), เตือนวันประกาศงบเช้า (08:45)

**ยัง broadcast ทุกคน (ไม่แก้):** สแกน (17:30 + สั่งเอง), สรุปงบเช้า (08:55 + startup),
heartbeat (08:30), poll ข่าว SET

## การตัดสินใจดีไซน์ (ยืนยันกับผู้ใช้แล้ว)

1. **watch_state = ของกลางต่อหุ้น** — สถานะราคา (ทะลุไฮ 3 ด. / ผ่านไฮ 5 วัน / หลุด Low /
   ไฮใหม่) ของหุ้นตัวหนึ่งเหมือนกันทุกคน เก็บ `{หุ้น: สถานะ}` เดิม คำนวณครั้งเดียวต่อรอบ
   (ยิง Yahoo ตัวละครั้ง) แล้ว route ข้อความไปเฉพาะผู้ใช้ที่ติดตามหุ้นนั้น
2. **Migration** — ไฟล์ format เดิม (list) ยกให้ chat_id ที่ลงทะเบียนอยู่ ณ ตอน migrate
   (ปัจจุบันมีเจ้าของคนเดียว) ทำครั้งเดียวตอนบอทเปิด

## โครงสร้างข้อมูล

### `watchlist.json` (เปลี่ยน format)

เดิม:
```json
["AOT", "PTT"]
```
ใหม่ (key = chat_id เป็น string ตาม JSON):
```json
{
  "808446026": ["AOT", "PTT"],
  "123456789": ["CPALL"]
}
```

### `watch_state.json` (คงเดิม — ไม่แตะ format)

```json
{ "AOT": {"above_pre_low": true, "broke_pre3m_high": false, ...} }
```
keyed ตามหุ้น (global) เพิ่มแค่ **prune** ทิ้ง entry ของหุ้นที่ไม่มีใครติดตามแล้ว
เพื่อคง baseline-on-add (ถ้าหุ้นถูกเพิ่มกลับมาใหม่ จะเริ่มนับ baseline ใหม่ ไม่เตือนย้อน)

## เปลี่ยน `stock_core.py`

watchlist funcs รับ `chat_id` (int/str — normalize เป็น str ภายใน):

| ฟังก์ชัน | พฤติกรรม |
|---|---|
| `get_watchlist(chat_id)` | คืน list ของ chat นั้น (ว่างถ้าไม่มี) |
| `add_to_watchlist(ticker, chat_id)` | เพิ่มเข้าลิสต์ chat นั้น คืน `(base, symbols_ของ_chat_นั้น)` |
| `remove_from_watchlist(ticker, chat_id)` | ลบจากลิสต์ chat นั้น คืน `(base_or_None, remaining)` |
| `get_all_watched_symbols()` *(ใหม่)* | union หุ้นทุก chat (ให้ monitor ดึงตัวละครั้ง) |
| `get_watchers(symbol)` *(ใหม่)* | คืน list chat_id (str) ที่ติดตามหุ้นนั้น |
| `migrate_legacy_watchlist(chat_ids)` *(ใหม่)* | ถ้าไฟล์เป็น list → เขียนใหม่เป็น `{str(cid): list สำเนา}` สำหรับทุก cid ใน chat_ids; ถ้า chat_ids ว่างไม่ทำ (กันข้อมูลหาย) |

ภายใน:
- `_load_watchlists()` คืน dict เสมอ — ถ้าอ่านเจอ list (ยังไม่ migrate) คืน `{}`
  (get_watchlist จึงคืนว่างชั่วคราวจนกว่า migrate จะเขียน dict; migrate รันตอน startup
  ก่อน job ใดๆ จึงไม่มีผลจริง)
- normalize chat_id เป็น `str()` ทุกจุดที่เป็น key (JSON key เป็น string)
- `_base_symbol()` reuse เดิม

## เปลี่ยน `telegram_bot.py`

1. **startup (post_init / main ก่อน run_polling):** เรียก
   `stock_core.migrate_legacy_watchlist(_load_chat_ids())` ครั้งเดียว
2. **`ติดตาม <หุ้น>`:** `add_to_watchlist(t, update.effective_chat.id)`
3. **`เลิกติดตาม <หุ้น>`:** `remove_from_watchlist(t, update.effective_chat.id)`
4. **`ลิสต์` (`build_watchlist_summary`):** รับ `chat_id` แสดงลิสต์ของ chat ที่สั่ง
5. **ปุ่ม ➕ (callback):** `add_to_watchlist(sym, update.effective_chat.id)` (chat ของคนกด)
6. **`check_watchlist_changes()`:** เดิน `get_all_watched_symbols()`, คำนวณสถานะเหมือนเดิม
   (watch_state global), **คืน `dict {หุ้น: [ข้อความ]}`** แทน list แบน; prune watch_state
   ให้เหลือเฉพาะหุ้นใน union
7. **`watchlist_monitor_job`:** ได้ `alerts_by_symbol` → วนแต่ละ chat_id: รวมข้อความของหุ้น
   ที่อยู่ใน `get_watchlist(cid)` แล้วส่งเฉพาะ chat นั้น (chat ที่ลิสต์ว่าง/ไม่โดนเตือน = ข้าม)
8. **`build_earnings_reminder(chat_id)` + `earnings_reminder_job`:** วนแต่ละ chat_id สร้าง
   reminder จากลิสต์ของ chat นั้น ส่งเฉพาะ chat นั้น
9. **เครื่องหมาย "อยู่ในลิสต์แล้ว" ในข้อความ broadcast** (สแกน/heartbeat/ดิจสต์ — จุดที่เดิม
   ใช้ `get_watchlist()` ไม่มี arg): เปลี่ยนไปใช้ `get_all_watched_symbols()` (union) —
   เป็นแค่ cosmetic บอกว่า "มีคนติดตามอยู่"; ปุ่ม ➕ ยังเข้าลิสต์คนกดเอง (idempotent)

## จัดการ error / กรณีขอบ

- ลิสต์ chat ว่าง → job ข้าม chat นั้น (ไม่ส่งข้อความเปล่า)
- หุ้นเดียวหลายคนติดตาม → ยิง Yahoo ตัวเดียว (cache), เตือนครั้งเดียวต่อหุ้น route หลายคน
- add หุ้นที่มีอยู่แล้ว → idempotent (เช็ค `not in`) เหมือนเดิม
- migrate idempotent: รันซ้ำบน dict อยู่แล้วไม่ทำอะไร
- เขียนไฟล์ fail → get คืนว่าง/ไม่ crash (คง try/except เดิม)

## การทดสอบ

รันสคริปต์ตรง (ไม่มี test suite) จำลอง 2 chat_id ผ่าน temp file:
1. `add_to_watchlist("AOT", 111)` / `add_to_watchlist("PTT", 222)` → `get_watchlist(111)==["AOT"]`,
   `get_watchlist(222)==["PTT"]` (แยกจริง)
2. `get_all_watched_symbols()` == `{"AOT","PTT"}`; `get_watchers("AOT")==["111"]`
3. `remove_from_watchlist("AOT", 222)` → คืน `(None, ...)` ไม่กระทบ 111
4. Migration: เขียน `["AOT"]` ลงไฟล์ → `migrate_legacy_watchlist([808446026])` →
   ไฟล์กลายเป็น `{"808446026":["AOT"]}`
5. Routing: จำลอง `alerts_by_symbol={"AOT":[...]}`, chat 111 ติดตาม AOT, chat 222 ไม่ →
   111 ได้ 222 ไม่ได้
6. smoke: `import telegram_bot` (TOKEN ok), `import scanner`, `build_watchlist_summary(cid)`
7. ให้หลายโมเดล (code-reviewer + อีกตัว) รีวิว diff เรื่อง correctness + ไม่มี regression
   กับ flow broadcast เดิม

## ไม่กระทบ

- `watch_state.json` format เดิม
- `.gitignore` เดิม (`watchlist.json` ไม่เข้า git อยู่แล้ว)
- บอทที่รันบนเครื่องเจ้าของ: migrate อัตโนมัติ ลิสต์เดิมยังอยู่ (ยกให้เจ้าของ)
- Flow broadcast (สแกน/ดิจสต์) พฤติกรรมเดิม
