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
import subprocess
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
        # 電腦版右側的當日路線地圖。測試裡會把 Google 的請求攔掉，只驗網址
        "mapsEmbedKey": "TEST_EMBED_KEY",
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
             # 停留(分) 是含交通的總時間：90 − 20 → 卡片上停留 1 小時 10 分
             "travel": 20, "mode": "walking",
             "hours": "09:00–17:00、週二休", "fee": "A$43（可刷卡）", "tel": "+61 2 9250 7111",
             "intro": "介紹第一段，確認有渲染。\n第二段，確認換行會分段。",
             "links": [{"label": "官網", "url": "https://example.com", "style": "theme"}]},
            {"type": "transit", "time": "13:00", "line": "T1 線", "duration": "約 12 分",
             # 當地語跟站名一樣的只顯示一次；不一樣的照常顯示
             "steps": [{"name": "Circular Quay", "nameLocal": "Circular Quay", "active": True,
                        "note": "提醒框會把這一列撐高，線不能斷"},
                       {"name": "Town Hall", "active": False},
                       {"name": "Central", "nameLocal": "Central Station", "active": True}]},
            {"type": "options", "time": "18:00", "title": "晚餐二選一",
             "place": "Darling Harbour",
             "choices": [{"title": "海鮮市場"}, {"title": "牛排館"}]},
            {"type": "lodging", "title": "測試飯店", "address": "1 Test St",
             "notes": ["含早餐"], "stay": 15, "travel": 15, "mode": "driving",
             "hours": "Check-in 15:00／退房 10:00"},
        ]},
        {"id": "d2", "nav": "D2 回程", "date": "2027-07-02", "title": "Day 2 - 回家", "blocks": [
            {"type": "weather"},
            {"type": "checklist", "key": "pack", "title": "行李",
             "groups": [{"name": "證件", "items": [
                 "護照",
                 {"text": "線上填寫入境手續", "url": "https://example.com/visa"},
                 {"text": "帶附註的項目", "note": "這行是補充說明"}]}]},
            {"type": "cards", "title": "信用卡", "items": [
                {"name": "有額度卡", "rate": 11, "regions": "日本",
                 "until": "2099-12-31", "quota": 5000, "register": True,
                 "note": "條件第一段\n條件第二段"},
                {"name": "無上限卡", "rate": 3.3, "regions": "海外",
                 "until": "2099-12-31", "quota": 0},
                {"name": "過期卡", "rate": 5, "regions": "日本",
                 "until": "2020-01-01", "quota": 1000},
            ]},
            {"type": "shopping", "title": "購物", "notes": ["購物說明"],
             "groups": [
                 # coupon 的群組不給數量與金額欄位——券是用掉的，不是買到的
                 {"name": "折價券", "coupon": True, "items": [
                     {"name": "有圖的券", "img": "img/swap-coupon.png"},
                     {"name": "要現場生成的券", "url": "https://example.com/coupon"}]},
                 {"name": "藥妝店", "items": [
                     {"name": "托買的東西", "who": "媽媽"}]},
             ]},
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


def key_test_trip(items: list) -> dict:
    """只有一張清單的行程，用來驗證勾選 key 的穩定性。
       tripId 固定，這樣兩個版本會共用同一份 localStorage。"""
    trip = json.loads(json.dumps(SETTLE_TRIP))
    trip["meta"]["tripId"] = "keytest"
    trip["days"][0]["blocks"] = [{
        "type": "checklist", "key": "k", "title": "清單",
        "groups": [{"name": "群", "items": items}]
    }]
    return trip


def regroup_trip(groups: dict) -> dict:
    """多個分組的清單，用來驗證「換分組不會讓勾消失」。
       groups 是 {分組名稱: [項目, …]}；tripId 固定，兩個版本共用同一份 localStorage。"""
    trip = json.loads(json.dumps(SETTLE_TRIP))
    trip["meta"]["tripId"] = "regrouptest"
    trip["days"][0]["blocks"] = [{
        "type": "checklist", "key": "k", "title": "清單",
        "groups": [{"name": n, "items": it} for n, it in groups.items()]
    }]
    return trip


# 卡片上私人資料的測試資料：兩張卡片各掛一段，驗證解開一次會同時顯示、內容不會混在一起。
# 密語故意混大小寫與連字號，測的時候再用不同寫法輸入。
VAULT_PASS = "Test-Pass-2027"
VAULT_PLAIN = {
    "passphrase": VAULT_PASS,
    "sections": [
        {"key": "t1", "items": [
            {"label": "門鎖密碼", "value": "SECRET-4821"},
            {"label": "入住指南", "url": "https://example.com/guide"},
            {"note": "密碼盒掛在門把上"}]},
        {"key": "t2", "items": [{"label": "訂單編號", "value": "ORDER-7731"}]},
    ],
}
VAULT_SECRETS = ["SECRET-4821", "ORDER-7731", "example.com/guide", "密碼盒掛在門把上",
                 VAULT_PASS, "testpass2027"]


def make_vault(tmp: Path) -> dict:
    """用正式的 tools/encrypt-vault.mjs --seal 加密——順便驗證 Node 加密、瀏覽器解密兩邊對得上。"""
    src = tmp / "vault-plain.json"
    src.write_text(json.dumps(VAULT_PLAIN, ensure_ascii=False), encoding="utf-8")
    try:
        out = subprocess.run(["node", str(REPO / "tools" / "encrypt-vault.mjs"), "--seal", str(src)],
                             capture_output=True, text=True, encoding="utf-8")
    finally:
        src.unlink()
    if out.returncode != 0:
        sys.exit("產生私人資料的測試密文失敗：" + out.stderr)
    return json.loads(out.stdout)


def vault_trip(vault: dict) -> dict:
    trip = json.loads(json.dumps(SETTLE_TRIP))
    trip["meta"]["tripId"] = "vaulttest"
    trip["days"][0]["blocks"] = [
        {"type": "stop", "kind": "sight", "time": "15:00", "title": "測試住處門口",
         "secret": "t1", "secretHint": "門鎖密碼", "notes": [], "links": []},
        {"type": "lodging", "time": "20:00", "title": "測試飯店",
         "secret": "t2", "secretHint": "訂單編號", "notes": [], "links": []},
        {"type": "expenses"}]
    trip["vault"] = vault
    return trip


def photo_trip(src: str) -> dict:
    """一張有照片、一張沒照片的介紹。照片用網站副本裡真的檔案，才驗得到離線快取。"""
    trip = json.loads(json.dumps(SETTLE_TRIP))
    trip["meta"]["tripId"] = "phototest"
    trip["days"][0]["blocks"] = [
        {"type": "stop", "kind": "sight", "time": "10:00", "title": "有照片的景點",
         "notes": [], "links": [], "intro": "第一段介紹。\n第二段介紹。",
         "photo": {"src": src, "credit": "測試作者", "license": "CC BY-SA 4.0",
                   "href": "https://commons.wikimedia.org/wiki/File:Test.jpg"}},
        {"type": "stop", "kind": "sight", "time": "11:00", "title": "沒照片的景點",
         "notes": [], "links": [], "intro": "只有文字的介紹。"},
        {"type": "expenses"}]
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

        # 介紹照片的測試借用網站裡真的一張照片；行程沒有照片就跳過那一段
        intro_imgs = sorted((src / "img" / "intro").glob("*.jpg"))
        photo_src = f"img/intro/{intro_imgs[0].name}" if intro_imgs else ""

        for name, trip in [("settle", SETTLE_TRIP), ("rate", rate_null_trip()),
                           ("unknown", unknown_block_trip()),
                           ("vault", vault_trip(make_vault(tmp))),
                           ("photo", photo_trip(photo_src))]:
            shutil.copytree(tmp / "site", tmp / name)
            swap_trip_data(tmp / name / "index.html", trip)

        # 勾選 key 的穩定性：同一個 tripId、同一個 origin，只差在清單中間插了一項。
        # key 若用陣列索引，插入後打勾會整排往下錯位。
        for name, items in [("keyA", ["甲", "乙", "丙"]),
                            ("keyB", ["甲", "新插入的", "乙", "丙"])]:
            shutil.copytree(tmp / "site", tmp / name)
            swap_trip_data(tmp / name / "index.html", key_test_trip(items))

        # 換分組：同一個項目從「甲組」搬到「乙組」。key 若含分組名稱，勾就會消失。
        for name, groups in [("regroupA", {"甲組": ["甲", "乙"], "乙組": ["丙"]}),
                             ("regroupB", {"甲組": ["甲"], "乙組": ["乙", "丙"]})]:
            shutil.copytree(tmp / "site", tmp / name)
            swap_trip_data(tmp / name / "index.html", regroup_trip(groups))

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

            ride = page.locator("#main .card", has_text="Circular Quay")
            ride_text = ride.inner_text()
            r.check("交通卡：當地語跟站名一樣時只顯示一次", ride_text.count("Circular Quay") == 1,
                    f"出現 {ride_text.count('Circular Quay')} 次")
            r.check("交通卡：當地語不同時照常顯示", "Central Station" in ride_text)
            weights = [int(w) for w in ride.locator(".route-body strong").evaluate_all(
                "els => els.map(e => getComputedStyle(e).fontWeight)")]
            r.check("交通卡：經過站比起終點淡", len(weights) == 3 and weights[1] < weights[0] == weights[2],
                    f"{weights}")
            # 每一站（最後一站除外）圓點下面的線要畫到這一列底部，並且緊接著下一段
            joined = ride.locator(".route").evaluate("""r => [...r.children].slice(0, -1)
                .filter(k => k.classList.contains("route-step"))
                .map(k => Math.round(k.getBoundingClientRect().bottom - k.nextElementSibling.getBoundingClientRect().top) === 0
                       && parseFloat(getComputedStyle(k, "::before").height) >= k.offsetHeight - 24)""")
            r.check("交通卡：有提醒框的站，線也連到下一站", bool(joined) and all(joined), f"{joined}")

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

            # 交通、營業時間、費用、電話：各自一列，不塞在說明裡
            opera = page.locator("#main .card", has_text="雪梨歌劇院")
            otext = opera.inner_text()
            r.check("交通：顯示步行與分鐘數", "步行 20 分" in otext, otext.splitlines()[0])
            # 停留(分) 含交通，卡片要扣掉——不扣的話門司港會顯示停留 3 小時 50 分
            r.check("交通：停留扣掉交通（90 − 20）", "停留 1 小時 10 分" in otext)
            r.check("交通：沒有把總時間當成停留", "停留 1 小時 30 分" not in otext)
            r.check("營業時間：獨立一列", "09:00–17:00、週二休" in otext)
            r.check("費用：獨立一列", "A$43（可刷卡）" in otext)
            tel = opera.locator("a.tel")
            r.check("電話：是可以點的連結", tel.count() == 1)
            r.check("電話：撥號只留數字與 +", tel.get_attribute("href") == "tel:+61292507111",
                    tel.get_attribute("href"))
            tb = tel.bounding_box()
            r.check("電話：觸控目標 ≥ 44px", tb and round(tb["height"]) >= 44,
                    f"{round(tb['height']) if tb else '—'}px 高")
            hotel = page.locator("#main .card.lodge", has_text="測試飯店")
            htext = hotel.inner_text()
            r.check("住宿：也顯示交通", "車程 15 分" in htext)
            r.check("住宿：交通等於總時間時不顯示停留", "停留" not in htext)
            r.check("住宿：營業時間（入住退房）獨立一列", "Check-in 15:00／退房 10:00" in htext)

            # 信用卡回饋額度（在第二天）
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            r.check("信用卡：三張都渲染", page.locator(".cc").count() == 3,
                    f"{page.locator('.cc').count()} 張")
            r.check("信用卡：沒有額度的不畫進度條",
                    page.locator('.cc[data-card="無上限卡"] .cc-bar').count() == 0)
            r.check("信用卡：過期的有標示",
                    page.locator('.cc[data-card="過期卡"] .cc-expired').count() == 1)
            r.check("信用卡：未過期的不標",
                    page.locator('.cc[data-card="有額度卡"] .cc-expired').count() == 0)

            quota_card = page.locator('.cc[data-card="有額度卡"]')
            inp = quota_card.locator(".cc-spent")
            inp.click()
            inp.type("2000", delay=30)
            page.wait_for_timeout(300)
            # 整頁重畫的話輸入框會失焦，這是不重畫的全部理由
            r.check("信用卡：輸入時輸入框不失焦",
                    page.evaluate("document.activeElement.classList.contains('cc-spent')"))
            r.check("信用卡：剩餘額度正確（額度 5000 − 已刷 2000）",
                    "3,000" in quota_card.locator(".cc-left").inner_text(),
                    quota_card.locator(".cc-left").inner_text())
            r.check("信用卡：進度條寬度正確",
                    quota_card.locator(".cc-bar i").evaluate("e => e.style.width") == "40%",
                    quota_card.locator(".cc-bar i").evaluate("e => e.style.width"))
            r.check("信用卡：已刷金額不進同步佇列",
                    "cards" not in page.evaluate(
                        "(JSON.parse(localStorage.getItem('trip:swaptest-2027')||'{}').queue||[])"
                        ".map(q => q.type).join(',')"))

            # 購物清單與優惠券（同一個元件，也在第二天）
            r.check("購物：兩個群組都渲染", page.locator(".shop-g").count() == 2,
                    f"{page.locator('.shop-g').count()} 組")
            r.check("購物：三個項目都渲染", page.locator(".shop-i").count() == 3,
                    f"{page.locator('.shop-i').count()} 項")
            r.check("購物：有填圖片的才有縮圖", page.locator(".shop-img").count() == 1)
            r.check("購物：有填連結的才有跳轉按鈕", page.locator(".shop-link").count() == 1)
            r.check("購物：沒有圖片的要讓開左上角的 ✕",
                    page.locator('.shop-i[data-id="折價券:要現場生成的券"].no-img').count() == 1)
            r.check("購物：誰要的有顯示",
                    "媽媽" in page.locator('.shop-i[data-id="藥妝店:托買的東西"]').inner_text())

            # 券是用掉、不是買到，所以整組都不該有數量與金額
            r.check("購物：券的群組沒有數量與金額欄位",
                    page.locator('.shop-g[data-group="折價券"] .shop-note').count() == 0
                    and page.locator('.shop-g[data-group="折價券"] .shop-amt').count() == 0,
                    f"{page.locator('.shop-g[data-group=\"折價券\"] .shop-note').count()} 個備註／"
                    f"{page.locator('.shop-g[data-group=\"折價券\"] .shop-amt').count()} 個金額")
            r.check("購物：商品的群組有數量與金額欄位",
                    page.locator('.shop-g[data-group="藥妝店"] .shop-note').count() == 1
                    and page.locator('.shop-g[data-group="藥妝店"] .shop-amt').count() == 1)
            # 券不是待辦事項，是拿出來給店員看的東西，所以也不給勾選框
            r.check("購物：券沒有勾選框",
                    page.locator('.shop-g[data-group="折價券"] input[type=checkbox]').count() == 0,
                    f"{page.locator('.shop-g[data-group=\"折價券\"] input[type=checkbox]').count()} 個")
            r.check("購物：券的群組標題顯示張數不是進度",
                    page.locator('.shop-g[data-group="折價券"] .shop-n').inner_text() == "共 2 張",
                    page.locator('.shop-g[data-group="折價券"] .shop-n').inner_text())
            r.check("購物：券的名稱還是看得到",
                    "有圖的券" in page.locator('.shop-i[data-id="折價券:有圖的券"]').inner_text())

            item = page.locator('.shop-i[data-id="藥妝店:托買的東西"]')
            item.locator("input[type=checkbox]").check()
            page.wait_for_timeout(300)
            r.check("購物：勾選後群組計數就地更新",
                    page.locator('.shop-g[data-group="藥妝店"] .shop-n').inner_text() == "1/1",
                    page.locator('.shop-g[data-group="藥妝店"] .shop-n').inner_text())
            amt = item.locator(".shop-amt")
            amt.click()
            amt.type("1600", delay=30)
            page.wait_for_timeout(300)
            # 跟信用卡同一個理由：重畫整頁會讓輸入框失焦
            r.check("購物：輸入金額時輸入框不失焦",
                    page.evaluate("document.activeElement.classList.contains('shop-amt')"))
            r.check("購物：勾選與金額不進同步佇列",
                    "shopping" not in page.evaluate(
                        "(JSON.parse(localStorage.getItem('trip:swaptest-2027')||'{}').queue||[])"
                        ".map(q => q.type).join(',')"))
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            r.check("購物：重新整理後商品的勾選還在",
                    page.locator('.shop-i[data-id="藥妝店:托買的東西"] input[type=checkbox]').is_checked())
            r.check("購物：重新整理後商品的金額還在",
                    page.locator('.shop-i[data-id="藥妝店:托買的東西"] .shop-amt').input_value() == "1600",
                    page.locator('.shop-i[data-id="藥妝店:托買的東西"] .shop-amt').input_value())

            # ✕ 的熱區。圓圈只有 28px（放大會擋住券上的條碼），
            # 靠 ::after 補到 44px——所以要量實際可點範圍，不是元素尺寸。
            page.locator(".shop-x").first.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            hit = page.evaluate("""() => {
              const b = document.querySelector('.shop-x');
              b.scrollIntoView({block:'center'});
              const r = b.getBoundingClientRect();
              /* 圓圈本身一定要打得到，否則下面量到的 0 是「捲出畫面」而不是「沒熱區」 */
              const self = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2) === b;
              let w = 0, h = 0;
              for (let d = 1; d <= 20; d++){
                if (document.elementFromPoint(r.left - d, r.top + r.height/2) === b) w = d;
                if (document.elementFromPoint(r.left + r.width/2, r.top - d) === b) h = d;
              }
              return { self, w: Math.round(r.width) + w * 2, h: Math.round(r.height) + h * 2 };
            }""")
            r.check("購物：✕ 的可點範圍 ≥ 44px",
                    hit["self"] and hit["w"] >= 44 and hit["h"] >= 44,
                    f"{hit['w']}×{hit['h']}" + ("" if hit["self"] else "（圓圈本身就打不到）"))

            # 刪掉的項目要能還原——誤觸 ✕ 不該讓一張券永久消失
            r.check("購物：沒刪東西時不顯示還原按鈕",
                    page.locator(".shop-restore").count() == 0)
            page.locator('.shop-i[data-id="折價券:有圖的券"] .shop-x').click()
            page.wait_for_timeout(400)
            r.check("購物：刪除後項目消失", page.locator(".shop-i").count() == 2)
            r.check("購物：刪除後出現還原按鈕",
                    "1 項" in page.locator(".shop-restore").inner_text(),
                    page.locator(".shop-restore").inner_text())
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            r.check("購物：重新整理後刪掉的不會跑回來",
                    page.locator(".shop-i").count() == 2)
            page.locator(".shop-restore").click()
            page.wait_for_timeout(400)
            r.check("購物：還原後項目回來了", page.locator(".shop-i").count() == 3)

            # 新增項目與群組
            page.locator('.shop-add[data-addto="藥妝店"] [name="n"]').fill("自己加的東西")
            page.locator('.shop-add[data-addto="藥妝店"] button[type="submit"]').click()
            page.wait_for_timeout(400)
            page.locator('.shop-addg [name="g"]').fill("超市")
            page.locator('.shop-addg button[type="submit"]').click()
            page.wait_for_timeout(400)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            r.check("購物：新增的項目重新整理後還在",
                    page.locator('.shop-g[data-group="藥妝店"] .shop-i').count() == 2,
                    f"{page.locator('.shop-g[data-group=\"藥妝店\"] .shop-i').count()} 項")
            r.check("購物：新增的群組重新整理後還在",
                    page.locator('.shop-g[data-group="超市"]').count() == 1)

            # 點圖放大給店員看
            page.locator(".shop-img").first.click()
            page.wait_for_timeout(400)
            r.check("購物：點圖會放大", page.locator("#zoom[open]").count() == 1)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            r.check("購物：Esc 可以關掉", page.locator("#zoom[open]").count() == 0)

            # 通用打包物品不該被倒進購物清單（購物是另一個型別、另一個分頁）
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            r.check("購物：沒有混進通用打包物品",
                    "護照" not in page.locator("#main .shop-grid").first.inner_text())

            # 清單備註
            r.check("清單項目的附註有渲染", page.locator(".chk-note").count() == 1,
                    f"{page.locator('.chk-note').count()} 條")

            # 清單項目的連結（在第二天）
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            link = page.locator(".chk-link")
            r.check("清單連結：有填網址的項目才出現", link.count() == 1,
                    f"{link.count()} 個（這組資料只有 1 項填了網址）")
            r.check("清單連結：觸控目標 ≥ 44px",
                    round(link.bounding_box()["height"]) >= 44 and round(link.bounding_box()["width"]) >= 44,
                    f"{round(link.bounding_box()['width'])}×{round(link.bounding_box()['height'])}")
            r.check("清單連結：網址正確", link.get_attribute("href") == "https://example.com/visa")
            # 這題才是重點：連結若放在 <label> 裡，點它會順便把項目打勾
            r.check("清單連結：不在 <label> 內（否則點了會打勾）",
                    page.evaluate("!document.querySelector('.chk-link').closest('label')"))
            boxes = page.locator(".chk input")
            before = [boxes.nth(i).is_checked() for i in range(boxes.count())]
            with ctx.expect_page() as popup:
                link.click()
            popup.value.close()
            page.wait_for_timeout(300)
            after = [boxes.nth(i).is_checked() for i in range(boxes.count())]
            r.check("清單連結：點了不會把項目打勾", before == after, f"{before} → {after}")
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

            # 7c ── 「現在進行到哪」與跨時區的天數 ──────────────
            # 真實日期不在行程期間內，所以用假時鐘把時間固定在旅程中。
            # 瀏覽器時區刻意設成台北，目的地是東京——兩者差一小時，
            # 正好用來驗證日期是不是真的用目的地時區在算。
            def at(utc_iso, tz="Asia/Taipei"):
                c = browser.new_context(timezone_id=tz)
                pg = c.new_page()
                pg.clock.set_fixed_time(utc_iso)
                pg.goto(f"http://127.0.0.1:{port}/site/")
                pg.wait_for_load_state("networkidle")
                pg.wait_for_timeout(600)
                return c, pg

            # 2026-11-17 03:00Z = 東京 12:00，上色見熊野座神社（10:50 起、停留 145 分）進行中
            ctx, page = at("2026-11-17T03:00:00Z")
            nowc = page.locator("#main .card.is-now")
            r.check("目前行程：剛好標出一張卡片", nowc.count() == 1, f"{nowc.count()} 張")
            r.check("目前行程：標在上色見熊野座神社上", "上色見熊野座神社" in nowc.inner_text())
            line = page.locator("#main .card.is-now .now-line").inner_text()
            r.check("目前行程：顯示剩餘時間與離開時刻",
                    "還有 1 小時 15 分" in line and "13:15" in line, f"「{line}」")
            r.check("已結束的項目有淡化", page.locator("#main .card.is-past").count() >= 2,
                    f"{page.locator('#main .card.is-past').count()} 張")
            r.check("自動跳到今天的分頁",
                    "D4" in page.locator('.nav-item[aria-selected="true"]').inner_text())
            r.check("導覽列標出今天", page.locator(".nav-item.is-today").count() == 1)
            # 分頁按鈕上的日期
            tab_txt = page.locator('.nav-item[aria-selected="true"]').inner_text().replace("\n", " / ")
            r.check("分頁按鈕帶日期與星期", "11/17 二" in tab_txt, f"「{tab_txt}」")
            r.check("沒有日期的分頁不顯示那一行",
                    page.locator(".nav-item", has_text="記帳").locator(".nav-sub").count() == 0)

            # 連線狀態是 position:fixed、z-index 比置頂導覽列高，擺在右上角會
            # 直接蓋住分頁按鈕。另外 top 與 bottom 同時存在會把它撐滿整個視窗高。
            sb = page.locator(".sync").bounding_box()
            r.check("連線狀態沒有被撐高", sb["height"] < 60, f"高 {round(sb['height'])}px")
            covered = []
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tb = tabs.nth(i).bounding_box()
                if tb and not (sb["x"] + sb["width"] < tb["x"] or tb["x"] + tb["width"] < sb["x"]
                               or sb["y"] + sb["height"] < tb["y"] or tb["y"] + tb["height"] < sb["y"]):
                    covered.append(tabs.nth(i).inner_text().replace("\n", " "))
            r.check("連線狀態沒有蓋住分頁按鈕", not covered, "、".join(covered))
            # [hidden] 來自 UA 樣式表，會被 .now-line 的 display:inline-flex 蓋掉，
            # 導致每張有時間的卡片都露出一小條空的色塊。只驗 is-now 那張抓不到。
            r.check("其他卡片的標記列真的看不見",
                    page.locator("#main .card:not(.is-now):not(.is-next) .now-line:visible").count() == 0,
                    f"{page.locator('#main .card:not(.is-now):not(.is-next) .now-line:visible').count()} 條露出來")
            # 住宿卡片自己有 1px 邊框，高亮用 shorthand 才壓得過
            w = page.evaluate("getComputedStyle(document.querySelector('#main .card.is-now')).borderLeftWidth")
            r.check("目前項目的高亮邊框是 4px", w == "4px", f"實際 {w}")
            ctx.close()

            # 東京 06:00：當天第一項（金鱗湖 07:00）還沒開始，應標成「接下來」
            # 而不是什麼都不標——早上起床打開 app 就是這個情境。
            ctx, page = at("2026-11-16T21:00:00Z")
            nxt = page.locator("#main .card.is-next")
            r.check("還沒開始時標「接下來」",
                    nxt.count() == 1 and page.locator("#main .card.is-now").count() == 0,
                    f"is-next {nxt.count()} / is-now {page.locator('#main .card.is-now').count()}")
            if nxt.count():
                t = nxt.locator(".now-line").inner_text()
                r.check("「接下來」顯示還要多久才開始",
                        "後開始" in t and "07:00" in t, f"「{t}」")
                r.check("「接下來」標在當天第一項上", "金鱗湖" in nxt.inner_text())
            ctx.close()

            # 跨日關鍵測試：台北 2026-11-16 23:30，此時東京已是 11-17
            # 用裝置時區會算成「第 3 天」，用目的地時區才是「第 4 天」
            ctx, page = at("2026-11-16T15:30:00Z")
            head = page.locator("#wx-now").inner_text()
            r.check("跨日：天數用目的地時區（台北 11/16 深夜 → 東京已 11/17）",
                    "第 4 天" in head, f"標頭顯示「{head.splitlines()[0] if head else ''}」")
            r.check("跨日：日期也顯示目的地的 11/17", "11/17" in head)
            ctx.close()

            # 行程還沒開始：不該標任何東西，且顯示倒數
            ctx, page = at("2026-10-01T03:00:00Z")
            r.check("還沒出發：不標目前行程",
                    page.locator("#main .card.is-now, #main .card.is-next").count() == 0)
            r.check("還沒出發：顯示倒數天數", "出發倒數" in page.locator("#wx-now").inner_text())
            ctx.close()

            # 7ca ── 編輯入口只留在行前 ─────────────────────────
            # 「編輯行程」「重新產生網站」是出發前才用的東西。旅途中打開
            # 當天的分頁，頁尾擺著「重新產生網站」只是噪音，誤觸還會跳出去。
            # 必須用真實站：SWAP_TRIP 沒有 d0，也沒有 sheetUrl／builderUrl。
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/site/")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)

            bar = page.locator("#edit-bar")
            tabs = page.locator(".nav-item")
            prep = [i for i in range(tabs.count())
                    if tabs.nth(i).inner_text().startswith("📝")]
            r.check("編輯列：行程有行前分頁", len(prep) == 1, f"{len(prep)} 個")
            tabs.nth(prep[0]).click()
            page.wait_for_timeout(300)
            # 一律用 is_visible()，不要用 getBoundingClientRect()：#edit-bar 是
            # display:flex，被 UA 的 [hidden] 蓋掉與否用高度看不出來。
            r.check("編輯列：行前看得到", bar.is_visible())
            r.check("編輯列：兩顆按鈕都在", bar.locator("a").count() == 2,
                    bar.inner_text().replace("\n", " / "))

            other = [i for i in range(tabs.count()) if i != prep[0]]
            still = []
            for i in other:
                tabs.nth(i).click()
                page.wait_for_timeout(200)
                if bar.is_visible():
                    still.append(tabs.nth(i).inner_text().replace("\n", " "))
            r.check("編輯列：其他分頁都看不到", not still,
                    f"{len(other)} 個分頁" + (f"，還看得到：{still}" if still else "，全部隱藏"))
            # 這一項擋的是「[hidden] 輸給 display:flex」的回歸——少了
            # #edit-bar[hidden]{display:none}，屬性有下去但畫面照樣看得到。
            r.check("編輯列：是靠 hidden 屬性藏的",
                    page.locator("#edit-bar[hidden]").count() == 1)
            # footer 還掛著 Open-Meteo 的 CC BY 授權標示，不能跟著一起藏掉
            r.check("編輯列：footer 的授權標示沒被一起藏掉",
                    "Open-Meteo" in page.locator("footer").inner_text())
            ctx.close()

            # 7d ── 清單中間插入項目，既有的勾選不能錯位 ────────
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/keyA/")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            boxes = page.locator(".chk input")
            boxes.nth(1).check()          # 勾「乙」
            page.wait_for_timeout(300)
            r.check("插入測試：先在舊版勾選「乙」", boxes.nth(1).is_checked())

            # 換成中間多一項的版本（同 tripId，共用 localStorage）
            page.goto(f"http://127.0.0.1:{port}/keyB/")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            labels = page.locator(".chk")
            state = {}
            for i in range(labels.count()):
                state[labels.nth(i).inner_text().strip()] = \
                    page.locator(".chk input").nth(i).is_checked()
            r.check("插入項目後：「乙」仍然是勾選的", state.get("乙") is True, str(state))
            r.check("插入項目後：新項目沒有被誤勾", state.get("新插入的") is False, str(state))
            r.check("插入項目後：「丙」沒有被誤勾", state.get("丙") is False, str(state))
            ctx.close()

            # 7e ── 換分組不會讓勾消失 ─────────────────────
            # 打包清單會重新分類（照「這是什麼」分，同一種東西才擺在一起）。
            # 勾選的 key 若含分組名稱，搬到別組的項目已經打的勾就全沒了。
            def checked(pg):
                labels = pg.locator(".chk")
                return {labels.nth(i).inner_text().strip(): pg.locator(".chk input").nth(i).is_checked()
                        for i in range(labels.count())}

            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/regroupA/")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            page.locator(".chk", has_text="乙").locator("input").check()
            page.wait_for_timeout(300)
            page.goto(f"http://127.0.0.1:{port}/regroupB/")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            st = checked(page)
            r.check("換分組：搬到別組的「乙」仍然是勾選的", st.get("乙") is True, str(st))
            r.check("換分組：其他項目沒有被誤勾", st.get("甲") is False and st.get("丙") is False, str(st))
            ctx.close()

            # 已經在手機上打過勾的人，存的是舊格式「清單:分組:項目」——升級後要轉成
            # 「清單:項目」，而且不能照 TRIP_DATA 反查（舊的分組名稱已經不在裡面了）
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{port}/regroupB/")
            page.wait_for_load_state("networkidle")
            page.evaluate("""() => localStorage.setItem('trip:regrouptest', JSON.stringify({
                checks: { __keys_v2: true, 'k:已經不存在的舊分組:丙': true, 'k:甲組:甲': false } }))""")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(500)
            st = checked(page)
            r.check("舊格式轉換：舊分組名稱下的勾，轉換後還在", st.get("丙") is True, str(st))
            r.check("舊格式轉換：沒勾的不會變成勾", st.get("甲") is False, str(st))
            keys = page.evaluate("Object.keys(JSON.parse(localStorage.getItem('trip:regrouptest')).checks)")
            r.check("舊格式轉換：存回去的是新格式", "k:丙" in keys and "__keys_v3" in keys
                    and not any(k.count(":") > 1 for k in keys), str(keys))
            ctx.close()

            # 真實網站的行李清單：五大類照順序、同一張清單裡沒有重複
            # （括號裡的補充說明與標點不算，跟 builder 去重的 packNorm 同一套）
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            page.get_by_role("tab", name=re.compile("行前")).click()
            page.wait_for_timeout(500)
            norm = lambda t: re.sub(r"[\s、，,。．・/／()（）＋+]", "",
                                    re.sub(r"[（(].*?[)）]", "", t)).lower()
            lists = page.locator("#main [data-chk]")
            for i in range(lists.count()):
                card = lists.nth(i)
                title = card.locator(".subject").inner_text().splitlines()[0].strip()
                items = [x.strip() for x in card.locator(".chk").all_inner_texts()]
                seen, dup = {}, []
                for t in items:
                    n = norm(t.splitlines()[0])
                    if n in seen: dup.append(f"{seen[n]} ／ {t}")
                    seen[n] = t
                r.check(f"行李清單沒有重複：{title}", not dup, "；".join(dup) or f"{len(items)} 項")
            pack = page.locator("#main [data-chk]", has_text="行李清單")
            heads = [x.strip() for x in pack.locator(".chk-group h4").all_inner_texts()]
            r.check("行李清單：照五大類排列", heads[:5] == ["證件與錢", "電子產品", "衣物與鞋", "盥洗與藥品", "隨身雜物"],
                    "／".join(heads))
            ctx.close()

            # 7f ── 電腦版的當日路線地圖 ──────────────────
            # 攔掉 Google 的地圖請求：只驗 iframe 的網址對不對，不真的載入
            def map_page(width, url=swap):
                c = browser.new_context(viewport={"width": width, "height": 900})
                c.route("**/maps/embed/**", lambda rt: rt.abort())
                pg = c.new_page()
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.goto(url)
                pg.wait_for_load_state("networkidle")
                pg.wait_for_timeout(500)
                return c, pg, errs

            from urllib.parse import urlparse, parse_qs
            ctx, page, errs = map_page(1400)
            dm = page.locator("#daymap")
            src = page.locator("#daymap iframe").get_attribute("src") or ""
            qs = parse_qs(urlparse(src).query)
            r.check("地圖（寬 1400）：看得到", dm.is_visible())
            r.check("地圖：用 Embed API 的路線模式", src.startswith("https://www.google.com/maps/embed/v1/directions?"), src[:70])
            r.check("地圖：帶金鑰", qs.get("key") == ["TEST_EMBED_KEY"])
            # 第一天：雪梨歌劇院（沒有地圖連結，退回當地語標題＋地點）→ 測試飯店
            r.check("地圖：起點是當天第一站", qs.get("origin") == ["Sydney Opera House, Bennelong Point"], str(qs.get("origin")))
            r.check("地圖：終點是當天最後一站（住宿）", qs.get("destination") == ["測試飯店, 1 Test St"], str(qs.get("destination")))
            r.check("地圖：當天有開車就畫開車路線", qs.get("mode") == ["driving"], str(qs.get("mode")))
            mb, db = page.locator("main").bounding_box(), dm.bounding_box()
            r.check("地圖（寬 1400）：在行程右邊的第三欄", db["x"] > mb["x"] + mb["width"] - 1,
                    f"地圖 x={round(db['x'])}、行程右緣 {round(mb['x'] + mb['width'])}")
            href = page.locator(".daymap-open").get_attribute("href") or ""
            r.check("地圖：「在 Google 地圖開啟」是同一條路線",
                    "maps/dir/?api=1" in href and "travelmode=driving" in href and "Sydney" in href, href[:80])
            # 沒有景點的分頁不畫。但版面寬度不能跟著變：退回兩欄會整個重新置中，
            # 切分頁時左邊的按鈕橫向跳將近 200px
            nav_x = page.locator(".nav-bar").bounding_box()["x"]
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            r.check("地圖：沒有景點的那天不顯示", not dm.is_visible())
            nav_x2 = page.locator(".nav-bar").bounding_box()["x"]
            r.check("地圖：切到沒地圖的分頁，左欄不會移動", abs(nav_x2 - nav_x) < 1,
                    f"{round(nav_x)} → {round(nav_x2)}")
            r.check("地圖：沒有 JS 例外", not errs, "；".join(errs))
            ctx.close()

            ctx, page, errs = map_page(1100)
            mb, db = page.locator("main").bounding_box(), page.locator("#daymap").bounding_box()
            r.check("地圖（寬 1100）：放在行程上方", db and db["y"] + db["height"] <= mb["y"] + 1,
                    f"地圖底 {round(db['y'] + db['height']) if db else '—'}、行程頂 {round(mb['y'])}")
            ctx.close()

            ctx, page, errs = map_page(400)
            r.check("地圖（手機）：不顯示", not page.locator("#daymap").is_visible())
            r.check("地圖（手機）：iframe 沒有設網址，不會載入",
                    page.locator("#daymap iframe").get_attribute("src") is None)
            ctx.close()

            # 沒填金鑰的網站（目前真正的九州網站）：電腦寬度也不顯示、不出錯
            ctx, page, errs = map_page(1400, base)
            r.check("地圖：沒有金鑰就不顯示", not page.locator("#daymap").is_visible()
                    and page.locator(".app.has-map").count() == 0)
            r.check("地圖：沒有金鑰也沒有 JS 例外", not errs, "；".join(errs))
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

            # 10 ── 卡片上的私人資料 ───────────────────────
            # 這裡刻意讀磁碟上的檔案而不是 page.content()：要驗的是「送出去的原始碼」
            # 沒有明文，而 page.content() 是當下的 DOM，解鎖後本來就會有。
            served = (tmp / "vault" / "index.html").read_text(encoding="utf-8")
            leaked = [s for s in VAULT_SECRETS if s in served]
            r.check("私人資料：原始碼裡沒有明文與密語", not leaked, "、".join(leaked))

            ctx = browser.new_context()
            page = ctx.new_page()
            vault_errors = []
            page.on("pageerror", lambda e: vault_errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/vault/")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            main = page.locator("#main")
            door = page.locator("#main .card", has_text="測試住處門口")
            hotel = page.locator("#main .card", has_text="測試飯店")
            r.check("私人資料：鎖著時兩張卡片各有一顆按鈕",
                    page.locator("#main .secret-ask").count() == 2)
            r.check("私人資料：按鈕寫出這張卡片有什麼", "🔒 顯示門鎖密碼" in door.inner_text())
            r.check("私人資料：鎖著時畫面上沒有內容", "SECRET-4821" not in main.inner_text())

            door.locator(".secret-ask").click()
            r.check("私人資料：按下去就在這張卡片裡出現輸入框",
                    door.locator(".vault-pass").count() == 1 and hotel.locator(".vault-pass").count() == 0)

            door.locator(".vault-pass").fill("wrong pass")
            door.locator(".vault-form button[type=submit]").click()
            page.wait_for_function(
                "document.querySelector('.vault-msg')?.classList.contains('is-err')", timeout=15000)
            r.check("私人資料：密語錯誤時說清楚", "通關密語不對" in door.inner_text())
            r.check("私人資料：密語錯誤時不顯示內容", "SECRET-4821" not in main.inner_text())

            # 大小寫、空白、連字號都不計較；用 Enter 送出等同手機鍵盤的「前往」
            door.locator(".vault-pass").fill("TEST pass 2027")
            door.locator(".vault-pass").press("Enter")
            page.wait_for_selector("#main .secret-val", timeout=15000)
            r.check("私人資料：密語寫法不同（大小寫、空白）也解得開", "SECRET-4821" in door.inner_text())
            r.check("私人資料：解開一次，其他卡片也一起顯示", "ORDER-7731" in hotel.inner_text())
            r.check("私人資料：內容出現在各自的卡片上，沒有混在一起",
                    "ORDER-7731" not in door.inner_text() and "SECRET-4821" not in hotel.inner_text())
            r.check("私人資料：網址變成按鈕",
                    door.locator('a[href="https://example.com/guide"]').count() == 1)
            r.check("私人資料：備註有顯示", "密碼盒掛在門把上" in door.inner_text())

            page.reload()
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("#main .secret-val", timeout=15000)
            r.check("私人資料：勾「記住」後重新整理會自動解開",
                    "SECRET-4821" in main.inner_text() and "ORDER-7731" in main.inner_text())

            page.locator("#main [data-vault-lock]").first.click()
            page.wait_for_timeout(300)
            r.check("私人資料：上鎖後所有卡片都回到按鈕",
                    page.locator("#main .secret-ask").count() == 2 and "SECRET-4821" not in main.inner_text())
            page.reload()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            r.check("私人資料：上鎖後重新整理不會自動解開",
                    page.locator("#main .secret-ask").count() == 2 and "ORDER-7731" not in main.inner_text())

            # 解密完全在手機本機，不需要網路
            ctx.set_offline(True)
            hotel.locator(".secret-ask").click()
            hotel.locator(".vault-pass").fill(VAULT_PASS)
            hotel.locator(".vault-form button[type=submit]").click()
            page.wait_for_selector("#main .secret-val", timeout=15000)
            r.check("私人資料：離線也解得開", "ORDER-7731" in hotel.inner_text())
            r.check("私人資料：沒有 JS 例外", not vault_errors, "; ".join(vault_errors[:2]))
            ctx.close()

            # 11 ── 介紹照片 ──────────────────────────────
            if not photo_src:
                print("  （這個行程沒有 img/intro 照片，跳過介紹照片的測試）")
            else:
                ctx = browser.new_context()
                page = ctx.new_page()
                photo_errors = []
                page.on("pageerror", lambda e: photo_errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/photo/")
                page.wait_for_load_state("networkidle")
                page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=15000)
                page.reload()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(800)

                with_photo = page.locator("#main .card", has_text="有照片的景點")
                without = page.locator("#main .card", has_text="沒照片的景點")
                bar = with_photo.locator(".intro.has-photo summary")
                r.check("介紹照片：有照片的卡片才有淡底",
                        bar.count() == 1 and without.locator(".has-photo").count() == 0)
                bg = bar.evaluate("s => [getComputedStyle(s, '::after').backgroundImage, getComputedStyle(s, '::after').opacity]")
                r.check("介紹照片：收合時那一條鋪著照片", photo_src in bg[0] and float(bg[1]) > 0, f"{bg}")
                pic = with_photo.locator("img.intro-photo")
                r.check("介紹照片：收合時看不到大照片", not pic.is_visible())

                bar.click()
                page.wait_for_function(
                    "document.querySelector('img.intro-photo')?.complete", timeout=10000)
                r.check("介紹照片：展開後看得到大照片",
                        pic.is_visible() and pic.evaluate("i => i.naturalWidth") > 0)
                faded = bar.evaluate("s => getComputedStyle(s, '::after').opacity")
                r.check("介紹照片：展開後淡底收掉", float(faded) == 0, faded)
                credit = with_photo.locator(".photo-credit")
                r.check("介紹照片：照片底下有作者與授權，並連到出處",
                        "測試作者" in credit.inner_text() and "CC BY-SA 4.0" in credit.inner_text()
                        and credit.get_attribute("href", timeout=2000).startswith("https://commons.wikimedia.org/"))
                r.check("介紹照片：沒照片的卡片維持原樣",
                        without.locator("img").count() == 0 and "只有文字的介紹" in without.locator(".intro-body").text_content())

                cached = page.evaluate(
                    "async s => !!(await (await caches.open('trip-img')).match(s))", photo_src)
                r.check("介紹照片：開網站時就先存進 trip-img 快取", cached)

                ctx.set_offline(True)
                page.reload()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(800)
                page.locator("#main .intro.has-photo summary").click()
                page.wait_for_timeout(800)
                r.check("介紹照片：離線也看得到",
                        page.locator("#main img.intro-photo").evaluate("i => i.naturalWidth") > 0)
                r.check("介紹照片：沒有 JS 例外", not photo_errors, "; ".join(photo_errors[:2]))
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
