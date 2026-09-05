# -*- coding: utf-8 -*-
""".env อ่านผ่าน python-dotenv (utf-8-sig) — BOM ใน .env ทำให้ BOT_TOKEN="" แล้วบอทปิดเงียบ

เดิม _load_bot_token (telegram_bot.py) / _env_value (scanner.py) เปิดไฟล์เอง encoding="utf-8"
— .env ที่ถูกเซฟใหม่ด้วยโปรแกรมที่ใส่ BOM (เช่น Notepad "UTF-8") บรรทัดแรกจะกลายเป็น
"﻿BOT_TOKEN=..." จับ "BOT_TOKEN=" ไม่ติด → token ว่าง → main() print แล้ว return เงียบๆ
→ run_bot.bat เห็น exit code ปกติ ไม่วน restart แต่ก็ไม่มีใครรู้ว่าทำไมบอทหาย
ใหม่: อ่านผ่าน dotenv.dotenv_values(..., encoding="utf-8-sig") ทนทั้งมี/ไม่มี BOM (เหมือน
HK/US) แต่ไม่เรียก load_dotenv() — ห้ามแตะ os.environ (สองโมดูลอ่านอิสระต่อกัน ไม่อยากให้
ลำดับ import ของโมดูลหนึ่งมีผลต่ออีกโมดูล และเทสต์อื่นในไฟล์นี้/ไฟล์ข้างเคียงจะเพี้ยนถ้า
os.environ ติดค่าจากเทสต์ก่อนหน้า)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scanner  # noqa: E402
import telegram_bot as tb  # noqa: E402


def _write_env(tmp_path, content, *, bom=False):
    p = tmp_path / ".env"
    p.write_text(content, encoding="utf-8-sig" if bom else "utf-8")
    return str(p)


# ── _load_bot_token (telegram_bot.py) ───────────────────────────────────────


def test_load_bot_token_reads_a_bom_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "BOT_TOKEN=123456:AAbbccDDeeff\n", bom=True)
    assert tb._load_bot_token(env_path) == "123456:AAbbccDDeeff"


def test_load_bot_token_strips_surrounding_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    env_path = _write_env(tmp_path, 'BOT_TOKEN="123:ABC"\n')
    assert tb._load_bot_token(env_path) == "123:ABC"


def test_load_bot_token_env_var_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "111:FROMENV")
    env_path = _write_env(tmp_path, "BOT_TOKEN=222:FROMFILE\n")
    assert tb._load_bot_token(env_path) == "111:FROMENV"


def test_load_bot_token_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    assert tb._load_bot_token(str(tmp_path / ".env")) == ""


def test_load_bot_token_does_not_mutate_os_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "BOT_TOKEN=123:ABC\n")
    tb._load_bot_token(env_path)
    assert "BOT_TOKEN" not in os.environ


# ── scanner._env_value ───────────────────────────────────────────────────────


def test_env_value_reads_a_bom_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    env_path = _write_env(tmp_path, "DASHBOARD_URL=https://example.com\n", bom=True)
    assert scanner._env_value("DASHBOARD_URL", env_path=env_path) == "https://example.com"


def test_env_value_strips_surrounding_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    env_path = _write_env(tmp_path, 'DASHBOARD_URL="https://example.com"\n')
    assert scanner._env_value("DASHBOARD_URL", env_path=env_path) == "https://example.com"


def test_env_value_env_var_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://from-env")
    env_path = _write_env(tmp_path, "DASHBOARD_URL=https://from-file\n")
    assert scanner._env_value("DASHBOARD_URL", env_path=env_path) == "https://from-env"


def test_env_value_missing_file_returns_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    assert scanner._env_value("DASHBOARD_URL", default="x",
                              env_path=str(tmp_path / ".env")) == "x"


def test_env_value_does_not_mutate_os_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    env_path = _write_env(tmp_path, "DASHBOARD_URL=https://example.com\n")
    scanner._env_value("DASHBOARD_URL", env_path=env_path)
    assert "DASHBOARD_URL" not in os.environ
