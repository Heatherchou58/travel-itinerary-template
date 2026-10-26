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
import base64
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
            {"type": "transit", "time": "13:00", "line": "T1 線", "duration": "約 12 分", "stay": 25,
             # 當地語跟站名一樣的只顯示一次；不一樣的照常顯示
             "steps": [{"name": "Circular Quay", "nameLocal": "Circular Quay", "active": True,
                        "note": "提醒框會把這一列撐高，線不能斷"},
                       {"name": "Town Hall", "active": False},
                       {"name": "Central", "nameLocal": "Central Station", "active": True}]},
            {"type": "options", "time": "18:00", "title": "晚餐二選一（也可以兩家都吃）",
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
                 {"name": "優惠券", "coupon": True, "items": [
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
    # 金額有小數（分），用分加總比對，免得浮點數差一點點
    recv = sum(round(r["amt"] * 100) for r in data["rows"] if r["dir"] == "recv")
    pay = sum(round(r["amt"] * 100) for r in data["rows"] if r["dir"] == "pay")
    return recv == pay, f"畫面上應收 {recv / 100}、應付 {pay / 100}"


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


def _even_split_cents(data):
    # 平分就是平分：41,553 四人分，每人 10,388.25，不能有人因為零頭多攤 1 元。
    # 以前一律把零頭給名單第一個人，這五筆會變成 A 10,391、C 10,387、D 10,386
    by = {r["who"]: r for r in data["rows"]}
    owe = [round(by[m]["amt"] * 100) for m in ("A", "C", "D")]
    ok = owe == [1038825] * 3 and by["B"]["dir"] == "recv" and round(by["B"]["amt"] * 100) == sum(owe)
    return ok, f"A {owe[0] / 100} / C {owe[1] / 100} / D {owe[2] / 100}，B 應收 {by['B']['amt']}"


SETTLE_CASES = [
    ("兩人均分 1000 整除", [expense("A", 1000, ["A", "B"])], _two_way_split),
    ("三人均分 1000 除不盡：淨額加總要對得起來",
     [expense("A", 1000, ["A", "B", "C"])], _sum_balanced),
    ("沒列在分攤名單的人不該被算進去",
     [expense("A", 900, ["A", "B"])], _only_split_members),
    ("轉帳建議要是最少筆數",
     [expense("A", 400, ["A", "B", "C", "D"]), expense("B", 400, ["A", "B", "C", "D"])],
     _fewest_transfers),
    ("平分到小數點：同一個人先付五筆、四人平分，另外三人都是 10,388.25",
     [expense("B", h, ["A", "B", "C", "D"]) for h in (14493, 11145, 5838, 3035, 7042)],
     _even_split_cents),
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


def transit_trip() -> dict:
    """沒開車的一天：三站＋住宿，交通方式有搭車也有步行。
    Google 的大眾運輸一次只能查兩點之間，所以整天只標地點、點某一站才看那一段怎麼搭。"""
    trip = json.loads(json.dumps(SWAP_TRIP))
    trip["meta"]["tripId"] = "transittest"
    stop = lambda title, place, mode=None: {"type": "stop", "kind": "sight", "time": "10:00",
                                            "title": title, "place": place, "notes": [], "links": [],
                                            **({"mode": mode} if mode else {})}
    trip["days"][0]["blocks"] = [
        stop("甲站", "A 區"),
        stop("乙站", "B 區", "transit"),
        stop("丙站", "C 區", "walking"),
        {"type": "lodging", "title": "丁旅館", "address": "D 區", "notes": [], "mode": "transit"},
    ]
    # 隔天：地圖要從前一晚的住宿（丁旅館）出發
    trip["days"][1]["blocks"][0:0] = [
        stop("戊站", "E 區", "transit"),
        # 續住同一間：地址寫法跟前一晚不同，名字一樣就要當成同一個地方
        {"type": "lodging", "title": "丁旅館（續住）", "titleLocal": "丁旅館", "address": "D 區 1-2",
         "notes": [], "mode": "transit"}]
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
    # 地圖嵌入金鑰也清掉：測試不拿真的金鑰去連 Google，「沒有金鑰」的情境也用這份副本測
    patched = re.sub(r'"mapsEmbedKey": "[^"]*"', '"mapsEmbedKey": ""', patched)
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
                           ("photo", photo_trip(photo_src)),
                           ("transit", transit_trip())]:
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

            # 離線拿不到檔案時瀏覽器顯示的是錯誤頁，#main 根本不會存在。原本用
            # page.content() 找「503」會被原始碼裡任何含這三個數字的字串誤判
            # （顏色碼、地址都有可能），所以改成讀畫面（CLAUDE.md 第六節）
            r.check("離線重新整理：頁面開得起來",
                    page.title() != "" and page.locator("#main").count() > 0
                    and "503" not in page.locator("body").inner_text())
            days = page.locator(".nav-item").count()
            r.check("離線：行程日期導覽有渲染", days >= 2, f"{days} 個分頁")
            # 資訊（信用卡、免稅、緊急資訊）是出發前就要讀的，排在行前後面
            tabs = [t.split("\n")[0].strip() for t in page.locator(".nav-item").all_inner_texts()]
            if any("資訊" in t for t in tabs):
                r.check("分頁順序：資訊緊接在行前後面", "行前" in tabs[0] and "資訊" in tabs[1], "／".join(tabs))
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

            # 記帳：必填沒填時，錯誤寫在欄位底下、不跳 alert（原本用 alert() 擋住整個畫面）
            said = []
            page.on("dialog", lambda d: (said.append(d.message), d.dismiss()))
            page.locator("#e-item").fill("")
            page.locator("#e-local").fill("")
            page.locator("#e-home").fill("")
            page.locator("#e-add").click()
            page.wait_for_timeout(300)
            r.check("記帳：必填沒填不跳 alert", not said, "／".join(said))
            r.check("記帳：錯誤寫在欄位底下",
                    page.locator("#e-item-err").is_visible() and "項目" in page.locator("#e-item-err").inner_text())
            r.check("記帳：沒填的欄位標成 aria-invalid、用 describedby 綁住錯誤",
                    page.locator("#e-item").get_attribute("aria-invalid") == "true"
                    and page.locator("#e-item").get_attribute("aria-describedby") == "e-item-err")
            r.check("記帳：焦點移到第一個沒填的欄位", page.evaluate("document.activeElement.id") == "e-item")
            # 新增成功要有回饋
            page.locator("#e-item").fill("復原測試")
            page.locator("#e-local").fill("500")
            page.locator("#e-add").click()
            page.wait_for_timeout(500)
            r.check("記帳：新增成功有提示", "已記下" in page.locator("#toast").inner_text(),
                    page.locator("#toast").inner_text())
            # 誤刪可以復原（記帳是同步的，誤刪會讓所有人的裝置上都少一筆）
            page.locator(".exp-item", has_text="復原測試").locator(".exp-del").click()
            page.wait_for_timeout(400)
            r.check("記帳：刪除後項目消失", page.locator(".exp-item", has_text="復原測試").count() == 0)
            page.locator("#toast .toast-act").click()
            page.wait_for_timeout(400)
            r.check("記帳：按「復原」項目回來", page.locator(".exp-item", has_text="復原測試").count() == 1)
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
            tag = ride.locator(".time + .stay")
            r.check("交通卡：總耗時放在時間旁邊", tag.count() == 1 and "總耗時約 25 分" in tag.inner_text(),
                    tag.inner_text() if tag.count() else "沒有標籤")
            # 上一站最後一行字到下一站圓點的距離。以前站與站之間會另外插一段 34px 的連接線，
            # 站點自己也畫線之後就變成一塊空白
            gaps = ride.locator(".route").evaluate("""r => { const st = [...r.querySelectorAll(".route-step")];
                return st.slice(0, -1).map((s, i) => Math.round(
                    st[i + 1].querySelector(".route-dot").getBoundingClientRect().top
                    - s.querySelector(".route-body").lastElementChild.getBoundingClientRect().bottom)); }""")
            r.check("交通卡：站與站之間沒有多餘空白", bool(gaps) and max(gaps) <= 24, f"{gaps} px")
            opt = page.locator("#main .card", has_text="海鮮市場").locator(".subject")
            sizes = opt.evaluate("""s => { const n = s.querySelector(".subject-note");
                return n ? [n.textContent, parseFloat(getComputedStyle(s).fontSize), parseFloat(getComputedStyle(n).fontSize)] : null; }""")
            r.check("二選一：標題最後的括號補充字比較小",
                    bool(sizes) and sizes[0] == "（也可以兩家都吃）" and sizes[2] < sizes[1], f"{sizes}")

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
            # 左上角是「抵達」時間：資料的 time 是從上一站出發（11:30），加上步行 20 分
            r.check("時間：卡片左上角是抵達時間（出發 11:30＋步行 20 分）",
                    opera.locator(".time").inner_text() == "11:50", opera.locator(".time").inner_text())
            r.check("營業時間：獨立一列", "09:00–17:00、週二休" in otext)
            r.check("費用：獨立一列", "A$43（可刷卡）" in otext)
            tel = opera.locator("a.tel")
            r.check("電話：是可以點的連結", tel.count() == 1)
            r.check("電話：撥號只留數字與 +", tel.get_attribute("href") == "tel:+61292507111",
                    tel.get_attribute("href"))
            tb = tel.bounding_box()
            r.check("電話：觸控目標 ≥ 44px", tb and round(tb["height"]) >= 44,
                    f"{round(tb['height']) if tb else '—'}px 高")
            # 點擊範圍是用 padding 撐出來再用負 margin 抵掉的，那一列看起來要跟營業時間一樣高
            row_h = opera.evaluate("""c => { const rows = [...c.querySelectorAll(".meta")];
                const tel = c.querySelector("a.tel").closest(".meta");
                const hours = rows.find(r => r.textContent.includes("09:00"));
                return [tel, hours].map(r => Math.round(r.getBoundingClientRect().height)); }""")
            r.check("電話：那一列不比其他資訊列高", row_h[0] <= row_h[1] + 2, f"電話 {row_h[0]}px／營業時間 {row_h[1]}px")
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
                    page.locator('.shop-i[data-id="優惠券:要現場生成的券"].no-img').count() == 1)
            r.check("購物：誰要的有顯示",
                    "媽媽" in page.locator('.shop-i[data-id="藥妝店:托買的東西"]').inner_text())

            # 券是用掉、不是買到，所以整組都不該有數量與金額
            r.check("購物：券的群組沒有數量與金額欄位",
                    page.locator('.shop-g[data-group="優惠券"] .shop-note').count() == 0
                    and page.locator('.shop-g[data-group="優惠券"] .shop-amt').count() == 0,
                    f"{page.locator('.shop-g[data-group=\"優惠券\"] .shop-note').count()} 個備註／"
                    f"{page.locator('.shop-g[data-group=\"優惠券\"] .shop-amt').count()} 個金額")
            r.check("購物：商品的群組有數量與金額欄位",
                    page.locator('.shop-g[data-group="藥妝店"] .shop-note').count() == 1
                    and page.locator('.shop-g[data-group="藥妝店"] .shop-amt').count() == 1)
            # 券不是待辦事項，是拿出來給店員看的東西，所以也不給勾選框
            r.check("購物：券沒有勾選框",
                    page.locator('.shop-g[data-group="優惠券"] input[type=checkbox]').count() == 0,
                    f"{page.locator('.shop-g[data-group=\"優惠券\"] input[type=checkbox]').count()} 個")
            r.check("購物：券的群組標題顯示張數不是進度",
                    page.locator('.shop-g[data-group="優惠券"] .shop-n').inner_text() == "共 2 張",
                    page.locator('.shop-g[data-group="優惠券"] .shop-n').inner_text())
            r.check("購物：券的名稱還是看得到",
                    "有圖的券" in page.locator('.shop-i[data-id="優惠券:有圖的券"]').inner_text())

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
            page.locator('.shop-i[data-id="優惠券:有圖的券"] .shop-x').click()
            page.wait_for_timeout(400)
            r.check("購物：刪除後項目消失", page.locator(".shop-i").count() == 2)
            # 誤刪可以按「復原」整個拿回來；確認完再刪一次，後面的還原按鈕測試照原本的流程
            page.locator("#toast .toast-act").click()
            page.wait_for_timeout(400)
            r.check("購物：按「復原」項目回來", page.locator(".shop-i").count() == 3)
            page.locator('.shop-i[data-id="優惠券:有圖的券"] .shop-x').click()
            page.wait_for_timeout(400)
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

            # 新增時附圖片與連結：圖存在 IndexedDB（不進 localStorage，免得塞爆連記帳都存不了），
            # 連結只收 http／https
            PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
            idb_count = lambda: page.evaluate("""() => new Promise(ok => {
                const r = indexedDB.open('trip:swaptest-2027:img');
                r.onsuccess = () => { const db = r.result;
                  if (!db.objectStoreNames.contains('img')){ db.close(); return ok(0); }
                  const c = db.transaction('img').objectStore('img').count();
                  c.onsuccess = () => { ok(c.result); db.close(); }; };
                r.onerror = () => ok(-1); })""")
            form = page.locator('.shop-add[data-addto="藥妝店"]')
            form.locator('[name="n"]').fill("附圖的東西")
            form.locator('[name="u"]').fill("www.example.com/coupon")
            form.locator('[name="f"]').set_input_files({"name": "c.png", "mimeType": "image/png", "buffer": PNG})
            r.check("附圖片：選了圖，按鈕變成「已選圖片」", "已選圖片" in form.locator(".shop-file").inner_text())
            form.locator('button[type="submit"]').click()
            page.wait_for_timeout(800)
            mine = page.locator('.shop-g[data-group="藥妝店"] .shop-i', has_text="附圖的東西")
            src = lambda: mine.locator("img.shop-img").get_attribute("src") or ""
            try: mine.locator("img.shop-img[src^='blob:']").wait_for(timeout=5000)
            except Exception: pass
            # 圖是非同步從 IndexedDB 讀出來的，等它補上網址再檢查
            blob_ready = """() => [...document.querySelectorAll('img[data-uimg]')].some(i => i.src.startsWith('blob:'))"""
            try: page.wait_for_function(blob_ready, timeout=5000)
            except Exception: pass
            r.check("附圖片：新增的項目有縮圖（從 IndexedDB 讀出來）", src().startswith("blob:"), src()[:30])
            link = mine.locator(".shop-link")
            r.check("附連結：沒打 https:// 的自動補上、按鈕寫「開啟連結」",
                    link.get_attribute("href") == "https://www.example.com/coupon" and link.inner_text() == "開啟連結",
                    f"{link.get_attribute('href')}／{link.inner_text()}")
            r.check("附圖片：圖存在 IndexedDB、沒有塞進 localStorage",
                    idb_count() == 1 and "data:image" not in page.evaluate("JSON.stringify(localStorage)"))
            mine.locator("img.shop-img").click()
            page.wait_for_timeout(300)
            r.check("附圖片：自己加的圖也能點開放大", page.locator("#zoom[open]").count() == 1)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            page.locator(".nav-item").nth(1).click()
            try: page.wait_for_function(blob_ready, timeout=5000)
            except Exception: pass
            r.check("附圖片：重新整理後圖還在", src().startswith("blob:"), src()[:30])
            # 不合格的連結擋下來：javascript: 點了會在網站裡執行程式
            # 錯誤改成寫在畫面上的提示（原本是 alert）；同時確認沒有任何對話框跳出來——
            # 這裡填的就是 javascript:alert(1)，有對話框就代表網址被執行了
            dialogs = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            form.locator('[name="n"]').fill("壞連結")
            form.locator('[name="u"]').fill("javascript:alert(1)")
            form.locator('button[type="submit"]').click()
            page.wait_for_timeout(500)
            said = page.locator("#toast").inner_text()
            r.check("附連結：javascript: 這種網址擋下來、不新增、提示寫在畫面上",
                    page.locator(".shop-i", has_text="壞連結").count() == 0 and "網址" in said and not dialogs,
                    said + ("／對話框：" + "、".join(dialogs) if dialogs else ""))
            # 券的群組加的連結，按鈕寫「開啟優惠券」
            cform = page.locator('.shop-add[data-addto="優惠券"]')
            cform.locator('[name="n"]').fill("自己加的券")
            cform.locator('[name="u"]').fill("https://example.com/my-coupon")
            cform.locator('button[type="submit"]').click()
            page.wait_for_timeout(500)
            r.check("附連結：優惠券那組的按鈕寫「開啟優惠券」",
                    page.locator(".shop-i", has_text="自己加的券").locator(".shop-link").inner_text() == "開啟優惠券")
            mine.locator(".shop-x").click()
            # 圖存在 IndexedDB、刪了回不來，所以等 6 秒「復原」時限過了才真的刪（提示收起來就是過了）
            page.wait_for_timeout(400)
            r.check("附圖片：復原時限內圖還留著（按復原拿得回來）", mine.count() == 0 and idb_count() == 1,
                    f"還剩 {idb_count()} 張")
            page.wait_for_function("!document.querySelector('#toast')?.classList.contains('show')", timeout=9000)
            page.wait_for_timeout(500)
            r.check("附圖片：復原時限過了，IndexedDB 裡的圖也一起刪", mine.count() == 0 and idb_count() == 0,
                    f"還剩 {idb_count()} 張")

            # 已經在清單裡的項目（這裡用 Excel 給的那一項）之後再補圖、換圖、移除圖
            seed = page.locator('.shop-i[data-id="藥妝店:托買的東西"]')
            r.check("補圖：沒有圖的項目有「加圖片」", seed.locator(".shop-addimg").count() == 1)
            seed.locator("[data-simg]").set_input_files({"name": "c.png", "mimeType": "image/png", "buffer": PNG})
            try: page.wait_for_function(blob_ready, timeout=5000)
            except Exception: pass
            seed_src = lambda: seed.locator("img.shop-img").get_attribute("src") or "" if seed.locator("img.shop-img").count() else ""
            r.check("補圖：選了圖就出現在卡片上、「加圖片」收起來",
                    seed_src().startswith("blob:") and seed.locator(".shop-addimg").count() == 0 and idb_count() == 1,
                    seed_src()[:30])
            r.check("補圖：勾選和金額沒有被洗掉",
                    seed.locator("input[type=checkbox]").is_checked() and seed.locator(".shop-amt").input_value() == "1600")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            page.locator(".nav-item").nth(1).click()
            try: page.wait_for_function(blob_ready, timeout=5000)
            except Exception: pass
            r.check("補圖：重新整理後還在", seed_src().startswith("blob:"), seed_src()[:30])
            seed.locator("img.shop-img").click()
            page.wait_for_timeout(300)
            acts = page.locator("#zoom .zoom-acts")
            r.check("補圖：放大後可以換一張或移除", acts.locator("[data-zimg]").count() == 1 and acts.locator("[data-zdel]").count() == 1)
            before = seed_src()
            acts.locator("[data-zimg]").set_input_files({"name": "d.png", "mimeType": "image/png", "buffer": PNG})
            # 換圖是非同步的：等卡片上出現「新的」圖片網址
            try: page.wait_for_function("""o => { const i = document.querySelector('.shop-i[data-id="藥妝店:托買的東西"] img.shop-img');
                                                  return !!i && i.src.startsWith('blob:') && i.src !== o; }""", arg=before, timeout=5000)
            except Exception: pass
            r.check("補圖：換一張後放大視窗關掉、卡片上還是有圖",
                    page.locator("#zoom[open]").count() == 0 and seed_src().startswith("blob:") and idb_count() == 1)
            seed.locator("img.shop-img").click()
            page.wait_for_timeout(300)
            acts.locator("[data-zdel]").click()
            page.wait_for_timeout(500)
            r.check("補圖：移除圖片後回到「加圖片」、IndexedDB 也清掉",
                    seed.locator("img.shop-img").count() == 0 and seed.locator(".shop-addimg").count() == 1 and idb_count() == 0,
                    f"還剩 {idb_count()} 張")
            page.locator('.shop-img[src*="img/"]').first.click()
            page.wait_for_timeout(300)
            r.check("補圖：Excel 給的圖放大後不能換、不能移除",
                    page.locator("#zoom .zoom-acts").inner_html().strip() == "")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

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
            # 金額有兩位小數，換成分再加總
            advanced = sum(round(float(c[1]) * 100) for c in member_rows) / 100
            owed = sum(round(float(c[2]) * 100) for c in member_rows) / 100
            r.check("CSV 結清摘要：代墊總額加總 = 花費總額", advanced == 1000, f"實際 {advanced}")
            r.check("CSV 結清摘要：應分攤加總 = 花費總額", owed == 1000, f"實際 {owed}")

            screen = page.evaluate(READ_SETTLE)
            csv_net = {c[0]: round(float(c[3]) * 100) for c in member_rows}
            screen_net = {row["who"]: round(row["amt"] * 100) for row in screen["rows"]}
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

            # 2026-11-17 03:00Z = 東京 12:00，上色見熊野座神社（11:05 起、停留 115 分）進行中
            ctx, page = at("2026-11-17T03:00:00Z")
            nowc = page.locator("#main .card.is-now")
            r.check("目前行程：剛好標出一張卡片", nowc.count() == 1, f"{nowc.count()} 張")
            r.check("目前行程：標在上色見熊野座神社上", "上色見熊野座神社" in nowc.inner_text())
            line = page.locator("#main .card.is-now .now-line").inner_text()
            r.check("目前行程：顯示剩餘時間與離開時刻",
                    "還有 1 小時" in line and "13:00" in line, f"「{line}」")
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

            # 東京 11:15：已經從草千里出發、還沒到上色見熊野座神社（11:05 出發、交通 50 分）
            ctx, page = at("2026-11-17T02:15:00Z")
            nowc = page.locator("#main .card.is-now")
            line = nowc.locator(".now-line").inner_text() if nowc.count() else ""
            r.check("在路上：標「前往中」與預計抵達時刻",
                    nowc.count() == 1 and "上色見熊野座神社" in nowc.inner_text() and "前往中" in line and "11:55" in line,
                    f"「{line}」")
            # 卡片左上角是「抵達」時間，要跟「前往中 · 預計 HH:MM 抵達」的時刻一樣
            arrive = re.search(r"預計 (\d{2}:\d{2}) 抵達", line)
            r.check("在路上：卡片左上角就是抵達時間（跟「預計抵達」一致）",
                    nowc.count() == 1 and bool(arrive) and nowc.locator(".time").inner_text() == arrive.group(1),
                    nowc.locator(".time").inner_text() if nowc.count() else "")
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
                    if tabs.nth(i).inner_text().strip().startswith("行前")]
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
            # 群組名是 h3：卡片標題已經是 h2，群組要低一階才不會跳級（第 12 段的標題層級那條）
            heads = [x.strip() for x in pack.locator(".chk-group h3").all_inner_texts()]
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

            # 地圖上方的地點按鈕：點名字只看那一點、再點一次回到路線；勾選框決定畫不畫進路線，
            # 「全選」沒全選就全部勾起來、已經全選就全部取消；二選一共用號碼、一次只勾一個。
            # 點卡片等於點對應的那顆按鈕
            frame = page.locator("#daymap iframe")
            def map_q():
                u = frame.get_attribute("src") or ""
                kind = u.split("/embed/v1/")[1].split("?")[0] if "/embed/v1/" in u else ""
                return kind, parse_qs(urlparse(u).query).get("q", [""])[0]
            def via():
                return parse_qs(urlparse(frame.get_attribute("src") or "").query).get("waypoints", [""])[0]
            chips = page.locator(".daymap-pick button[data-p]")
            pressed = lambda i: chips.nth(i).get_attribute("aria-pressed") == "true"
            all_box = page.locator(".daymap-pick input[data-all]")
            partial = lambda: page.eval_on_selector(".daymap-pick input[data-all]", "e => e.indeterminate")
            box = lambda i: page.locator(f'.daymap-pick input[data-skip="{i}"]')
            open_href = lambda: page.locator(".daymap-open").get_attribute("href") or ""
            opera = page.locator("#main .card[data-mapq]").nth(0)
            dinner = page.locator("#main .card[data-mapq]").nth(1)
            hotel = page.locator("#main .card[data-mapq]").nth(2)
            O, S, K, H = 0, 1, 2, 3   # 歌劇院、海鮮市場、牛排館（二選一）、飯店
            OPERA, HOTEL = "Sydney Opera House, Bennelong Point", "測試飯店, 1 Test St"
            r.check("地點按鈕：第一天沒有前一晚的住宿；二選一共用一個號碼",
                    chips.all_inner_texts() == ["① 雪梨歌劇院", "② 海鮮市場", "② 牛排館", "③ 測試飯店"],
                    "／".join(chips.all_inner_texts()))
            r.check("二選一：一開始只勾第一個，路線經過它",
                    box(S).is_checked() and not box(K).is_checked() and via() == "海鮮市場", via())
            r.check("地點按鈕：一開始算全選（二選一勾一個就算）、沒有只看某一點",
                    all_box.is_checked() and not partial() and not any(pressed(i) for i in range(4)))
            box(K).check(); page.wait_for_timeout(200)
            r.check("二選一：勾另一個，原本的自動取消、路線改經過新的",
                    box(K).is_checked() and not box(S).is_checked() and via() == "牛排館", via())
            r.check("二選一：換選項還是算全選", all_box.is_checked() and not partial())
            dinner.locator(".subject").click(); page.wait_for_timeout(200)
            r.check("二選一：點二選一的卡片，看的是勾著的那個", map_q() == ("place", "牛排館") and pressed(K), str(map_q()))
            dinner.locator(".subject").click(); box(S).check(); page.wait_for_timeout(200)

            chips.nth(O).click(); page.wait_for_timeout(200)
            r.check("地點按鈕：開車的日子點名字，地圖只標那一點", map_q() == ("place", OPERA), str(map_q()))
            r.check("地點按鈕：被點的那顆亮起來，對應的卡片也框起來",
                    pressed(O) and not pressed(H) and "map-focus" in (opera.get_attribute("class") or "")
                    and page.locator(".card.map-focus").count() == 1)
            chips.nth(O).click(); page.wait_for_timeout(200)
            r.check("地點按鈕：再點一次同一顆就回到路線",
                    map_q()[0] == "directions" and not pressed(O) and page.locator(".card.map-focus").count() == 0,
                    str(map_q()))

            # 點卡片：跟點按鈕一樣
            opera.locator(".subject").click(); page.wait_for_timeout(200)
            r.check("點卡片：地圖換成那一點、上面的按鈕跟著亮", map_q() == ("place", OPERA) and pressed(O), str(map_q()))
            opera.locator(".subject").click(); page.wait_for_timeout(200)
            r.check("點卡片：再點一次同一張就回到路線", map_q()[0] == "directions" and not pressed(O), str(map_q()))
            # 卡片裡的連結照常開新分頁，不會順便切換地圖（攔下預設動作，免得真的開出去）
            page.evaluate("""() => { const a = document.querySelector('#main .card[data-mapq] a.btn');
                                     a.addEventListener('click', e => e.preventDefault(), { once: true }); a.click(); }""")
            page.wait_for_timeout(200)
            r.check("點卡片：點卡片裡的按鈕不會切換地圖", map_q()[0] == "directions", str(map_q()))
            hotel.focus(); page.keyboard.press("Enter"); page.wait_for_timeout(200)
            r.check("點卡片：用鍵盤也能選（Enter）", map_q() == ("place", HOTEL) and pressed(H), str(map_q()))
            hotel.locator(".subject").click(); page.wait_for_timeout(200)

            # 勾選
            box(O).uncheck(); page.wait_for_timeout(200)
            r.check("勾選：取消的點不畫進路線", map_q()[0] == "directions" and "Opera" not in open_href()
                    and "Opera" not in (frame.get_attribute("src") or ""), str(map_q()))
            r.check("勾選：取消的點名字劃掉，但還是可以點來看",
                    "is-off" in (page.locator(".daymap-pick span.dm-chip").nth(O).get_attribute("class") or ""))
            r.check("全選：只勾一部分時顯示「–」", partial() and not all_box.is_checked())
            chips.nth(O).click(); page.wait_for_timeout(200)
            r.check("勾選：取消的點點名字照樣能看", map_q() == ("place", OPERA), str(map_q()))
            # 正在只看某一點時改勾選：切回路線，才看得到路線的變化
            box(O).check(); page.wait_for_timeout(200)
            r.check("勾選：只看某一點時改勾選，地圖切回路線",
                    map_q()[0] == "directions" and not pressed(O) and page.locator(".card.map-focus").count() == 0,
                    str(map_q()))
            box(O).uncheck(); page.wait_for_timeout(200)
            all_box.click(); page.wait_for_timeout(200)
            r.check("全選：只勾一部分時按下去，全部勾起來（二選一還是只留一個）",
                    box(O).is_checked() and box(H).is_checked() and box(S).is_checked() and not box(K).is_checked()
                    and all_box.is_checked() and not partial() and map_q()[0] == "directions", str(map_q()))
            all_box.click(); page.wait_for_timeout(200)
            r.check("全選：已經全選時再按一次，全部取消、地圖收起來改成提示",
                    not any(box(i).is_checked() for i in range(4)) and not all_box.is_checked()
                    and not frame.is_visible() and page.locator(".daymap-empty").is_visible()
                    and page.locator(".daymap-open").count() == 0)
            all_box.click(); page.wait_for_timeout(200)
            r.check("全選：全部取消後再按一次，全部勾回來、二選一勾第一個",
                    box(O).is_checked() and box(H).is_checked() and box(S).is_checked() and not box(K).is_checked()
                    and frame.is_visible() and map_q()[0] == "directions", str(map_q()))
            # 換一天再回來：不再只看某一點，取消的勾選保留
            box(O).uncheck(); chips.nth(H).click(); page.wait_for_timeout(200)
            page.locator(".nav-item").nth(1).click(); page.wait_for_timeout(300)
            page.locator(".nav-item").nth(0).click(); page.wait_for_timeout(300)
            r.check("換一天再回來：不再只看某一點、取消的勾選還在",
                    not any(pressed(i) for i in range(4)) and page.locator(".card.map-focus").count() == 0
                    and not box(O).is_checked() and box(H).is_checked(), str(map_q()))
            all_box.click(); page.wait_for_timeout(200)
            r.check("換一天再回來：按全選就是原本的路線",
                    box(O).is_checked() and map_q()[0] == "directions" and "Opera" in open_href(), str(map_q()))
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
            page.locator("#main .card[data-mapq] .subject").first.click(); page.wait_for_timeout(200)
            r.check("地圖（手機）：點卡片不會載入地圖、卡片不能用 Tab 選",
                    page.locator("#daymap iframe").get_attribute("src") is None
                    and page.locator("#main .card[tabindex]").count() == 0)
            # 手機也要有開啟路線的按鈕（地圖本身只在電腦版）。手機上多半用 Google 地圖 App 開
            mobile_btn = page.locator(".daymap-open")
            mhref = mobile_btn.get_attribute("href") if mobile_btn.count() else ""
            r.check("開啟路線按鈕（手機）：也看得到、是整天的路線",
                    mobile_btn.count() == 1 and mobile_btn.is_visible()
                    and "maps/dir/?api=1" in mhref and "origin=" in mhref and "destination=" in mhref, mhref[:80])
            r.check("開啟路線按鈕（手機）：在當天第一張卡片上方、點擊範圍 ≥ 44px",
                    mobile_btn.count() == 1 and mobile_btn.bounding_box()["height"] >= 44
                    and mobile_btn.bounding_box()["y"] < page.locator("#main .card").first.bounding_box()["y"])
            ctx.close()

            # 沒開車的日子：整天用大眾運輸只標地點（Google 加了途經點就畫不出線），
            # 點某一站看「從上一站怎麼去」，交通方式用那張卡片的
            ctx, page, errs = map_page(1400, f"http://127.0.0.1:{port}/transit/")
            frame = page.locator("#daymap iframe")
            def leg():
                u = frame.get_attribute("src") or ""
                q = parse_qs(urlparse(u).query)
                kind = u.split("/embed/v1/")[1].split("?")[0] if "/embed/v1/" in u else ""
                one = lambda k: q.get(k, [""])[0]
                return kind, one("mode"), one("origin") or one("q"), one("destination"), one("waypoints")
            link = page.locator(".daymap-open")
            lq = lambda: parse_qs(urlparse(link.get_attribute("href") or "").query)
            chips = page.locator(".daymap-pick button[data-p]")
            A, B, C, D, E = "甲站, A 區", "乙站, B 區", "丙站, C 區", "丁旅館, D 區", "戊站, E 區"
            r.check("搭車日：整天用大眾運輸、只標各點", leg() == ("directions", "transit", A, D, f"{B}|{C}"), str(leg()))
            r.check("搭車日：整天的 Google 地圖連結用步行（大眾運輸不能加途經點）",
                    lq().get("travelmode") == ["walking"] and "今天的路線" in link.inner_text())
            chips.nth(0).click(); page.wait_for_timeout(200)
            r.check("搭車日：點第一站只標位置（第一天沒有前一晚的住宿）", leg()[:3] == ("place", "", A), str(leg()))
            chips.nth(1).click(); page.wait_for_timeout(200)
            r.check("搭車日：點某一站看從上一站怎麼搭", leg() == ("directions", "transit", A, B, ""), str(leg()))
            r.check("搭車日：連結改成開這一段、用大眾運輸",
                    "這一段" in link.inner_text() and lq().get("travelmode") == ["transit"]
                    and lq().get("origin") == [A] and lq().get("destination") == [B] and "waypoints" not in lq(),
                    link.get_attribute("href")[:90])
            chips.nth(2).click(); page.wait_for_timeout(200)
            r.check("搭車日：交通方式跟著卡片（走過去的那段用步行）", leg() == ("directions", "walking", B, C, ""), str(leg()))
            page.locator('.daymap-pick input[data-skip="2"]').uncheck()   # 不去丙站
            chips.nth(3).click(); page.wait_for_timeout(200)
            r.check("搭車日：上一站被取消就從再上一站出發", leg() == ("directions", "transit", B, D, ""), str(leg()))
            page.locator(".daymap-pick input[data-all]").click(); page.wait_for_timeout(200)
            r.check("搭車日：按全選回到只標各點、連結回到今天的路線",
                    leg() == ("directions", "transit", A, D, f"{B}|{C}") and "今天的路線" in link.inner_text(), str(leg()))

            # 隔天：從前一晚的住宿出發
            page.locator(".nav-item").nth(1).click(); page.wait_for_timeout(300)
            r.check("隔天：第一顆是前一晚的住宿（不編號），當天的點從 ① 開始",
                    chips.all_inner_texts() == ["丁旅館", "① 戊站"]
                    and chips.nth(0).locator("svg.ic use[href='#i-bed']").count() == 1
                    and "前一晚住宿" in (chips.nth(0).get_attribute("title") or ""), "／".join(chips.all_inner_texts()))
            r.check("隔天：路線從前一晚的住宿出發、晚上回到同一間（只列一顆）",
                    leg() == ("directions", "transit", D, D, E), str(leg()))
            chips.nth(1).click(); page.wait_for_timeout(200)
            r.check("隔天：點第一站，看從住宿怎麼過去", leg() == ("directions", "transit", D, E, ""), str(leg()))
            page.locator("#main .card[data-mapq]").nth(1).locator(".subject").click(); page.wait_for_timeout(200)
            r.check("隔天：點晚上的住宿卡，看從最後一站怎麼回去、住宿那顆跟著亮",
                    leg() == ("directions", "transit", E, D, "")
                    and chips.nth(0).get_attribute("aria-pressed") == "true", str(leg()))
            r.check("搭車日：沒有 JS 例外", not errs, "；".join(errs))
            ctx.close()

            # 沒填金鑰的網站（九州網站的副本，金鑰在準備副本時清掉了）：地圖不顯示，但開啟路線的按鈕要有——
            # 它用的是不需要金鑰的 Google 地圖網址
            ctx, page, errs = map_page(1400, base)
            r.check("地圖：沒有金鑰就不顯示", not page.locator("#daymap").is_visible()
                    and page.locator(".app.has-map").count() == 0)
            page.locator(".nav-item", has_text="D2").first.click()
            page.wait_for_timeout(400)
            btn = page.locator(".daymap-open")
            href = btn.get_attribute("href") if btn.count() else ""
            r.check("開啟路線按鈕：沒有金鑰也出現", btn.count() == 1 and btn.is_visible())
            r.check("開啟路線按鈕：是整天的路線（起點、終點、途經點、交通方式）",
                    "maps/dir/?api=1" in href and "travelmode=driving" in href
                    and "origin=" in href and "destination=" in href and "waypoints=" in href, href[:90])
            first = page.locator("#main .card").first.bounding_box()
            bb = btn.bounding_box()
            r.check("開啟路線按鈕：在當天第一張卡片上方", bb["y"] + bb["height"] <= first["y"] + 1,
                    f"按鈕底 {round(bb['y'] + bb['height'])}、第一張卡片頂 {round(first['y'])}")
            # 按鈕是插在 #main 裡的：切來切去不能越積越多
            page.locator(".nav-item", has_text="D3").first.click(); page.wait_for_timeout(300)
            page.locator(".nav-item", has_text="D2").first.click(); page.wait_for_timeout(300)
            r.check("開啟路線按鈕：切分頁不會重複出現", page.locator(".daymap-open").count() == 1)
            page.locator(".nav-item", has_text="行前").first.click(); page.wait_for_timeout(300)
            r.check("開啟路線按鈕：沒有景點的分頁不出現", page.locator(".daymap-open").count() == 0)
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
            r.check("私人資料：按鈕寫出這張卡片有什麼", "顯示門鎖密碼" in door.inner_text())
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
            # 「上鎖」在框的右上角，跟第一筆資料同一列，不另外佔一行
            geo = hotel.locator(".secret").evaluate("""s => { const b = s.getBoundingClientRect(),
                l = s.querySelector('.secret-lock').getBoundingClientRect(),
                first = s.querySelector('.secret-items > *').getBoundingClientRect();
                return { right: Math.round(b.right - l.right), lockLeft: l.left - b.left, half: b.width / 2,
                         lockTop: Math.round(l.top - b.top), firstBottom: Math.round(first.bottom - b.top),
                         belowLast: Math.round(l.bottom - s.querySelector('.secret-items').getBoundingClientRect().bottom) }; }""")
            r.check("私人資料：「上鎖」在右上角、跟第一筆資料同一列",
                    geo["lockLeft"] > geo["half"] and geo["lockTop"] < geo["firstBottom"] and geo["belowLast"] <= 0, str(geo))

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
                r.check("介紹照片：展開後看得到照片",
                        pic.is_visible() and pic.evaluate("i => i.naturalWidth") > 0)
                faded = bar.evaluate("s => getComputedStyle(s, '::after').opacity")
                r.check("介紹照片：展開後淡底收掉", float(faded) == 0, faded)
                # 文字在上、照片在下；照片照原本的比例完整顯示，不能被裁切
                lay = with_photo.locator(".intro-body").evaluate("""b => {
                  const ps = b.querySelectorAll('p'), img = b.querySelector('img.intro-photo');
                  const last = ps[ps.length - 1].getBoundingClientRect(), r = img.getBoundingClientRect();
                  return { gap: Math.round(r.top - last.bottom), shown: +(r.width / r.height).toFixed(3),
                           natural: +(img.naturalWidth / img.naturalHeight).toFixed(3) };
                }""")
                r.check("介紹照片：文字在上、照片在下，沒有疊在一起", lay["gap"] >= 0, str(lay))
                r.check("介紹照片：照片照原本比例完整顯示，沒有裁切", abs(lay["shown"] - lay["natural"]) < 0.02, str(lay))
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

            # 12 ── ui-ux-pro-max 規則：之後所有 UI 變更都要過 ──────────
            # 規則出處：ui-ux-pro-max-skill-main/.claude/skills/ui-ux-pro-max/references/quick-reference.md
            ctx = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True, has_touch=True)
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)
            UX_PROBE = """() => {
                const vis = e => e.checkVisibility() && e.getBoundingClientRect().width > 0;
                const nums = c => c.match(/[0-9.]+/g).map(Number);
                const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; };
                  return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
                const over = (fg, bg) => { const a = fg.length > 3 ? fg[3] : 1; return [0, 1, 2].map(i => fg[i] * a + bg[i] * (1 - a)); };
                const bgOf = e => { const layers = [];
                  for (let n = e; n; n = n.parentElement){ const c = nums(getComputedStyle(n).backgroundColor);
                    const a = c.length > 3 ? c[3] : 1; if (a > 0) layers.push(c); if (a >= 1) break; }
                  let bg = [255, 255, 255]; for (const l of layers.reverse()) bg = over(l, bg); return bg; };
                const name = e => (e.innerText || e.getAttribute("aria-label") || e.tagName).trim().slice(0, 20);
                const out = { contrast: [], target: [], spacing: [], alt: [], heading: [], emoji: [], label: [], overflow: 0 };
                for (const e of document.querySelectorAll("body *")){
                  if (!vis(e)) continue;
                  const own = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
                  if (!own) continue;
                  const s = getComputedStyle(e), fs = parseFloat(s.fontSize);
                  const bg = bgOf(e), fg = over(nums(s.color), bg);
                  const L1 = lum(fg), L2 = lum(bg), ratio = (Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05);
                  const need = (fs >= 24 || (fs >= 18.66 && +s.fontWeight >= 700)) ? 3 : 4.5;
                  if (ratio < need) out.contrast.push(`${name(e)} ${ratio.toFixed(2)}`);
                }
                /* .shop-x 的圓圈只有 28px，點擊範圍由 ::after 撐到 44px，不量本體 */
                const T = [...document.querySelectorAll('a[href], button, select, textarea, summary, [role=tab], label.chip, input:not([type=checkbox]):not([type=radio]):not([type=hidden])')]
                  .filter(e => vis(e) && !e.matches(".shop-x"));
                const R = T.map(e => e.getBoundingClientRect());
                T.forEach((e, i) => { if (R[i].width < 44 || R[i].height < 44) out.target.push(`${name(e)} ${Math.round(R[i].width)}×${Math.round(R[i].height)}`); });
                for (let i = 0; i < T.length; i++) for (let j = i + 1; j < T.length; j++){
                  if (T[i].contains(T[j]) || T[j].contains(T[i])) continue;
                  const A = R[i], B = R[j];
                  const dx = Math.max(A.left, B.left) - Math.min(A.right, B.right);
                  const dy = Math.max(A.top, B.top) - Math.min(A.bottom, B.bottom);
                  if ((dy < 0 && dx < 8) || (dx < 0 && dy < 8)) out.spacing.push(`${name(T[i])}／${name(T[j])}`);
                }
                for (const im of document.querySelectorAll("img")) if (vis(im) && !im.getAttribute("alt")) out.alt.push(im.getAttribute("src"));
                let last = 0;
                for (const h of document.querySelectorAll("h1,h2,h3,h4,h5,h6")){ if (!vis(h)) continue;
                  const lv = +h.tagName[1]; if (lv > last + 1) out.heading.push(`${h.tagName} ${h.innerText.slice(0, 12)}`); last = lv; }
                /* 看得見的標籤：包住它或 for 指向它的 label、或 aria-labelledby 指向看得見的文字。
                   提示文字（placeholder）與 aria-label 都不算 */
                for (const el of document.querySelectorAll("input, select, textarea")){
                  if (el.type === "hidden" || el.type === "file") continue;
                  const box = (el.type === "checkbox" || el.type === "radio") ? (el.closest("label") || el) : el;
                  if (!vis(box)) continue;
                  const named = [...(el.labels || [])].some(l => vis(l) && l.textContent.trim())
                    || (el.getAttribute("aria-labelledby") || "").split(" ").some(id => {
                         const n = document.getElementById(id); return n && vis(n) && n.textContent.trim(); });
                  if (!named) out.label.push(el.name || el.className || el.type);
                }
                const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                for (let t; (t = tw.nextNode());){ const el = t.parentElement;
                  if (!el || el.closest("#app-title, script, style") || !vis(el)) continue;
                  const m = t.textContent.match(/\\p{Extended_Pictographic}/gu);
                  if (m) out.emoji.push(`${m.join("")} ${t.textContent.trim().slice(0, 12)}`); }
                out.overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
                return out;
            }"""
            fails = {k: [] for k in ["contrast", "target", "spacing", "alt", "heading", "emoji", "label", "focus", "overflow"]}
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(300)
                page.evaluate("document.querySelectorAll('#main details').forEach(d => d.open = true)")
                if page.locator("#main .secret-ask").count():   # 私人資料的密語框按了才出現
                    page.locator("#main .secret-ask").first.click()
                    page.wait_for_timeout(100)
                    # 密語框一出現就取得焦點：不能被置頂的分頁列蓋住（focus-not-obscured）
                    if page.evaluate("document.activeElement.getBoundingClientRect().top < document.querySelector('.nav-bar').getBoundingClientRect().bottom"):
                        fails["focus"].append(tab)
                    page.evaluate("window.scrollTo(0, 0)")   # 間距量的是版面，不是捲到一半疊在分頁列下的樣子
                got = page.evaluate(UX_PROBE)
                for k in ["contrast", "target", "spacing", "alt", "heading", "emoji", "label"]:
                    fails[k] += [f"{tab}：{x}" for x in got[k]]
                if got["overflow"] > 0: fails["overflow"].append(f"{tab}：{got['overflow']}px")
            for k, label in [("contrast", "文字對比 ≥ 4.5:1（大字 3:1）"), ("target", "觸控目標 ≥ 44×44px"),
                             ("spacing", "觸控目標之間 ≥ 8px"), ("alt", "圖片都有替代文字"),
                             ("heading", "標題層級不跳級"), ("emoji", "介面不用 emoji 當圖示（網站標題除外）"),
                             ("label", "輸入框都有看得見的標籤（不只提示文字）"),
                             ("focus", "取得焦點的密語框沒有被置頂分頁列蓋住"),
                             ("overflow", "375px 不會橫向捲動")]:
                r.check(f"UI 規則：{label}", not fails[k], "；".join(sorted(set(fails[k]))[:4]) or "各分頁都過")
            ctx.close()

            ctx = browser.new_context(viewport={"width": 375, "height": 812}, reduced_motion="reduce")
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            moving = set()
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tabs.nth(i).click()
                page.wait_for_timeout(300)
                moving |= set(page.evaluate("document.getAnimations().filter(a => a.playState === 'running' && a.effect.getTiming().duration > 1).map(a => a.animationName || 'transition')"))
            r.check("UI 規則：減少動態效果時沒有動畫在跑", not moving, "、".join(sorted(moving)))
            ctx.close()

            # 楓葉飄落（maple）：手機 4–7 片、電腦 8–14 片，週期 18–35 秒，大小 10–24px，透明度 .12–.30。
            # 一片一個元素，用 getAnimations 數真的在跑的，不是數 DOM
            LEAF_PROBE = """() => {
                const leaves = [...document.querySelectorAll('.leaves i')].filter(e => e.checkVisibility());
                const s = leaves.map(e => getComputedStyle(e));
                const running = document.getAnimations().filter(a => a.animationName === 'leaf-fall' && a.playState === 'running').length;
                return { n: leaves.length, running,
                         dur: s.map(x => parseFloat(x.animationDuration)),
                         size: s.map(x => parseFloat(x.width)),
                         op: s.map(x => parseFloat(x.opacity)),
                         clickThrough: getComputedStyle(document.querySelector('.leaves')).pointerEvents === 'none' };
            }"""
            for w, lo, hi in ((375, 4, 7), (1400, 8, 14)):
                ctx = browser.new_context(viewport={"width": w, "height": 812})
                page = ctx.new_page()
                page.goto(base)
                page.wait_for_load_state("networkidle")
                if page.evaluate("document.documentElement.dataset.theme") != "maple":
                    ctx.close(); continue
                lf = page.evaluate(LEAF_PROBE)
                r.check(f"楓葉（寬 {w}）：同時 {lo}–{hi} 片，而且都在動",
                        lo <= lf["n"] <= hi and lf["running"] == lf["n"], f"{lf['n']} 片、{lf['running']} 片在動")
                r.check(f"楓葉（寬 {w}）：週期 18–35 秒、大小 10–24px、透明度 .12–.30",
                        all(18 <= d <= 35 for d in lf["dur"]) and all(10 <= z <= 24 for z in lf["size"])
                        and all(.12 <= o <= .30 for o in lf["op"]),
                        f"週期 {min(lf['dur'])}–{max(lf['dur'])}s、大小 {min(lf['size'])}–{max(lf['size'])}px、"
                        f"透明度 {min(lf['op'])}–{max(lf['op'])}")
                r.check(f"楓葉（寬 {w}）：不接點擊", lf["clickThrough"])
                ctx.close()

            # 深色模式也要過對比。五個主題各一套深色，而且有幾個地方是「光換 token 不夠」的
            # （.btn 的底色是 --c-text，深色下會翻過來）——只改淺色就會在這裡失敗。
            ctx = browser.new_context(viewport={"width": 375, "height": 812}, color_scheme="dark")
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            # 深色模式特有的第二條：面跟面要分得開。上面那支探針只量文字，量不到
            # 「說明框的底色跟卡片底色幾乎一樣」——使用者是看畫面才發現的，當時
            # 區別度只有 1.02（1.00 ＝ 完全同色）。
            FLAT_PROBE = """() => {
                const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; };
                  return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
                const nums = c => c.match(/[0-9.]+/g).map(Number);
                const out = [];
                for (const el of document.querySelectorAll('#main .note-box, #main .src, #main .wx-day, #main .stay.move, #main .chk-group h3, #main .card.lodge')){
                  // 住宿卡本身就是一張卡片，要跟頁面底色比
                  const card = el.matches('.card') ? document.body : el.closest('.card');
                  if (!card || !el.checkVisibility()) continue;
                  const a = lum(nums(getComputedStyle(el).backgroundColor));
                  const b = lum(nums(getComputedStyle(card).backgroundColor));
                  const ratio = (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
                  if (ratio < 1.2) out.push(`${el.className.split(' ')[0]} ${ratio.toFixed(2)}`);
                }
                return out;
            }"""
            dark_fail, dark_flat = [], []
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(300)
                page.evaluate("document.querySelectorAll('#main details').forEach(d => d.open = true)")
                if page.locator("#main .secret-ask").count():
                    page.locator("#main .secret-ask").first.click()
                    page.wait_for_timeout(100)
                    page.evaluate("window.scrollTo(0, 0)")
                dark_fail += [f"{tab}：{x}" for x in page.evaluate(UX_PROBE)["contrast"]]
                dark_flat += [f"{tab}：{x}" for x in page.evaluate(FLAT_PROBE)]
            r.check("UI 規則：深色模式的文字對比 ≥ 4.5:1（大字 3:1）", not dark_fail,
                    "；".join(sorted(set(dark_fail))[:4]) or "各分頁都過")
            r.check("UI 規則：深色模式下說明框等區塊跟卡片分得開", not dark_flat,
                    "；".join(sorted(set(dark_flat))[:4]) or "各分頁都過")
            ctx.close()

            # 配色切換鍵：跟隨系統／淺色／深色三段循環，記在本機，手動指定要蓋過系統。
            # 這個情境的系統偏好是深色，所以「手動選淺色」能不能壓過它是重點。
            ctx = browser.new_context(viewport={"width": 375, "height": 812}, color_scheme="dark")
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            btn = page.locator("#scheme")
            seq = []
            for _ in range(4):
                seq.append((page.evaluate("document.documentElement.dataset.scheme"),
                            btn.get_attribute("aria-label") or ""))
                btn.click()
                page.wait_for_timeout(150)
            r.check("配色：預設跟隨系統（這裡是深色）",
                    seq[0][0] == "dark" and "跟隨系統" in seq[0][1], str(seq[0]))
            r.check("配色：手動選淺色會蓋過系統的深色",
                    seq[1][0] == "light" and "淺色" in seq[1][1], str(seq[1]))
            r.check("配色：可以手動選深色", seq[2][0] == "dark" and "深色" in seq[2][1], str(seq[2]))
            r.check("配色：繞一圈回到跟隨系統",
                    seq[3][0] == "dark" and "跟隨系統" in seq[3][1], str(seq[3]))
            # 上面按了四次，現在停在「淺色」；重新整理要記得（<head> 那段在繪製前就套上）
            page.reload()
            page.wait_for_load_state("networkidle")
            r.check("配色：重新整理後記得選擇",
                    page.evaluate("document.documentElement.dataset.scheme") == "light",
                    page.evaluate("document.documentElement.dataset.scheme"))
            ctx.close()

            # 電腦版才有的介面（右側地圖上方的地點列）也要過標籤這條
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            unlabeled = []
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(300)
                unlabeled += [f"{tab}：{x}" for x in page.evaluate(UX_PROBE)["label"]]
            r.check("UI 規則：電腦版輸入框也都有看得見的標籤", not unlabeled, "；".join(sorted(set(unlabeled))[:4]) or "各分頁都過")
            ctx.close()

            # 配色：⚠️ 開頭的句子拆成自己的提醒框（.note-box.warn），一般說明框裡不能再混著提醒。
            # 用圖示判斷：⚠ 是唯一會換成 warning 圖示的 emoji（EMOJI_IC），文字比對換了行程就不準
            ctx = browser.new_context(viewport={"width": 375, "height": 812})
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            mixed, warn_boxes, warn_bad = [], 0, []
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(200)
                res = page.evaluate("""() => ({
                  mixed: document.querySelectorAll('#main .note-box:not(.warn) > .ic-warn').length,
                  boxes: [...document.querySelectorAll('#main .note-box.warn')].filter(e => e.checkVisibility()).length,
                  bad: [...document.querySelectorAll('#main .note-box.warn')]
                         .filter(e => !(e.firstElementChild && e.firstElementChild.matches('.ic-warn'))).length,
                })""")
                if res["mixed"]: mixed.append(f"{tab} {res['mixed']} 句")
                if res["bad"]: warn_bad.append(f"{tab} {res['bad']} 個")
                warn_boxes += res["boxes"]
            r.check("配色：⚠️ 提醒拆成獨立的提醒框，不混在一般說明裡", not mixed and not warn_bad and warn_boxes > 0,
                    f"提醒框 {warn_boxes} 個" + (f"；混在說明裡：{'、'.join(mixed)}" if mixed else "")
                    + (f"；提醒框開頭不是提醒圖示：{'、'.join(warn_bad)}" if warn_bad else ""))

            # 配色：湖水藍、森林綠、米黃這些是楓葉主題自己的顏色。共用版面只認 token，
            # 別的主題沒指定就退回它自己的主色——換成其他主題後，畫面上不能出現這幾個顏色
            MAPLE_ONLY = """() => {
              const ban = ['63, 137, 144', '44, 98, 104', '217, 233, 231', '82, 107, 70', '228, 235, 220',
                           '248, 240, 227', '248, 238, 220', '245, 218, 214', '243, 227, 199',
                           '111, 183, 189', '143, 205, 210', '33, 58, 60', '54, 44, 36', '44, 37, 26'];
              const hit = new Set();
              for (const el of document.querySelectorAll('header, header *, .sidenav *, #main, #main *')){
                const cs = getComputedStyle(el);
                for (const v of [cs.color, cs.backgroundColor, cs.borderLeftColor, cs.borderTopColor])
                  for (const b of ban) if (v.includes(`(${b}`)) hit.add(`${el.className.baseVal ?? el.className} ${b}`);
              }
              return [...hit];
            }"""
            leaks = []
            for scheme in ("light", "dark"):
                for theme in ("snow", "sakura", "ocean", "city"):
                    page.evaluate(f"document.documentElement.dataset.scheme = '{scheme}'; document.documentElement.dataset.theme = '{theme}'")
                    for day in ("d1", "d2", "info"):
                        if page.locator(f"#tab-{day}").count():
                            page.locator(f"#tab-{day}").click()
                            page.wait_for_timeout(100)
                            leaks += [f"{scheme}/{theme}/{day}：{x}" for x in page.evaluate(MAPLE_ONLY)]
            r.check("配色：楓葉主題的第二層顏色不會出現在其他主題", not leaks,
                    "；".join(sorted(set(leaks))[:4]) or "四個主題 × 淺色深色都沒有")
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
