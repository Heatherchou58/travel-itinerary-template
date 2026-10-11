#!/usr/bin/env python3
"""旅遊行程網站 —— 離線與資料層的自動化驗證。

跑的是「專案規範的除錯檢查清單」裡沒辦法靠肉眼確認的那幾項：離線行為、
CSV 的 BOM、鍵盤 focus，以及最重要的「把 TRIP_DATA 換掉，網站仍完整運作」。

安全性：腳本會把行程資料夾複製到系統暫存區，並把複製品的 FIREBASE_CONFIG
改成 null 才拿去測。這是刻意的——否則「離線新增花費」會把測試資料寫進
真正的共用 Firestore。代價是「恢復連線後自動補傳」那一段驗不到，只能手動
測（手機開飛航模式新增一筆，關掉後看右上角是否變回「已同步」）。

用法：
    python tests/offline_test.py                  # 預設測 fukuoka-2026
    python tests/offline_test.py --trip seoul-2027

需要：pip install playwright && playwright install chromium
"""

import argparse
import functools
import http.server
import json
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent

# 置換用的測試資料。每個 block 型別各放一個，漏掉 renderer 的話這裡就會抓到。
#
# theme 填具體值而不是 "auto"：執行時只做 dataset.theme = M.theme，
# "auto" → 半球與月份的推導是 builder 產生網站時算好的，這支腳本測的是
# 產出後的網站，碰不到那段邏輯。填 "auto" 只會得到一個沒有主題的頁面。
SWAP_TRIP = {
    "meta": {
        "tripId": "swaptest-2027",
        "title": "SWAP TEST 2027",
        "subtitle": "置換測試・驗證資料層",
        "theme": "ocean",
        "startDate": "2027-07-01",
        "endDate": "2027-07-02",
        "place": {"name": "Sydney", "nameLocal": "Sydney",
                  "lat": -33.8688, "lon": 151.2093, "timezone": "Australia/Sydney"},
        "money": {"home": "TWD", "local": "AUD", "rate": 21.5},
        "members": ["A", "B"],
        "categories": ["食", "行"],
    },
    "days": [
        {"id": "d1", "nav": "D1 抵達", "date": "2027-07-01", "title": "Day 1 - 出發", "blocks": [
            {"type": "flight", "airline": "China Airlines", "code": "CI51",
             "from": "TPE", "to": "SYD", "date": "2027-07-01",
             "depart": "21:30", "arrive": "10:05", "fromTerminal": "T1"},
            {"type": "stop", "kind": "sight", "time": "11:30",
             "title": "雪梨歌劇院", "titleLocal": "Sydney Opera House",
             "place": "Bennelong Point", "stay": 90, "notes": ["測試備註"],
             "intro": "介紹第一段，確認有渲染。\n第二段，確認換行會分段。",
             "links": [{"label": "官網", "url": "https://example.com", "style": "theme"}]},
            {"type": "transit", "time": "13:00", "line": "T1 線", "duration": "約 12 分",
             "steps": [{"name": "Circular Quay", "active": True},
                       {"name": "Town Hall", "active": False},
                       {"name": "Central", "active": True}]},
            {"type": "options", "time": "18:00", "title": "晚餐二選一",
             "place": "Darling Harbour",
             "choices": [{"title": "海鮮市場"}, {"title": "牛排館"}]},
            {"type": "lodging", "title": "測試飯店", "address": "1 Test St",
             "notes": ["含早餐"]},
        ]},
        {"id": "d2", "nav": "D2 回程", "date": "2027-07-02", "title": "Day 2 - 回家", "blocks": [
            {"type": "weather"},
            {"type": "checklist", "key": "pack", "title": "行李",
             "groups": [{"name": "證件", "items": ["護照", "簽證"]}]},
            {"type": "expenses"},
        ]},
    ],
    "phrases": [{"local": "How much?", "zh": "多少錢？"}],
    "emergency": [{"label": "報警", "value": "000", "tel": True}],
}

# theme 填 "auto" 或留空時的執行時推導。南北半球同月份必須得到不同結果，
# 否則等於沒有處理半球——紐澳的 12 月是盛夏，套飄雪就錯了。
AUTO_THEME_CASES = [
    ("auto + 北半球 1 月 → snow",   "auto", "2027-01-15",  35.6762, "snow"),
    ("auto + 南半球 1 月 → ocean",  "auto", "2027-01-15", -33.8688, "ocean"),
    ("留空 + 北半球 10 月 → maple", "",     "2027-10-15",  35.6762, "maple"),
]


