# -*- coding: utf-8 -*-
"""วันหยุดตลาด: _is_market_holiday อ่าน holidays.txt + ไฟล์จริงต้องมีวันหยุดข้างหน้าเสมอ

conftest ชี้ tb._HOLIDAYS_PATH ไป tmp แล้ว — เทสต์เขียนไฟล์ได้เต็มที่ ของจริงไม่โดน
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_bot as tb


def _write_holidays(text):
    with open(tb._HOLIDAYS_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def test_holiday_matches_ce_date():
    _write_holidays("# comment\n\n13/10/2026\n")
    assert tb._is_market_holiday(datetime.date(2026, 10, 13))


def test_holiday_matches_be_date():
    _write_holidays("13/10/2569\n")
    assert tb._is_market_holiday(datetime.date(2026, 10, 13))


def test_non_holiday_day_and_garbage_line_ignored():
    _write_holidays("ไม่ใช่วันที่\n13/10/2026\n")
    assert not tb._is_market_holiday(datetime.date(2026, 10, 14))


def test_missing_file_means_no_holiday():
    assert not os.path.exists(tb._HOLIDAYS_PATH)
    assert not tb._is_market_holiday(datetime.date(2026, 10, 13))


def test_real_holidays_file_parses_and_has_upcoming_date():
    """canary: ไฟล์จริงต้องไม่ว่างและไม่ค้างอดีตทั้งหมด — ล้มเมื่อไหร่ = ถึงเวลา
    เติมวันหยุดปีถัดไปจากประกาศ SET (ไม่งั้น guard วันหยุดทั้ง 7 จุดเป็นหมัน)"""
    real = os.path.join(tb._BASE_DIR, "holidays.txt")
    dates = []
    with open(real, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d = tb._parse_thai_date(line)
            assert d is not None, "บรรทัด parse ไม่ได้ (comment ต้องขึ้นต้น #): " + repr(line)
            dates.append(d)
    assert dates, "holidays.txt ไม่มีวันหยุดสักวัน — guard วันหยุดไม่ทำงาน"
    assert max(dates) >= datetime.date.today(), "วันหยุดในไฟล์เป็นอดีตทั้งหมด — เติมปีถัดไปจากประกาศ SET"
