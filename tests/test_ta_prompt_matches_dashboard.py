# -*- coding: utf-8 -*-
"""เทสต์กัน drift ข้ามโปรเจกต์: ta_prompt.build_ta_prompt (Python) ต้องให้ข้อความเท่ากับ
taPrompt() ตัวจริงใน Trading_Dashboard/market_dashboard.js ทุกบรรทัด ยกเว้น ข.8

วิธี: ตัดฟังก์ชัน taPrompt + ค่าคงที่ที่มันใช้ออกจาก market_dashboard.js ด้วย regex → รันใน Node
กับหุ้นจริงจาก payload ในเครื่อง → เทียบทีละบรรทัดกับฝั่ง Python
ไม่มี node / ไม่มีไฟล์ JS / ไม่มี payload → skip (CI ที่ไม่มี dashboard ยังผ่าน)
ฝั่ง dashboard เพิ่ม/แก้บรรทัดเมื่อไหร่ เทสต์นี้แดงทันที — นี่คือราคาของการ port

ที่อยู่ไฟล์ JS: env DASHBOARD_JS → <DASHBOARD_SITE_DIR>/../market_dashboard.js → เดาจากโครง Desktop
"""
import json
import os
import random
import re
import shutil
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import ta_prompt  # noqa: E402

B8_PREFIX = "ข.8 วันประกาศงบถัดไป:"


def _env_value(key):
    """อ่าน env หรือ .env ข้างโปรเจกต์ (สำเนาเล็ก ๆ ของ scanner._env_value — ไม่ import scanner
    เพราะดึง pandas/pyarrow มาทั้งชุดแค่เพื่ออ่านค่าเดียว)"""
    val = os.environ.get(key, "").strip()
    if val:
        return val
    env_path = os.path.join(_ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _find_dashboard():
    """คืน (js_path, site_dir) หรือ (None, None)"""
    js = _env_value("DASHBOARD_JS")
    site = _env_value("DASHBOARD_SITE_DIR")
    cands = []
    if js:
        cands.append((js, site or os.path.join(os.path.dirname(js), "site")))
    if site:
        cands.append((os.path.join(os.path.dirname(site.rstrip("\\/")), "market_dashboard.js"), site))
    guess = os.path.join(os.path.dirname(_ROOT), "Dashboard", "Trading_Dashboard")
    cands.append((os.path.join(guess, "market_dashboard.js"), os.path.join(guess, "site")))
    for js_path, site_dir in cands:
        if os.path.isfile(js_path) and os.path.isfile(os.path.join(site_dir, "data", "manifest.json")):
            return js_path, site_dir
    return None, None


JS_PATH, SITE_DIR = _find_dashboard()
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not (JS_PATH and NODE),
    reason="ต้องมี market_dashboard.js + payload ของ Trading_Dashboard และ node ในเครื่อง",
)

# regex ตัดชิ้นส่วนที่ taPrompt ใช้ — ถ้าตัดไม่ได้ = โครง JS เปลี่ยน → ต้องแดง ไม่ใช่ skip
JS_PIECES = [
    ("STAGE_DEF", r"const STAGE_DEF = \{[\s\S]*?\};"),
    ("TREND_W", r"const TREND_W = [^\n]*\n"),
    ("MKT_LABEL", r"const MKT_LABEL = [^\n]*\n"),
    ("SCAN_LABEL", r"const SCAN_LABEL = [^\n]*\n"),
    ("idxRetBars", r"function idxRetBars\(mk, bars\)\{[\s\S]*?\n\}\n"),
    ("taPrompt", r"function taPrompt\(s\)\{[\s\S]*?\n\}\n"),
]


def _extract_pieces(src):
    out = []
    for name, pat in JS_PIECES:
        m = re.search(pat, src)
        assert m, f"หา {name} ใน market_dashboard.js ไม่เจอ — โครงไฟล์ฝั่ง dashboard เปลี่ยน ต้องอัปเดต ta_prompt.py/เทสต์"
        out.append(m.group(0))
    return "\n".join(out)


def _load_payload():
    with open(os.path.join(SITE_DIR, "data", "manifest.json"), encoding="utf-8") as fh:
        man = json.load(fh)
    bid = man["build_id"]
    with open(os.path.join(SITE_DIR, "data", bid, "core.json"), encoding="utf-8") as fh:
        core = json.load(fh)
    with open(os.path.join(SITE_DIR, "data", bid, "stocks-TH.json"), encoding="utf-8") as fh:
        stocks = json.load(fh)["stocks"]
    return core, stocks


def _pick_stocks(stocks):
    """หุ้นตัวอย่าง: หัว/ท้าย + ทุกเคสสุดขั้วที่เจอใน payload + สุ่ม seed คงที่"""
    picked = {}
    for s in stocks[:5] + stocks[-5:]:
        picked[s["t"]] = s
    for s in stocks:
        if s.get("rs") is None or s.get("vmax3") is None or s.get("st") is None \
                or not s.get("n") or s.get("macdv") is None or s.get("bis") == 0:
            picked[s["t"]] = s
    rng = random.Random(20260815)
    for s in rng.sample(stocks, min(20, len(stocks))):
        picked[s["t"]] = s
    return list(picked.values())