# 分帳測試用：四名成員、只放記帳區塊。各情境靠注入 localStorage 餵不同花費，
# 所以只需要這一份副本。
SETTLE_TRIP = {
    "meta": {
        "tripId": "settletest", "title": "SETTLE TEST", "subtitle": "分帳測試",
        "theme": "city", "startDate": "2027-05-01", "endDate": "2027-05-01",
        "place": {"name": "Test", "lat": 25.0, "lon": 121.5, "timezone": "Asia/Taipei"},
        "money": {"home": "TWD", "local": "JPY", "rate": 0.22},
        "members": ["A", "B", "C", "D"], "categories": ["食"],
    },
    # 只有一天，所以載入後它就是選取中的分頁，不需要再點任何 tab。
    # （真實網站的「💰 記帳」也是一個 day，分頁完全由 days 生成。）
    "days": [{"id": "d1", "nav": "記帳", "date": "2027-05-01", "title": "記帳",
              "blocks": [{"type": "expenses"}]}],
    "phrases": [], "emergency": [],
}


def expense(payer: str, home: int, split: list) -> dict:
    return {"id": f"e{payer}{home}", "day": "d1", "payer": payer, "item": "測試",
            "local": 0, "home": home, "cat": "食", "split": split,
            "createdAt": 1, "createdBy": payer}


# (說明, 花費清單, 檢查函式)。檢查函式收到 {rows, tx}，回傳 (通過, 細節)。
def _sum_balanced(data):
    recv = sum(r["amt"] for r in data["rows"] if r["dir"] == "recv")
    pay = sum(r["amt"] for r in data["rows"] if r["dir"] == "pay")
    return recv == pay, f"畫面上應收 {recv}、應付 {pay}"


def _two_way_split(data):
    by = {r["who"]: r for r in data["rows"]}
    ok = (by["A"]["dir"] == "recv" and by["A"]["amt"] == 500
          and by["B"]["dir"] == "pay" and by["B"]["amt"] == 500
          and by["C"]["amt"] == 0 and by["D"]["amt"] == 0
          and len(data["tx"]) == 1)
    return ok, f"A {by['A']['dir']}{by['A']['amt']} / B {by['B']['dir']}{by['B']['amt']} / 轉帳 {len(data['tx'])} 筆"


def _only_split_members(data):
    by = {r["who"]: r for r in data["rows"]}
    ok = by["A"]["amt"] == 450 and by["B"]["amt"] == 450 and by["C"]["amt"] == 0 and by["D"]["amt"] == 0
    return ok, f"C {by['C']['amt']} / D {by['D']['amt']}（沒參與的人應為 0）"


def _fewest_transfers(data):
    # 四人兩兩相抵，最少 2 筆就能結清；貪婪法配對正確的話不會多出第三筆
    return len(data["tx"]) == 2, f"產生 {len(data['tx'])} 筆轉帳，最少應為 2 筆"


SETTLE_CASES = [
    ("兩人均分 1000 整除", [expense("A", 1000, ["A", "B"])], _two_way_split),
    ("三人均分 1000 除不盡：淨額加總要對得起來",
     [expense("A", 1000, ["A", "B", "C"])], _sum_balanced),
    ("沒列在分攤名單的人不該被算進去",
     [expense("A", 900, ["A", "B"])], _only_split_members),
    ("轉帳建議要是最少筆數",
     [expense("A", 400, ["A", "B", "C", "D"]), expense("B", 400, ["A", "B", "C", "D"])],
     _fewest_transfers),
]

# 讀出畫面上「結清建議」卡片的內容，而不是重算一次——要驗的是使用者看到什麼。
READ_SETTLE = """() => {
  const card = [...document.querySelectorAll('.card')]
    .find(c => c.querySelector('.subject')?.textContent.includes('結清建議'));
  if (!card) return null;
  const rows = [...card.querySelectorAll('.settle')].map(d => {
    const s = d.querySelectorAll('span');
    return {
      who: s[0].textContent.trim(),
      dir: s[1].textContent.trim().startsWith('應收') ? 'recv' : 'pay',
      amt: Number((s[1].querySelector('b')?.textContent || '').replace(/[^\\d.]/g, '')) || 0
    };
  });
  const note = card.querySelector('.note-box');
  const txt = note ? note.innerText.trim() : '';
  const tx = (!txt || txt.includes('已經結清')) ? [] : txt.split('\\n').filter(Boolean);
  return { rows, tx };
}"""


