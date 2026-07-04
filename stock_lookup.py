import sys

from yfinance.exceptions import YFRateLimitError

import stock_core
from stock_core import (
    format_pct,
    format_signed_pct,
    format_volume,
    volume_flag,
)


def lookup(ticker_input):
    try:
        d = stock_core.get_stock_data(ticker_input, log=True)
    except YFRateLimitError:
        print("\n  ⏳  Yahoo จำกัดการเรียกข้อมูลชั่วคราว (rate limit)")
        print("  ลองใหม่อีกครั้งใน 1-2 นาทีนะครับ\n")
        return
    except Exception as e:
        print(f"\n  ⚠️  เกิดข้อผิดพลาด: {type(e).__name__}: {e}\n")
        return

    if d is None:
        print(f"\n  ❌  ไม่พบข้อมูลสำหรับ '{ticker_input}'")
        print("  ลองพิมพ์ ticker ให้ตรง เช่น  AOT  /  AAPL  /  AOT.BK\n")
        return

    price = d["price"]
    bar = "─" * 46

    print(f"\n  {bar}")
    print(f"  📊  {d['name']}  ({d['ticker']})   ข้อมูล ณ {d['last_date']} (ดึง {d['fetched_time']} น.)")
    print(f"  {bar}")
    print(f"  ราคาล่าสุด     :  {price:>10.2f}  {d['currency']}   ({format_signed_pct(d['day_change_pct'])} วันนี้)")
    print(f"  {bar}")
    print(f"  ⚡ ปฏิกิริยาราคาวันนี้")
    print(f"  Gap เปิดวันนี้  :  {format_signed_pct(d['gap_pct'])}")
    print(f"  เปิด → ล่าสุด   :  {format_signed_pct(d['intraday_pct'])}")
    print(f"  เปลี่ยน 5 วัน   :  {format_signed_pct(d['chg_5d_pct'])}")
    print(f"  เปลี่ยน 1 เดือน :  {format_signed_pct(d['chg_1m_pct'])}")
    if d["vol_ratio"] is not None:
        print(f"  วอลุ่มวันนี้    :  {format_volume(d['volume'])}  ({d['vol_ratio']:.1f}x ของเฉลี่ย 20 วัน){volume_flag(d['vol_ratio'])}")
    else:
        print(f"  วอลุ่มวันนี้    :  {format_volume(d['volume'])}")
    if d["last_earnings"] or d["next_earnings"]:
        print(f"  {bar}")
        if d["last_earnings"]:
            line = f"  งบล่าสุด       :  {d['last_earnings']:%d/%m/%Y}   ({d['days_since_earnings']} วันก่อน)"
            if d["since_earnings_pct"] is not None:
                line += f"   ตั้งแต่งบ {format_signed_pct(d['since_earnings_pct'])}"
            print(line)
            r = d["earn_reaction"]
            if r is not None and r["change_pct"] is not None:
                line = f"  วันตอบรับงบ    :  {format_signed_pct(r['change_pct'])}"
                if r["vol_ratio"] is not None:
                    line += f"   (วอลุ่ม {r['vol_ratio']:.1f}x{volume_flag(r['vol_ratio'])})"
                print(line)
        if d["next_earnings"]:
            print(f"  งบรอบถัดไป     :  {d['next_earnings']:%d/%m/%Y}   (อีก {d['days_to_earnings']} วัน)")
    s = d["post_signals"]
    if s is not None and (d["days_since_earnings"] or 999) <= 60:
        import stock_core as _sc
        score, stars = _sc.signal_score(d)
        print(f"  {bar}")
        head = "  📌 สัญญาณหลังงบ"
        if score is not None:
            head += f"   {stars} ({score}/{_sc.SCORE_MAX})"
        print(head)
        hi5_status = "✅ ผ่านแล้ว" if s["broke_pre5d_high"] else "ยังไม่ผ่าน"
        print(f"  ไฮ 5 วันก่อนงบ :  {s['pre5d_high']:>10.2f}   {hi5_status}")
        hi_status = "✅ ทะลุแล้ว" if s["broke_pre3m_high"] else "ยังไม่ผ่าน"
        print(f"  ไฮ 3 ด.ก่อนงบ  :  {s['pre3m_high']:>10.2f}   {hi_status}")
        dsh = s["days_since_new_high"]
        print(f"  ไฮใหม่ล่าสุด   :  {'วันนี้ 🔥' if dsh == 0 else str(dsh) + ' วันทำการก่อน'}")
        low_status = "✅ ยังเหนือ" if s["above_pre_low"] else "⛔ หลุดแล้ว"
        print(f"  Low ก่อนงบ     :  {s['pre_earn_low']:>10.2f}   {low_status} ({format_signed_pct(s['pct_above_pre_low'])})")
    print(f"  {bar}")
    print(f"  High 5 วัน     :  {d['week_high']:>10.2f}   ({format_pct(price, d['week_high'])} จากปัจจุบัน)")
    print(f"  Low  5 วัน     :  {d['week_low']:>10.2f}   ({format_pct(price, d['week_low'])} จากปัจจุบัน)")
    print(f"  {bar}")
    print(f"  High 3 เดือน   :  {d['hi3m']:>10.2f}   ({format_pct(price, d['hi3m'])} จากปัจจุบัน)")
    print(f"  Low  3 เดือน   :  {d['lo3m']:>10.2f}   ({format_pct(price, d['lo3m'])} จากปัจจุบัน)")
    print(f"  {bar}")

    if d["hi52"] and d["lo52"]:
        print(f"  52w High       :  {d['hi52']:>10.2f}   ({format_pct(price, d['hi52'])} จากปัจจุบัน)")
        print(f"  52w Low        :  {d['lo52']:>10.2f}   ({format_pct(price, d['lo52'])} จากปัจจุบัน)")
        print(f"  {bar}")

    print()


def main():
    if len(sys.argv) > 1:
        # รับ ticker จาก argument
        for t in sys.argv[1:]:
            lookup(t)
    else:
        # Interactive mode
        print("\n  Stock Lookup Tool  |  SET / US Stocks")
        print("  พิมพ์ชื่อหุ้น แล้วกด Enter  (พิมพ์ 'q' เพื่อออก)\n")
        while True:
            try:
                inp = input("  > Ticker: ").strip()
                if not inp:
                    continue
                if inp.lower() in ("q", "quit", "exit"):
                    print("  ลาก่อน!\n")
                    break
                # รองรับหลายตัวพร้อมกัน เช่น "AOT PTT CPALL"
                for t in inp.split():
                    lookup(t)
            except (KeyboardInterrupt, EOFError):
                print("\n  ลาก่อน!\n")
                break


if __name__ == "__main__":
    main()