def _run_js(js_src, core, stocks):
    """รัน taPrompt ตัวจริงใน Node — ส่งชิ้นส่วน JS + ข้อมูลเป็น JSON ทาง stdin
    (ไม่ต้อง escape อะไร: new Function ฝั่ง Node ประกอบให้)"""
    pieces = _extract_pieces(js_src)
    payload = json.dumps({
        "DATA": {"asof": core.get("asof"), "markets": core.get("markets")},
        "stocks": stocks,
        "pieces": pieces,
    }, ensure_ascii=False)
    script = (
        "const fs=require('fs');"
        "const inp=JSON.parse(fs.readFileSync(0,'utf8'));"
        "const taPrompt=new Function('DATA', inp.pieces+'\\nreturn taPrompt;')(inp.DATA);"
        "process.stdout.write(JSON.stringify(inp.stocks.map(s=>taPrompt(s))));"
    )
    res = subprocess.run([NODE, "-e", script], input=payload, capture_output=True,
                         encoding="utf-8", timeout=60)
    assert res.returncode == 0, f"node ล้ม: {res.stderr[:500]}"
    return json.loads(res.stdout)


def compare_prompts(js_text, py_text):
    """คืนรายการความต่าง [(บรรทัดที่, js, py)] — ข.8 เทียบเฉพาะหัวบรรทัด"""
    js_lines, py_lines = js_text.split("\n"), py_text.split("\n")
    diffs = []
    if len(js_lines) != len(py_lines):
        diffs.append(("จำนวนบรรทัด", len(js_lines), len(py_lines)))
    for i, (a, b) in enumerate(zip(js_lines, py_lines), 1):
        if a.startswith(B8_PREFIX) or b.startswith(B8_PREFIX):
            if not (a.startswith(B8_PREFIX) and b.startswith(B8_PREFIX)):
                diffs.append((i, a, b))
            continue
        if a != b:
            diffs.append((i, a, b))
    return diffs


def test_comparator_detects_drift():
    """กันเทสต์หลอก: ตัวเปรียบเทียบต้องจับความต่างได้จริง"""
    base = "a\n" + B8_PREFIX + " x\nc"
    assert compare_prompts(base, base) == []
    assert compare_prompts(base, "a\n" + B8_PREFIX + " y\nc") == []      # ข.8 ต่างได้
    assert compare_prompts(base, "a\n" + B8_PREFIX + " x\nC") != []
    assert compare_prompts(base, base + "\nextra") != []


def test_python_port_matches_real_js_for_sampled_stocks():
    with open(JS_PATH, encoding="utf-8") as fh:
        js_src = fh.read()
    core, stocks = _load_payload()
    sample = _pick_stocks(stocks)
    js_out = _run_js(js_src, core, sample)
    market = dict(core["markets"]["TH"])
    market["core_asof"] = core.get("asof")
    failures = []
    for s, js_text in zip(sample, js_out):
        py_text = ta_prompt.build_ta_prompt(s, market, None)
        diffs = compare_prompts(js_text, py_text)
        if diffs:
            failures.append((s["t"], diffs[:3]))
    assert not failures, "Python ต่างจาก JS:\n" + "\n".join(
        f"  {t}: " + " | ".join(f"บรรทัด {i}: JS={a!r} PY={b!r}" for i, a, b in d) for t, d in failures)
    assert len(sample) >= 20


def test_template_line_headers_present_in_python_output():
    """ชั้นสอง (ไม่ต้องใช้ node): หัวบรรทัด ก.1…ค.10 และ === … === ใน template ของ taPrompt
    ต้องปรากฏในผลของฝั่ง Python ครบทุกอัน"""
    with open(JS_PATH, encoding="utf-8") as fh:
        js_src = fh.read()
    m = re.search(r"function taPrompt\(s\)\{[\s\S]*?\n\}\n", js_src)
    assert m
    # "ป้าย + คำแรก" ของแต่ละข้อ (เช่น "ก.3 ระดับราคา", "ข.5 RSI(14)") + หัวข้อ === … ===
    heads = set(re.findall(r"^((?:ก|ข|ค)\.\d+ \S+|=== [^=]+ ===)", m.group(0), re.M))
    assert len(heads) == 12, heads          # ก.1-ค.10 (10) + หัวข้อ 2
    core, stocks = _load_payload()
    market = dict(core["markets"]["TH"])
    market["core_asof"] = core.get("asof")
    out = ta_prompt.build_ta_prompt(stocks[0], market, None)
    missing = [h for h in heads if h not in out]
    assert not missing, missing