def rate_null_trip() -> dict:
    """匯率查不到時 rate 會是 null，網站必須照常可用（規範明文要求實測）。"""
    trip = json.loads(json.dumps(SETTLE_TRIP))
    trip["meta"]["tripId"] = "ratenull"
    trip["meta"]["money"]["rate"] = None
    return trip


def unknown_block_trip() -> dict:
    """未知型別要在畫面上顯示明確錯誤，不可靜默跳過（否則以為行程存到了，其實不見）。"""
    trip = json.loads(json.dumps(SETTLE_TRIP))
    trip["meta"]["tripId"] = "unknownblock"
    trip["days"][0]["blocks"] = [{"type": "totally-made-up-type", "title": "不存在的型別"},
                                 {"type": "expenses"}]
    return trip


def auto_theme_trip(theme: str, start: str, lat: float) -> dict:
    """照 SWAP_TRIP 複製一份，只改主題判定會用到的三個欄位。"""
    trip = json.loads(json.dumps(SWAP_TRIP))
    trip["meta"]["theme"] = theme
    trip["meta"]["startDate"] = start
    trip["meta"]["endDate"] = start
    trip["meta"]["place"]["lat"] = lat
    for i, day in enumerate(trip["days"]):
        day["date"] = start
    return trip


# ── 準備測試用的副本 ────────────────────────────────────────

def disable_firebase(path: Path) -> None:
    """把 FIREBASE_CONFIG 換成 null，確保測試碰不到真正的 Firestore。"""
    html = path.read_text(encoding="utf-8")
    patched = re.sub(r"const FIREBASE_CONFIG = \{[\s\S]*?\n\};",
                     "const FIREBASE_CONFIG = null;", html)
    if "const FIREBASE_CONFIG = null;" not in patched:
        sys.exit("準備測試副本失敗：找不到 FIREBASE_CONFIG，不敢繼續（可能會寫到真的雲端）")
    path.write_text(patched, encoding="utf-8")
    if re.search(r"AIza[A-Za-z0-9_-]{20,}", patched):
        sys.exit("準備測試副本失敗：副本裡仍有 API 金鑰殘留")


def swap_trip_data(path: Path, trip: dict) -> None:
    """用大括號配對找出 TRIP_DATA 的範圍再整段換掉（正則數不準巢狀括號）。"""
    html = path.read_text(encoding="utf-8")
    start = html.find("const TRIP_DATA = ")
    if start < 0:
        sys.exit("置換失敗：找不到 TRIP_DATA")
    i, depth, end = html.index("{", start), 0, -1
    for i in range(i, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        sys.exit("置換失敗：找不到 TRIP_DATA 的結尾")
    body = json.dumps(trip, ensure_ascii=False, indent=2)
    path.write_text(html[:start] + "const TRIP_DATA = " + body + html[end:],
                    encoding="utf-8")


def serve(root: Path) -> int:
    """在背景起一個靜態伺服器，回傳實際使用的埠號（0 = 讓系統挑）。"""
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # 不要把每個請求都印出來
            pass

    handler = functools.partial(Quiet, directory=str(root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


# ── 測試 ────────────────────────────────────────────────────

class Report:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append(ok)
        print(("  PASS  " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail else ""))

    def summary(self):
        passed = sum(self.rows)
        print(f"\n{'=' * 52}\n通過 {passed} / {len(self.rows)}")
        return passed == len(self.rows)


def run(trip: str) -> bool:
    src = REPO / trip
    if not (src / "index.html").exists():
        sys.exit(f"找不到 {src / 'index.html'}")

    tmp = Path(tempfile.mkdtemp(prefix="tripsite-"))
    try:
        shutil.copytree(src, tmp / "site")
        disable_firebase(tmp / "site" / "index.html")
        shutil.copytree(tmp / "site", tmp / "swap")
        swap_trip_data(tmp / "swap" / "index.html", SWAP_TRIP)

        for i, (_, theme, start, lat, _) in enumerate(AUTO_THEME_CASES):
            shutil.copytree(tmp / "site", tmp / f"auto{i}")
            swap_trip_data(tmp / f"auto{i}" / "index.html",
                           auto_theme_trip(theme, start, lat))

        for name, trip in [("settle", SETTLE_TRIP), ("rate", rate_null_trip()),
                           ("unknown", unknown_block_trip())]:
            shutil.copytree(tmp / "site", tmp / name)
            swap_trip_data(tmp / name / "index.html", trip)

        port = serve(tmp)
        base, swap = f"http://127.0.0.1:{port}/site/", f"http://127.0.0.1:{port}/swap/"
        r = Report()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            # 1 ── 離線後重新整理，各分頁仍可用 ────────────────
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=15000)
            page.reload()                      # 讓 Service Worker 接管這個頁面
            page.wait_for_load_state("networkidle")

            ctx.set_offline(True)
            page.reload()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1500)

            r.check("離線重新整理：頁面開得起來",
                    page.title() != "" and "503" not in page.content())
            days = page.locator(".nav-item").count()
            r.check("離線：行程日期導覽有渲染", days >= 2, f"{days} 個分頁")
            items = page.locator(".chk").count()
            r.check("離線：打包清單有渲染", items > 0, f"{items} 項")
            sync = page.locator("#sync").inner_text().strip()
            r.check("離線：連線狀態有標示（非空白）", sync != "", f"顯示「{sync}」")
            wx = page.locator("#wx-now").inner_text().strip()
            r.check("離線：天氣區塊不是空白", wx != "", f"{wx[:24]}…")

            # 2 ── 離線新增花費 ──────────────────────────────
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.get_by_role("tab", name=re.compile("記帳")).click()
            page.wait_for_timeout(500)
            page.locator("#e-item").fill("離線測試項目")
            page.locator("#e-local").fill("1000")
            page.locator("#e-add").click()
            page.wait_for_timeout(800)

            # 注意：一律讀 #main 的 innerText，不要用 page.content()。這個網站是
            # 單一 HTML、JS 內嵌，原始碼裡本來就含有 TRIP_DATA 和所有樣板字串，
            # 用 content() 比對等於永遠會中——就算 renderer 根本沒畫出來。
            r.check("離線新增花費：沒有 JS 例外", not errors, "; ".join(errors[:2]))
            r.check("離線新增花費：出現在列表",
                    "離線測試項目" in page.locator("#main").inner_text())

            page.reload()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1200)
            page.get_by_role("tab", name=re.compile("記帳")).click()
            page.wait_for_timeout(500)
            r.check("離線新增花費：重新整理後還在",
                    "離線測試項目" in page.locator("#main").inner_text())

            # 2b ── 打包清單是各自的，不進同步佇列 ────────────
            # 每個人的打包進度本來就不一樣，勾選不該上傳給其他人。
            # 記帳會進佇列、清單不會——兩者的差別就是這一項在把關。
            # （此時仍是離線狀態，正好是最容易誤把勾選排進佇列的情境。）
            page.locator(".nav-item").first.click()     # 清單在第一個分頁，不在記帳頁
            page.wait_for_timeout(400)
            page.locator(".chk input").first.check()
            page.wait_for_timeout(600)
            queued = page.evaluate(
                "JSON.parse(localStorage.getItem('trip:kyushu-fukuoka-2026') || '{}')"
                ".queue?.map(q => q.type) ?? []")
            r.check("打包清單：勾選不會進同步佇列", "checks" not in queued,
                    f"佇列內容 {queued}")
            r.check("打包清單：勾選有存在本機",
                    page.evaluate("Object.values(JSON.parse("
                                  "localStorage.getItem('trip:kyushu-fukuoka-2026')||'{}'"
                                  ").checks || {}).some(Boolean)"))

            # 3 ── 離線匯出 CSV ──────────────────────────────
            page.get_by_role("tab", name=re.compile("記帳")).click()   # 上一項切走了
            page.wait_for_timeout(400)
            with page.expect_download() as dl:
                page.locator("#e-csv").click()
            raw = Path(dl.value.path()).read_bytes()
            r.check("離線匯出 CSV：下載成功", len(raw) > 0, f"{len(raw)} bytes")
            r.check("CSV 開頭有 BOM（Excel 開中文不亂碼）", raw[:3] == b"\xef\xbb\xbf",
                    f"前三個位元組 {raw[:3].hex()}")
            text = raw.decode("utf-8-sig")
            r.check("CSV 中文可正確解碼", "離線測試項目" in text)
            r.check("CSV 換行是 CRLF", "\r\n" in text)
            ctx.close()

            # 4 ── 鍵盤操作看得見 focus ──────────────────────
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            visible = 0
            for _ in range(12):
                page.keyboard.press("Tab")
                info = page.evaluate("""() => {
                    const el = document.activeElement;
                    if (!el || el === document.body) return null;
                    const s = getComputedStyle(el);
                    return { style: s.outlineStyle, width: s.outlineWidth };
                }""")
                if info and info["style"] != "none" and info["width"] not in ("0px", ""):
                    visible += 1
            r.check("鍵盤 Tab：focus 框看得見", visible >= 5, f"12 次 Tab 有 {visible} 次有外框")
            ctx.close()

            # 5 ── 置換 TRIP_DATA，網站仍完整運作 ─────────────
            ctx = browser.new_context()
            page = ctx.new_page()
            swap_errors = []
            page.on("pageerror", lambda e: swap_errors.append(str(e)))
            page.goto(swap)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)

            r.check("置換 TRIP_DATA：沒有 JS 例外", not swap_errors, "; ".join(swap_errors[:2]))
            r.check("置換 TRIP_DATA：標題換掉了",
                    page.locator("#app-title").inner_text().strip() == "SWAP TEST 2027")
            n = page.locator(".nav-item").count()
            r.check("置換 TRIP_DATA：導覽由資料自動生成", n >= 2, f"{n} 個分頁")
            theme = page.evaluate("document.documentElement.dataset.theme")
            r.check("置換 TRIP_DATA：主題由資料套用（原站是 maple）", theme == "ocean",
                    f"實際 {theme}")

            shown = page.locator("#main").inner_text()
            for label, needle in [("flight 航班卡", "CI51"), ("stop 景點", "雪梨歌劇院"),
                                  ("transit 交通", "Circular Quay"),
                                  ("options 多選一", "海鮮市場"),
                                  ("lodging 住宿", "測試飯店")]:
                r.check(f"置換 TRIP_DATA：{label} 有渲染", needle in shown)

            # 景點介紹：預設收合，點了才展開
            intro = page.locator("details.intro")
            body = intro.locator(".intro-body")
            r.check("介紹：只有填了 intro 的景點才出現", intro.count() == 1,
                    f"{intro.count()} 個（這組資料只有 1 個景點填了）")
            r.check("介紹：預設是收合的", not body.is_visible())
            r.check("介紹：標題列是「看介紹」", intro.locator("summary").inner_text().strip() == "看介紹")
            r.check("介紹：標題列觸控目標 ≥ 44px",
                    round(intro.locator("summary").bounding_box()["height"]) >= 44,
                    f"{round(intro.locator('summary').bounding_box()['height'])}px")
            intro.locator("summary").click()
            page.wait_for_timeout(400)
            r.check("介紹：點了會展開", body.is_visible())
            r.check("介紹：換行有分成兩段", body.locator("p").count() == 2,
                    f"{body.locator('p').count()} 段")
            r.check("介紹：內容正確", "介紹第一段" in body.inner_text())
            ctx.close()

            # 6 ── theme 為 auto／留空時的執行時推導 ──────────
            for i, (label, _, _, _, want) in enumerate(AUTO_THEME_CASES):
                ctx = browser.new_context()
                page = ctx.new_page()
                page.goto(f"http://127.0.0.1:{port}/auto{i}/")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(800)
                got = page.evaluate("document.documentElement.dataset.theme")
                r.check(f"主題推導：{label}", got == want, f"實際 {got}")
                ctx.close()

            # 7 ── 分帳計算 ─────────────────────────────────
            for label, expenses, verify in SETTLE_CASES:
                ctx = browser.new_context()
                page = ctx.new_page()
                seed = json.dumps({"checks": {}, "expenses": expenses, "queue": []},
                                  ensure_ascii=False)
                page.add_init_script(
                    f"localStorage.setItem('trip:settletest', {json.dumps(seed)})")
                page.goto(f"http://127.0.0.1:{port}/settle/")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(600)
                data = page.evaluate(READ_SETTLE)
                if data is None:
                    r.check(f"分帳：{label}", False, "找不到結清建議卡片")
                else:
                    ok, detail = verify(data)
                    r.check(f"分帳：{label}", ok, detail)
                ctx.close()

            # 7b ── 結清摘要 CSV 的數字要跟畫面一致 ─────────────
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            seed = json.dumps({"checks": {},
                               "expenses": [expense("A", 1000, ["A", "B", "C"])],
                               "queue": []}, ensure_ascii=False)
            page.add_init_script(
                f"localStorage.setItem('trip:settletest', {json.dumps(seed)})")
            page.goto(f"http://127.0.0.1:{port}/settle/")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            with page.expect_download() as dl:
                page.locator("#e-csv2").click()
            csv = Path(dl.value.path()).read_bytes().decode("utf-8-sig")

            # 成員那幾列：成員,代墊總額,應分攤,淨額,狀態
            member_rows = [ln.split(",") for ln in csv.splitlines()
                           if ln.split(",")[0] in ("A", "B", "C", "D") and len(ln.split(",")) >= 4]
            advanced = sum(int(c[1]) for c in member_rows)
            owed = sum(int(c[2]) for c in member_rows)
            r.check("CSV 結清摘要：代墊總額加總 = 花費總額", advanced == 1000, f"實際 {advanced}")
            r.check("CSV 結清摘要：應分攤加總 = 花費總額", owed == 1000, f"實際 {owed}")

            screen = page.evaluate(READ_SETTLE)
            csv_net = {c[0]: int(c[3]) for c in member_rows}
            screen_net = {row["who"]: row["amt"] for row in screen["rows"]}
            r.check("CSV 結清摘要：淨額與畫面相同", csv_net == screen_net,
                    f"CSV {csv_net} / 畫面 {screen_net}")
            ctx.close()

            # 8 ── 匯率查不到（rate = null）時網站不能壞 ───────
            ctx = browser.new_context()
            page = ctx.new_page()
            rate_errors = []
            page.on("pageerror", lambda e: rate_errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/rate/")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)

            r.check("匯率 null：沒有 JS 例外", not rate_errors, "; ".join(rate_errors[:2]))
            r.check("匯率 null：記帳表單仍然可用",
                    page.locator("#e-item").count() == 1 and page.locator("#e-add").count() == 1)
            shown = page.locator("#main").inner_text()
            r.check("匯率 null：不顯示匯率說明", "僅供估算" not in shown)
            r.check("匯率 null：改為提示兩種幣別各自填寫", "沒有匯率資料" in shown)

            page.locator("#e-local").fill("1000")
            page.locator("#e-item").click()          # 觸發 change
            page.wait_for_timeout(300)
            r.check("匯率 null：不會亂自動換算",
                    page.locator("#e-home").input_value() == "",
                    f"本幣欄實際值「{page.locator('#e-home').input_value()}」")

            page.locator("#e-item").fill("手動填金額")
            page.locator("#e-home").fill("250")
            page.locator("#e-add").click()
            page.wait_for_timeout(600)
            r.check("匯率 null：手動填本幣仍能記帳",
                    "手動填金額" in page.locator("#main").inner_text())
            ctx.close()

            # 9 ── 未知 block 型別要顯示錯誤，不可靜默跳過 ──────
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/unknown/")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            body = page.locator("#main").inner_text()

            r.check("未知 block：畫面上有明確錯誤訊息", "未知的 block 型別" in body)
            r.check("未知 block：錯誤訊息指出是哪個型別", "totally-made-up-type" in body)
            err = page.locator("text=未知的 block 型別").first
            r.check("未知 block：錯誤訊息真的看得見（不是藏起來）",
                    err.count() > 0 and err.is_visible())
            r.check("未知 block：後面的區塊照常渲染（不會整頁中斷）",
                    page.locator("#e-add").count() == 1)
            ctx.close()

            browser.close()

        return r.summary()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="旅遊行程網站的離線與資料層驗證")
    ap.add_argument("--trip", default="fukuoka-2026", help="要測的行程資料夾（預設 fukuoka-2026）")
    args = ap.parse_args()
    print(f"測試 {args.trip}（副本已停用 Firebase，不會碰到真正的雲端資料）\n")
    sys.exit(0 if run(args.trip) else 1)
