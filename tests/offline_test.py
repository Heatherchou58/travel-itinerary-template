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
             # 當地語的名稱與地點都不顯示在卡片上（使用者的決定），但資料要留著：
             # 地圖查詢用的是「當地語名稱, 地點」
             "place": "Bennelong Point", "placeLocal": "Bennelong Pt.",
             "stay": 90, "notes": ["測試備註"],
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
             "place": "Darling Harbour", "placeLocal": "Darling Hbr.",
             # 選項卡自己的按鈕（整頓飯共用的，例如停車場）：放在標題列右上角
             "links": [{"label": "停車場", "url": "https://example.com/parking", "style": "booking"}],
             # 只有一顆按鈕的選項按鈕放右邊，兩顆以上的放下面
             "choices": [{"title": "海鮮市場", "links": [{"label": "地圖", "url": "https://example.com/m", "style": "map"}]},
                         {"title": "牛排館", "links": [{"label": "地圖", "url": "https://example.com/s", "style": "map"},
                                                     {"label": "訂位", "url": "https://example.com/r", "style": "booking"}]}]},
            # 跨過午夜：23:30 ＋ 90 分 = 隔天 01:00。只印 01:00 會看起來像倒退。
            # 故意用交通卡——它不帶地點，才不會多跑進電腦版地圖的地點列裡
            {"type": "transit", "time": "23:30", "title": "深夜巴士", "line": "N1 線", "stay": 90,
             "steps": [{"name": "Night Depot", "active": True}, {"name": "Airport", "active": True}]},
            {"type": "lodging", "title": "測試飯店", "titleLocal": "Test Hotel", "address": "1 Test St",
             "notes": ["含早餐"], "stay": 15, "travel": 15, "mode": "driving",
             "hours": "Check-in 15:00／退房 10:00",
             # 地圖按鈕用 place ID 釘住：右側的嵌入地圖要用 ID 查，不能只給名稱
             # （名稱解析不出來的話整條路線會退成世界地圖，九州的 ORIX 踩過）
             "links": [{"label": "地圖", "style": "map",
                        "url": "https://www.google.com/maps/search/?api=1&query=%E6%B8%AC%E8%A9%A6%E9%A3%AF%E5%BA%97%2C%201%20Test%20St"
                               "&query_place_id=TEST_PID_HOTEL"}]},
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
            {"type": "board"},
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


def fund_trip(trip_id: str = "fundtest") -> dict:
    """公積金：一個沒有日期的分頁（旅途工具）＋記帳（共用同一道鎖）＋一天（公積金花費選日期用）。"""
    trip = json.loads(json.dumps(SETTLE_TRIP))
    trip["meta"]["tripId"] = trip_id
    trip["days"] = [
        {"id": "fund", "nav": "🐷 公積金", "blocks": [{"type": "fund"}]},
        {"id": "wallet", "nav": "💰 記帳", "blocks": [{"type": "expenses"}]},
        {"id": "d1", "nav": "D1 測試", "date": "2027-05-01", "title": "Day 1", "blocks": [
            {"type": "stop", "kind": "sight", "time": "10:00", "title": "測試景點", "notes": [], "links": []}]},
    ]
    return trip


# 假的 Firebase：攔掉 gstatic 的三個模組，換成一份放在測試程式（Python）記憶體裡的資料庫，
# 兩個瀏覽器（兩支手機）共用。setDoc 整份覆寫、onSnapshot 每 200ms 看一次版本，跟真的一樣是
# 「整份文件」的語意。離線時（navigator.onLine 為 false）收不到更新，跟真的手機一樣。
FAKE_FB = {
    "firebase-app.js": "export const initializeApp = c => ({ c });",
    "firebase-auth.js": ("export const getAuth = () => ({});\n"
                         "export const signInAnonymously = async () => ({ user: { uid: 'fake-' + Math.random().toString(36).slice(2) } });"),
    "firebase-firestore.js": """export const getFirestore = () => ({});
export const doc = (db, ...p) => p.join('/');
export async function setDoc(path, data){
  if (!navigator.onLine) throw new Error('offline');
  await window.__fbSet(path, JSON.stringify(data));
}
export function onSnapshot(path, cb){
  let ver = -1;
  const tick = async () => {
    if (!navigator.onLine) return;
    const r = await window.__fbGet(path);
    if (r && r.v !== ver){ ver = r.v; cb({ data: () => JSON.parse(r.data), exists: () => true }); }
  };
  tick(); setInterval(tick, 200);
  return () => {};
}""",
}


def seed_shop(page) -> None:
    """購物清單放幾項進本機再重新整理：UI 探針才量得到一項一列、代購結算、鉛筆打開的編輯欄。
    其中一項是改版前的格式（「數量／備註」寫在 edits.note），確認舊資料照樣畫得出來。"""
    page.evaluate("""() => {
        const k = 'trip:kyushu-fukuoka-2026';
        const d = JSON.parse(localStorage.getItem(k) || '{}');
        d.shopping = {
          added: [
            { id: '+ux1', group: '藥妝店', name: '面膜', desc: '肌研 50ml', qty: 2, who: '媽媽' },
            { id: '+ux2', group: '零食食品', name: 'KitKat 抹茶', who: '同事' },
            { id: '+ux3', group: '伴手禮', name: '博多通りもん' } ],
          edits: { '+ux1': { done: true, amount: 2560 }, '+ux3': { note: '3 盒' } },
          removed: [], groups: [], closed: [] };
        localStorage.setItem(k, JSON.stringify(d));
    }""")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)


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
        {**stop("戊站", "E 區", "transit"), "travel": 15},   # 要先出門：起床時間照出門時間算
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


def guard_shared_code(app_dir: Path) -> None:
    """共用程式（app/）裡不能有 Firebase 設定或金鑰。

    disable_firebase 只清 index.html——設定一直放在資料那一段。哪天有人把設定搬進
    app.js，測試副本就會連上真正的 Firestore、把測試資料寫進大家的記帳，而且不會有人發現。"""
    for f in app_dir.glob("*.js"):
        code = f.read_text(encoding="utf-8")
        if re.search(r"(const|let|var)\s+FIREBASE_CONFIG\s*=\s*\{", code):
            sys.exit(f"準備測試副本失敗：{f.name} 裡有 FIREBASE_CONFIG，disable_firebase 清不到它")
        if re.search(r"AIza[A-Za-z0-9_-]{20,}", code):
            sys.exit(f"準備測試副本失敗：{f.name} 裡有 API 金鑰")


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


# 留言板的種子資料：第 12 段的 UI 探針只量「畫面上真的有的元素」，留言板空著的話
# 留言列（.bd-who／.bd-at／.bd-text）與 ✕ 的觸控範圍根本量不到。
# 三則都要在畫面上，三個分支才量得到：
#   自己這台送的（有 ✕）、旅伴送的（沒有 ✕）、沒登記過的裝置送的（標成可疑）。
# 另外要連「這台裝置的登記」一起放進去，不然畫面停在登記表單、根本沒有留言表單。
BOARD_ME = "白"
BOARD_DEV_ID = "11111111-2222-3333-4444-555555555555"
BOARD_DEV_NAME = "白的 iPhone"
BOARD_OLD_ID = "99999999-8888-7777-6666-555555555555"   # 同一個人換手機以前那台
BOARD_OTHER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # 旅伴那台
BOARD_GHOST_ID = "00000000-0000-0000-0000-000000000000"  # 沒登記過：要被標出來
BOARD_SEED = [
    {"id": "seed-mine", "by": BOARD_ME, "text": "我先到飯店了，行李可以先放櫃檯",
     "at": 1763200800000, "dev": BOARD_DEV_NAME, "devId": BOARD_DEV_ID},
    {"id": "seed-other", "by": "周", "text": "收到，我大概 20:30 到\n先去吃一蘭",
     "at": 1763204400000, "dev": "周的 Android 手機", "devId": BOARD_OTHER_ID},
    {"id": "seed-ghost", "by": BOARD_ME, "text": "這一則不是白那台裝置送出的",
     "at": 1763208000000, "dev": "不明的瀏覽器", "devId": BOARD_GHOST_ID},
]
BOARD_OWNERS = {
    # seen 裡有舊的那台：換過手機，以前的留言不該被標成可疑
    BOARD_ME: {"devId": BOARD_DEV_ID, "dev": BOARD_DEV_NAME, "at": 1763200000000,
               "seen": [BOARD_OLD_ID, BOARD_DEV_ID]},
    "周": {"devId": BOARD_OTHER_ID, "dev": "周的 Android 手機", "at": 1763200000000,
           "seen": [BOARD_OTHER_ID]},
}


def seed_board(page) -> None:
    """把留言與裝置登記寫進本機再重新整理（副本已停用 Firebase，不會被遠端蓋掉）。"""
    page.evaluate("""([rows, owners, me, dev]) => {
        const k = 'trip:kyushu-fukuoka-2026';
        const d = JSON.parse(localStorage.getItem(k) || '{}');
        d.board = rows;
        d.owners = owners;
        localStorage.setItem(k, JSON.stringify(d));
        localStorage.setItem(k + ':me', me);
        localStorage.setItem(k + ':device', JSON.stringify(dev));
    }""", [BOARD_SEED, BOARD_OWNERS, BOARD_ME,
           {"id": BOARD_DEV_ID, "name": BOARD_DEV_NAME, "by": BOARD_ME}])
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    got = page.evaluate("JSON.parse(localStorage.getItem('trip:kyushu-fukuoka-2026')||'{}')"
                        ".board?.length ?? 0")
    if got != len(BOARD_SEED):
        sys.exit(f"注入留言失敗（讀回 {got} 則）：UI 探針會量到一個空清單，每條都假通過")


def local_names(path: Path) -> set:
    """真實行程裡「跟中文名稱不一樣」的當地語名稱。卡片上不該出現任何一個。

    這些值一定要留在 TRIP_DATA 裡（地圖查詢用），所以不能靠「資料裡沒有」來驗，
    只能讀畫面（CLAUDE.md 第六節）。順便回傳數量，資料哪天不填了測試才不會變成空的。"""
    html = path.read_text(encoding="utf-8")
    start = html.find("const TRIP_DATA = ") + len("const TRIP_DATA = ")
    depth, end = 0, None
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        sys.exit("讀不到 TRIP_DATA 的範圍")
    out = set()

    def walk(node):
        if isinstance(node, dict):
            for k, base in (("titleLocal", "title"), ("nameLocal", "name"), ("placeLocal", "place")):
                v = node.get(k)
                # 中文名稱本身就含著當地語的（「鰻料理 田舎庵 小倉本店（鰻魚飯）」）不算：
                # 那是標題自己要顯示的字，不是被拿掉的那個當地語標籤
                if v and v not in (node.get(base) or ""):
                    out.add(v)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(html[start:end]).get("days", []))
    return out


def unlock_wallet(page) -> None:
    """記帳有密碼鎖（0000）。鎖本身在第 13 段驗；其他段落要的是鎖後面的東西，
    所以在這裡照使用者的動作解開——解開會記在這台裝置，同一個 context 之後都是開的。
    已經解開（或根本不在記帳頁）時是空動作，所以可以放在「每個分頁都走一遍」的迴圈裡。"""
    if not page.locator("#w-pass").count():
        return
    page.locator("#w-pass").fill("0000")
    page.locator("#w-form button[type=submit]").click()
    page.wait_for_timeout(200)


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
    site_dir = src        # src 在底下被幾個區段當成區域變數重新綁過，要用路徑的話用這個
    if not (src / "index.html").exists():
        sys.exit(f"找不到 {src / 'index.html'}")

    tmp = Path(tempfile.mkdtemp(prefix="tripsite-"))
    try:
        shutil.copytree(src, tmp / "site")
        disable_firebase(tmp / "site" / "index.html")
        # 程式與樣式只有一份在 repo 根目錄的 app/，網站用 ../app/app.js 引用。
        # 各個測試副本（site、swap、settle…）都是 tmp 底下的兄弟資料夾，共用這一份
        shutil.copytree(REPO / "app", tmp / "app")
        guard_shared_code(tmp / "app")
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
                           ("transit", transit_trip()),
                           ("fund", fund_trip()), ("sync", fund_trip("synctest"))]:
            shutil.copytree(tmp / "site", tmp / name)
            swap_trip_data(tmp / name / "index.html", trip)
        # 同步測試用假的 Firebase 設定：模組會被攔掉換成假的。萬一沒攔到，真的 SDK 拿這組
        # 不存在的金鑰也登入不了，碰不到真正的雲端
        sync_html = tmp / "sync" / "index.html"
        sync_html.write_text(sync_html.read_text(encoding="utf-8").replace(
            "const FIREBASE_CONFIG = null;",
            'const FIREBASE_CONFIG = { "apiKey": "fake-key", "projectId": "fake-project" };'), encoding="utf-8")
        if "fake-project" not in sync_html.read_text(encoding="utf-8"):
            sys.exit("準備同步測試失敗：換不了 FIREBASE_CONFIG")

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
            unlock_wallet(page)   # 離線也要解得開：密碼是寫死的，不用連線
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

            # 3b ── 留言板 ───────────────────────────────────
            # 留言是「所有人都看得到」，所以跟記帳同一類：會進同步佇列、刪除給 6 秒復原。
            # 署名來自「這台裝置的登記」：一個名字只認一台裝置，別台選同一個名字會被擋。
            # 仍然是離線狀態——旅途中訊號不好本來就是常態，留言要先留在本機。
            page.get_by_role("tab", name=re.compile("留言板")).click()
            page.wait_for_timeout(500)
            r.check("留言板：分頁在「旅途工具」那一組（跟購物、記帳同一組）",
                    page.locator("#nav-tools #tab-board").count() == 1
                    and page.locator("#nav-prep #tab-board").count() == 0
                    and page.locator("#nav #tab-board").count() == 0)
            r.check("留言板：還沒登記的裝置只能看，沒有留言表單",
                    page.locator("#bd-reg").count() == 1 and page.locator("#bd-add").count() == 0)
            r.check("留言板：裝置名稱有自動帶入偵測到的機種",
                    page.locator("#bd-dev").input_value().strip() != "",
                    f"帶入「{page.locator('#bd-dev').input_value()}」")

            # 什麼都沒選、名字也清掉 → 兩欄都在底下報錯，不跳 alert（全站已經沒有 alert()）
            said.clear()
            page.locator("#bd-dev").fill("")
            page.locator("#bd-reg").click()
            page.wait_for_timeout(300)
            r.check("留言板：登記沒填不跳 alert", not said, "／".join(said))
            r.check("留言板：沒選「我是誰」時錯誤寫在欄位底下",
                    page.locator("#bd-me-err").is_visible()
                    and "你是誰" in page.locator("#bd-me-err").inner_text(),
                    page.locator("#bd-me-err").inner_text())
            r.check("留言板：裝置名稱空白時錯誤也寫在欄位底下",
                    page.locator("#bd-dev-err").is_visible()
                    and "名字" in page.locator("#bd-dev-err").inner_text(),
                    page.locator("#bd-dev-err").inner_text())
            r.check("留言板：沒填的欄位標成 aria-invalid、用 describedby 綁住錯誤",
                    page.locator("#bd-dev").get_attribute("aria-invalid") == "true"
                    and page.locator("#bd-dev").get_attribute("aria-describedby") == "bd-dev-err")
            r.check("留言板：焦點移到第一個沒填的欄位（我是誰）",
                    page.evaluate("document.activeElement.name") == "bdme",
                    page.evaluate("document.activeElement.id || document.activeElement.name"))

            # 登記這台裝置（chip 裡的 radio 是藏起來的，要點看得見的 label）
            page.locator('#bd-me .chip:has(input[value="白"])').click()
            page.locator("#bd-dev").fill("白的測試機")
            page.locator("#bd-reg").click()
            page.wait_for_timeout(500)
            r.check("留言板：登記成功有提示", "已登記" in page.locator("#toast").inner_text(),
                    page.locator("#toast").inner_text())
            r.check("留言板：登記後卡片上寫著是誰的哪一台",
                    "白" in page.locator(".bd-reg").inner_text()
                    and "白的測試機" in page.locator(".bd-reg").inner_text(),
                    page.locator(".bd-reg").inner_text())
            r.check("留言板：登記之後才出現留言表單", page.locator("#bd-add").count() == 1)
            r.check("留言板：記帳頁的「我是誰」跟著登記走",
                    page.evaluate("localStorage.getItem('trip:kyushu-fukuoka-2026:me')") == "白")
            queued = page.evaluate(
                "JSON.parse(localStorage.getItem('trip:kyushu-fukuoka-2026') || '{}')"
                ".queue?.map(q => q.type) ?? []")
            r.check("留言板：登記會上傳（別台才知道這個名字被佔住了）", "owners" in queued,
                    f"佇列內容 {queued}")

            # 送出：出現在清單上，帶著留言者與時間
            page.locator("#bd-text").fill("離線測試留言")
            page.locator("#bd-add").click()
            page.wait_for_timeout(500)
            r.check("留言板：送出成功有提示", "已送出留言" in page.locator("#toast").inner_text(),
                    page.locator("#toast").inner_text())
            # 一律讀 #main 的 innerText，不要用 page.content()（CLAUDE.md 第六節）
            r.check("留言板：送出的留言出現在清單上",
                    "離線測試留言" in page.locator("#main").inner_text())
            mine = page.locator(".bd-item", has_text="離線測試留言")
            r.check("留言板：留言帶著留言者", "白" in mine.locator(".bd-who").inner_text(),
                    mine.locator(".bd-who").inner_text())
            r.check("留言板：留言帶著時間（目的地時區）",
                    re.search(r"\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}", mine.locator(".bd-at").inner_text())
                    is not None, mine.locator(".bd-at").inner_text())
            saved_msg = page.evaluate(
                "JSON.parse(localStorage.getItem('trip:kyushu-fukuoka-2026')||'{}')"
                ".board.find(m => m.text === '離線測試留言')")
            r.check("留言板：留言記下了是從哪一台裝置送出的",
                    saved_msg.get("dev") == "白的測試機" and bool(saved_msg.get("devId")),
                    f"dev={saved_msg.get('dev')} devId={str(saved_msg.get('devId'))[:8]}…")
            r.check("留言板：平常不在留言上顯示裝置（只有異常時才標）",
                    "白的測試機" not in mine.inner_text(), mine.inner_text())
            r.check("留言板：送出後輸入框清空", page.locator("#bd-text").input_value() == "")
            r.check("留言板：沒有 JS 例外", not errors, "; ".join(errors[:2]))

            # 留言跟記帳一樣會上傳（打包清單才是只存本機，見 2b），走的是同一條
            # 「整份文件覆寫」的路。代價寫在 CLAUDE.md 第五節，使用者已經接受。
            queued = page.evaluate(
                "JSON.parse(localStorage.getItem('trip:kyushu-fukuoka-2026') || '{}')"
                ".queue?.map(q => q.type) ?? []")
            r.check("留言板：留言會進同步佇列（跟記帳同一類）", "board" in queued,
                    f"佇列內容 {queued}")
            # 那個代價要寫在畫面上給使用者看，不能只活在 CLAUDE.md 裡
            r.check("留言板：表單底下寫明離線留言可能被覆蓋",
                    "蓋掉對方" in page.locator("#main").inner_text())

            page.reload()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1200)
            page.get_by_role("tab", name=re.compile("留言板")).click()
            page.wait_for_timeout(500)
            r.check("留言板：重新整理後留言還在",
                    "離線測試留言" in page.locator("#main").inner_text())

            # 誤刪可以復原：留言是同步的，誤觸 ✕ 會讓所有人的裝置上都少一則
            page.locator(".bd-item", has_text="離線測試留言").locator(".bd-del").click()
            page.wait_for_timeout(400)
            r.check("留言板：刪除後留言消失",
                    page.locator(".bd-item", has_text="離線測試留言").count() == 0)
            r.check("留言板：刪除的提示有「復原」可以按",
                    page.locator("#toast .toast-act").is_visible()
                    and "復原" in page.locator("#toast .toast-act").inner_text())
            page.locator("#toast .toast-act").click()
            page.wait_for_timeout(400)
            r.check("留言板：按「復原」留言回來",
                    page.locator(".bd-item", has_text="離線測試留言").count() == 1)

            # 打到一半的留言不能被重畫弄掉（別人送留言時 onSnapshot 會整頁重畫）
            page.locator("#bd-text").fill("打到一半的留言")
            page.wait_for_timeout(200)
            page.get_by_role("tab", name=re.compile("記帳")).click()
            page.wait_for_timeout(300)
            page.get_by_role("tab", name=re.compile("留言板")).click()
            page.wait_for_timeout(300)
            r.check("留言板：重畫之後打到一半的留言還在",
                    page.locator("#bd-text").input_value() == "打到一半的留言",
                    page.locator("#bd-text").input_value())

            # 3d ── 有新留言的提示 ─────────────────────────────
            # 副本停用了 Firebase，收不到真的推播，所以用「注入＋重新整理」模擬收到別人的留言
            def open_other_tab():
                page.reload()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1200)
                page.locator(".nav-item").first.click()   # 停在別的分頁，徽章才有意義
                page.wait_for_timeout(300)

            page.evaluate("""() => {
                const k = 'trip:kyushu-fukuoka-2026';
                const d = JSON.parse(localStorage.getItem(k) || '{}');
                const now = Date.now();
                d.board = [...(d.board || []),
                  { id:'new-1', by:'周', text:'我到車站了',   at: now,
                    dev:'周的手機', devId:'zhou-dev' },
                  { id:'new-2', by:'陳', text:'我大概晚十分鐘', at: now + 1000,
                    dev:'陳的手機', devId:'chen-dev' }];
                localStorage.setItem(k, JSON.stringify(d));
            }""")
            open_other_tab()
            badge = page.locator("#tab-board .bd-badge")
            r.check("新留言提示：分頁上出現未讀數",
                    badge.is_visible() and badge.inner_text().strip() == "2",
                    f"看到「{badge.inner_text()}」" if badge.count() else "沒有徽章")
            r.check("新留言提示：讀螢幕唸得出幾則",
                    "2 則新留言" in (page.locator("#tab-board").get_attribute("aria-label") or ""),
                    page.locator("#tab-board").get_attribute("aria-label"))
            # 徽章本身要對輔助技術隱藏，不然分頁名稱會被唸成「留言板 2」
            r.check("新留言提示：徽章不重複唸一次數字",
                    badge.get_attribute("aria-hidden") == "true")
            page.get_by_role("tab", name=re.compile("留言板")).click()
            page.wait_for_timeout(400)
            r.check("新留言提示：進去看過就不見了", not badge.is_visible())
            r.check("新留言提示：看過之後分頁名稱也恢復原樣",
                    not page.locator("#tab-board").get_attribute("aria-label"),
                    page.locator("#tab-board").get_attribute("aria-label"))
            open_other_tab()
            r.check("新留言提示：看過之後重新整理不會又冒出來", not badge.is_visible())

            # 自己送的不算未讀，不然每打一則自己的分頁上就多一個紅點
            page.evaluate("""() => {
                const k = 'trip:kyushu-fukuoka-2026';
                const d = JSON.parse(localStorage.getItem(k) || '{}');
                const dev = JSON.parse(localStorage.getItem(k + ':device'));
                d.board = [...(d.board || []), { id:'mine-later', by: dev.by,
                  text:'我自己後來補的', at: Date.now() + 60000, dev: dev.name, devId: dev.id }];
                localStorage.setItem(k, JSON.stringify(d));
            }""")
            open_other_tab()
            r.check("新留言提示：自己送的不算未讀", not badge.is_visible(),
                    badge.inner_text() if badge.is_visible() else "")

            # 3c ── 一個名字只認一台裝置 ──────────────────────
            # 換一個裝置編號＝模擬「另一台裝置」，名字沒變。
            # 使用者要的是前端直接擋下來，但換手機的人不能因此被鎖在外面，
            # 所以擋的同時要給一條「改用這台」的明路。
            page.evaluate("""() => {
                const k = 'trip:kyushu-fukuoka-2026:device';
                const d = JSON.parse(localStorage.getItem(k));
                localStorage.setItem(k, JSON.stringify({ ...d, id: 'another-device-0001' }));
            }""")
            page.reload()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1200)
            page.get_by_role("tab", name=re.compile("留言板")).click()
            page.wait_for_timeout(400)
            r.check("裝置登記：名字登記在別台時，這台不能留言",
                    page.locator("#bd-add").count() == 0)
            r.check("裝置登記：卡片上說清楚名字在哪一台",
                    page.locator("#main .card > .note-box.warn").count() == 1
                    and "不能用這個名字留言" in page.locator("#main .note-box.warn").inner_text(),
                    page.locator("#main .note-box.warn").inner_text()[:40])
            page.locator("#bd-redo").click()
            page.wait_for_timeout(300)
            page.locator('#bd-me .chip:has(input[value="白"])').click()
            page.locator("#bd-reg").click()
            page.wait_for_timeout(400)
            r.check("裝置登記：別台選同一個名字會被擋下來",
                    page.locator("#bd-me-err").is_visible()
                    and "已經登記在" in page.locator("#bd-me-err").inner_text()
                    and page.locator("#bd-add").count() == 0,
                    page.locator("#bd-me-err").inner_text())
            r.check("裝置登記：擋下來的同時給一條「改用這台」",
                    page.locator("#bd-take").is_visible())
            page.locator("#bd-take").click()
            page.wait_for_timeout(500)
            r.check("裝置登記：按「改用這台」之後就能留言了",
                    page.locator("#bd-add").count() == 1)
            # 換手機以前送的留言不該整批變成「可疑」——比的是歷來登記過的裝置（seen）
            r.check("裝置登記：接手之後，自己以前的留言不會被誤標",
                    page.locator(".bd-item.is-odd").count() == 0,
                    f"{page.locator('.bd-item.is-odd').count()} 則被標成可疑")

            # 前端擋不住直接寫雲端的人，所以沒登記過的裝置送出的留言要看得出來
            page.evaluate("""() => {
                const k = 'trip:kyushu-fukuoka-2026';
                const d = JSON.parse(localStorage.getItem(k) || '{}');
                d.board = [...(d.board || []), { id: 'forged-1', by: '白', text: '這則是別人冒名寫的',
                    at: Date.now(), dev: '不明的瀏覽器', devId: 'never-registered-9999' }];
                localStorage.setItem(k, JSON.stringify(d));
            }""")
            page.reload()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1200)
            page.get_by_role("tab", name=re.compile("留言板")).click()
            page.wait_for_timeout(400)
            odd = page.locator(".bd-item.is-odd")
            r.check("裝置登記：沒登記過的裝置送出的留言會被標出來",
                    odd.count() == 1 and "不是「白」登記的裝置" in odd.inner_text(),
                    odd.inner_text().replace("\n", " ") if odd.count() else "沒有標出來")
            r.check("裝置登記：標出來的那則不給 ✕（不是這台送的）",
                    odd.locator(".bd-del").count() == 0)
            r.check("裝置登記：自己的留言照樣有 ✕",
                    page.locator(".bd-item", has_text="離線測試留言").locator(".bd-del").count() == 1)
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
            # 卡片上不顯示當地語名稱（使用者的決定：中文名稱就夠了）。資料裡仍然留著，
            # 地圖按鈕與電腦版當日路線靠「當地語名稱, 地點」去查，拿掉會查不到店。
            # 五個會顯示當地語的地方各出一個：景點標題與地點、交通卡站名、
            # 住宿標題、二選一的地點（二選一的店名在第 13 段用真實行程驗）
            hidden_local = [x for x in ["Central Station", "Sydney Opera House", "Bennelong Pt.",
                                        "Darling Hbr.", "Test Hotel"] if x in shown]
            r.check("卡片：不顯示當地語名稱", not hidden_local, "還看得到：" + "、".join(hidden_local))
            r.check("交通卡：站名只出現一次", ride_text.count("Circular Quay") == 1,
                    f"出現 {ride_text.count('Circular Quay')} 次")
            r.check("卡片：當地語仍留在資料裡（地圖查詢要用）",
                    page.locator('#main .card[data-mapq]').first.get_attribute("data-mapq")
                    == "Sydney Opera House, Bennelong Point",
                    page.locator('#main .card[data-mapq]').first.get_attribute("data-mapq") or "")
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
            # 交通放在兩張卡片中間（這張卡片的正上方），不在卡片裡
            oleg = opera.locator("xpath=preceding-sibling::*[1][contains(@class,'leg')]")
            r.check("交通：在兩張卡片中間顯示步行與分鐘數",
                    oleg.count() == 1 and "步行 20 分" in oleg.inner_text() and "步行 20 分" not in otext,
                    oleg.inner_text() if oleg.count() else "卡片上方沒有交通")
            # 停留(分) 含交通，卡片要扣掉——不扣的話門司港會顯示停留 3 小時 50 分
            r.check("交通：停留扣掉交通（90 − 20）", "停留 1 小時 10 分" in otext)
            r.check("交通：沒有把總時間當成停留", "停留 1 小時 30 分" not in otext)
            # 左上角是「抵達」時間：資料的 time 是從上一站出發（11:30），加上步行 20 分
            r.check("時間：卡片左上角是抵達時間（出發 11:30＋步行 20 分）",
                    opera.locator(".time").inner_text() == "11:50", opera.locator(".time").inner_text())
            # 預計離開＝ time + stay。stay 含交通，所以它同時也是下一張卡片的出發時刻。
            # 這裡用控制得住的置換資料驗確切數字：11:30 ＋ 90 分 = 13:00，而 13:00 正好是
            # 下面那張交通卡的出發時間——兩者對不起來就表示有一邊漏加或多加了交通
            oleave = opera.locator(".stay.leave")
            r.check("時間：卡片上有預計離開時間（出發 11:30＋總共 90 分）",
                    oleave.count() == 1 and oleave.inner_text() == "離開 13:00",
                    oleave.inner_text() if oleave.count() else "沒有標籤")
            r.check("時間：離開時刻就是下一張卡片的出發時刻",
                    oleave.count() == 1 and oleave.inner_text().endswith(ride.locator(".time").inner_text()),
                    f"離開 {oleave.inner_text() if oleave.count() else '—'}／下一張出發 {ride.locator('.time').inner_text()}")
            # 地鐵卡是移動本身，停下來的那一刻叫「抵達」不叫「離開」（13:00 ＋ 25 分）
            rleave = ride.locator(".stay.leave")
            r.check("交通卡：到站時刻寫「抵達」不寫「離開」",
                    rleave.count() == 1 and rleave.inner_text() == "抵達 13:25",
                    rleave.inner_text() if rleave.count() else "沒有標籤")
            # 沒填停留就算不出離開時間——不要留一個空白或 NaN 的標籤
            dinner = page.locator("#main .card", has_text="海鮮市場")
            r.check("時間：沒填停留的卡片不出現離開時間",
                    dinner.locator(".time").count() == 1 and dinner.locator(".stay.leave").count() == 0,
                    f"{dinner.locator('.stay.leave').count()} 個標籤")
            # 跨過午夜要標「隔天」，不然 23:30 ＋ 90 分會印成「離開 01:00」，看起來像倒退
            late = page.locator("#main .card", has_text="深夜巴士").locator(".stay.leave")
            r.check("時間：跨過午夜的時刻標「隔天」",
                    late.count() == 1 and late.inner_text() == "抵達 隔天 01:00",
                    late.inner_text() if late.count() else "沒有標籤")
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
            # 二選一：只有一顆按鈕就放在右邊（手機寬度也是），兩顆以上維持在下面
            CHOICE_GEO = """c => { const t = c.querySelector('.choice-main').getBoundingClientRect();
                const bs = [...c.querySelectorAll('.btn')].map(b => b.getBoundingClientRect());
                return { right: bs.every(b => b.left >= t.right), below: bs.every(b => b.top >= t.bottom - 1),
                         textW: Math.round(t.width), btnH: Math.round(Math.min(...bs.map(b => b.height))) }; }"""
            for vw in (1280, 375):
                cctx = browser.new_context(viewport={"width": vw, "height": 900})
                cp = cctx.new_page()
                cp.goto(swap); cp.wait_for_load_state("networkidle"); cp.wait_for_timeout(400)
                one = cp.locator(".choice", has_text="海鮮市場").evaluate(CHOICE_GEO)
                two = cp.locator(".choice", has_text="牛排館").evaluate(CHOICE_GEO)
                r.check(f"二選一（寬 {vw}）：只有一顆按鈕放右邊、兩顆以上放下面",
                        one["right"] and not one["below"] and two["below"] and one["btnH"] >= 44,
                        f"一顆 {one}／兩顆 {two}")
                # 選項卡自己的按鈕：標題列右上角，不在任何一個選項裡面；寬度不夠就整顆掉到標題下面，
                # 不能把標題擠斷（使用者明確不要）、也不能超出卡片
                head = cp.locator(".card", has_text="海鮮市場").evaluate("""c => {
                    const h = c.querySelector('.opt-head'); if (!h) return null;
                    const main = h.querySelector('.opt-head-main'), m = main.getBoundingClientRect();
                    const b = h.querySelector('.opt-links .btn').getBoundingClientRect(), cr = c.getBoundingClientRect();
                    return { right: b.left >= m.right - 1, inside: b.right <= cr.right + .5 && b.left >= cr.left,
                             btnH: Math.round(b.height), clipped: main.scrollWidth > main.clientWidth + 1,
                             inChoice: !!h.closest('.choice'), title: !!main.querySelector('h2.subject') }; }""")
                r.check(f"選項卡的按鈕（寬 {vw}）：在標題列、不在選項裡、不超出卡片、標題沒被擠斷、≥44px",
                        bool(head) and head["inside"] and not head["inChoice"] and not head["clipped"]
                        and head["title"] and head["btnH"] >= 44, f"{head}")
                if vw == 1280:
                    r.check("選項卡的按鈕（電腦寬）：在標題的右邊（右上角）", bool(head) and head["right"], f"{head}")
                cctx.close()
            hleg = hotel.locator("xpath=preceding-sibling::*[1][contains(@class,'leg')]")
            r.check("住宿：交通也在卡片上方", hleg.count() == 1 and "車程 15 分" in hleg.inner_text())
            r.check("住宿：交通等於總時間時不顯示停留", "停留" not in htext)
            # 整列都在路上（純停留 0）：抵達就等於離開，同一排印兩個一樣的時刻只會更亂
            r.check("住宿：沒有停留就不留一個空的離開時間",
                    hotel.locator(".stay.leave").count() == 0,
                    f"{hotel.locator('.stay.leave').count()} 個標籤")
            r.check("住宿：營業時間（入住退房）獨立一列", "Check-in 15:00／退房 10:00" in htext)

            # 信用卡回饋額度（在第二天）
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            # 留言板也在第二天。留言是空的，但渲染過就會有這句——
            # 漏掉 renderer 的話 renderBlock 會改畫錯誤卡片，這條就會失敗
            r.check("置換 TRIP_DATA：board 留言板 有渲染",
                    "還沒有留言" in page.locator("#main").inner_text()
                    and page.locator("#main .bd-list").count() == 1)
            r.check("信用卡：三張都渲染", page.locator(".cc").count() == 3,
                    f"{page.locator('.cc').count()} 張")
            r.check("信用卡：沒有額度的不畫進度條",
                    page.locator('.cc[data-card="無上限卡"] .cc-bar').count() == 0)
            r.check("信用卡：過期的有標示",
                    page.locator('.cc[data-card="過期卡"] .cc-expired').count() == 1)
            r.check("信用卡：未過期的不標",
                    page.locator('.cc[data-card="有額度卡"] .cc-expired').count() == 0)

            # 信用卡：累計（使用者要求——旅程當下一筆一筆刷的，不會每次都湊整數，
            # 「加入」只把這一筆累加進已計入的總額；要大幅訂正才用「修改總額」直接覆寫。
            # 回饋%要比以前顯眼（使用者要求字放大）。
            quota_card = page.locator('.cc[data-card="有額度卡"]')
            expired_card = page.locator('.cc[data-card="過期卡"]')   # 也有額度(1000)，借來測「不會互相干擾」
            add_in = quota_card.locator(".cc-add-input")
            add_btn = quota_card.locator(".cc-add-btn")

            r.check("信用卡：回饋%字比卡名還大（使用者要求放大）",
                    float(quota_card.locator(".cc-rate").evaluate("e => getComputedStyle(e).fontSize"
                                                                   ".replace('px','')"))
                    > float(quota_card.locator(".cc-name").evaluate("e => getComputedStyle(e).fontSize"
                                                                     ".replace('px','')")))

            add_in.click(); add_in.type("2000", delay=30)
            add_btn.click()
            page.wait_for_timeout(200)
            r.check("信用卡：加入第一筆後已計入正確（0 + 2000）",
                    "2,000" in quota_card.locator(".cc-used").inner_text()
                    and "3,000" in quota_card.locator(".cc-left").inner_text(),
                    quota_card.locator(".cc-used").inner_text())
            r.check("信用卡：進度條寬度正確（2000／5000）",
                    quota_card.locator(".cc-bar i").evaluate("e => e.style.width") == "40%",
                    quota_card.locator(".cc-bar i").evaluate("e => e.style.width"))
            r.check("信用卡：加入後輸入框清空、游標留在原地（方便連打下一筆）",
                    add_in.input_value() == ""
                    and page.evaluate("document.activeElement.classList.contains('cc-add-input')"))

            # 累計：第二筆要「加上去」，不是覆寫成新值——這是這次改動的重點
            add_in.fill("500")
            add_in.press("Enter")   # 也測 Enter 鍵能送出，不是只有按鈕
            page.wait_for_timeout(200)
            r.check("信用卡：第二筆是累加（2000 + 500 = 2500），不是覆寫",
                    "2,500" in quota_card.locator(".cc-used").inner_text(),
                    quota_card.locator(".cc-used").inner_text())

            # 誤刪可以復原：加入也一樣——金額打錯很容易發生
            r.check("信用卡：加入後有提示、可以復原",
                    page.locator("#toast .toast-act").is_visible()
                    and "復原" in page.locator("#toast .toast-act").inner_text()
                    and "500" in page.locator("#toast").inner_text(),
                    page.locator("#toast").inner_text())
            page.locator("#toast .toast-act").click()
            page.wait_for_timeout(200)
            r.check("信用卡：按「復原」只退回上一筆（回到 2000，不是歸零）",
                    "2,000" in quota_card.locator(".cc-used").inner_text(),
                    quota_card.locator(".cc-used").inner_text())

            add_btn.click()   # 輸入框這時是空的
            page.wait_for_timeout(150)
            r.check("信用卡：空白按「加入」不會動（不會加進 0）",
                    "2,000" in quota_card.locator(".cc-used").inner_text(),
                    quota_card.locator(".cc-used").inner_text())

            # 不整頁重畫：在別張卡片打到一半的金額，加這張的時候不能被洗掉
            expired_in = expired_card.locator(".cc-add-input")
            expired_in.click(); expired_in.type("999", delay=30)
            add_in.click(); add_in.fill("300"); add_btn.click()
            page.wait_for_timeout(200)
            r.check("信用卡：加入一張卡不會洗掉別張卡還沒送出的輸入",
                    expired_in.input_value() == "999", expired_in.input_value())
            r.check("信用卡：累加到 2300", "2,300" in quota_card.locator(".cc-used").inner_text(),
                    quota_card.locator(".cc-used").inner_text())

            # 修改總額：少數要大幅度訂正才用，平常用「加入」就好
            r.check("信用卡：一開始沒有「修改總額」的輸入框", quota_card.locator(".cc-fix-input").count() == 0)
            quota_card.locator("[data-cc-fix]").click()
            page.wait_for_timeout(150)
            fix_in = quota_card.locator(".cc-fix-input")
            r.check("信用卡：「修改總額」展開，預填目前的總額",
                    fix_in.is_visible() and fix_in.input_value() == "2300", fix_in.input_value())
            fix_in.fill("1200")
            quota_card.locator(".cc-fix-cancel").click()
            page.wait_for_timeout(150)
            r.check("信用卡：取消不會改到總額", "2,300" in quota_card.locator(".cc-used").inner_text()
                    and quota_card.locator(".cc-fix-input").count() == 0,
                    quota_card.locator(".cc-used").inner_text())

            quota_card.locator("[data-cc-fix]").click()
            page.wait_for_timeout(150)
            quota_card.locator(".cc-fix-input").fill("1200")
            quota_card.locator(".cc-fix-ok").click()
            page.wait_for_timeout(150)
            r.check("信用卡：修改總額是覆寫，不是累加（變成 1200，不是 2300+1200）",
                    "1,200" in quota_card.locator(".cc-used").inner_text()
                    and "3,800" in quota_card.locator(".cc-left").inner_text()
                    and quota_card.locator(".cc-fix-input").count() == 0,
                    quota_card.locator(".cc-used").inner_text())

            r.check("信用卡：已刷金額不進同步佇列（只存本機）",
                    "cards" not in page.evaluate(
                        "(JSON.parse(localStorage.getItem('trip:swaptest-2027')||'{}').queue||[])"
                        ".map(q => q.type).join(',')"))

            # 排序：三條線的把手可以直接拖；鍵盤走上下鍵（WCAG 2.5.7：拖曳一定要有不用拖的替代方式）。
            # 順序只存本機——誰先刷哪張是個人的決定，A 手機排過不該動到 B 手機
            names = "() => [...document.querySelectorAll('#main .cc')].map(e => e.dataset.card)"
            store = ("() => (JSON.parse(localStorage.getItem('trip:swaptest-2027')||'{}')"
                     ".cardOrder||[]).join(',')")
            midof = """(i) => { const cs = [...document.querySelectorAll('#main .cc')];
                        const b = cs[i < 0 ? cs.length + i : i].getBoundingClientRect();
                        return b.top + b.height / 2; }"""

            def drag_card(name, to_y=None, over=None):
                """把 name 那張的把手拖到畫面的 to_y；over 是「拖到第幾張的中線上面」。
                   先把它捲進畫面——在視窗外的話抓不到把手，拖了也不會動。"""
                page.evaluate("""(n) => [...document.querySelectorAll('#main .cc')]
                          .find(e => e.dataset.card === n).scrollIntoView({ block: "center" })""", name)
                page.wait_for_timeout(150)
                if over is not None:
                    to_y = page.evaluate(midof, over) - 12
                g = page.evaluate("""(n) => { const cc = [...document.querySelectorAll('#main .cc')]
                            .find(e => e.dataset.card === n);
                          const r = cc.querySelector('[data-grip]').getBoundingClientRect();
                          return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }""", name)
                page.mouse.move(g["x"], g["y"])
                page.mouse.down()
                page.mouse.move(g["x"], to_y, steps=16)
                page.wait_for_timeout(150)
                n = page.locator("#main .cc.is-dragging").count()
                page.mouse.up()
                page.wait_for_timeout(300)
                return n

            before = page.evaluate(names)
            r.check("信用卡：每張卡片都有排序把手",
                    page.locator("#main [data-grip]").count() == len(before),
                    f"{page.locator('#main [data-grip]').count()}／{len(before)} 張")
            # 一路拖到最後面。刻意跨過好幾張：只拖一格的話，拖曳中途被中斷也看不出來
            # （設過 setPointerCapture 的元素一搬動就會丟 lostpointercapture，踩過一次）
            lifted = drag_card(before[0], page.evaluate(midof, -1) + 12)
            after = page.evaluate(names)
            r.check("信用卡：把手可以一路拖到最後面", after == before[1:] + [before[0]],
                    f"{before} → {after}")
            r.check("信用卡：拖的時候那張會浮起來", lifted == 1, f"{lifted} 張 is-dragging")
            r.check("信用卡：排序不進同步佇列（只存本機）",
                    "cards" not in page.evaluate(
                        "(JSON.parse(localStorage.getItem('trip:swaptest-2027')||'{}').queue||[])"
                        ".map(q => q.type).join(',')"))
            r.check("信用卡：排序存進本機", page.evaluate(store) == ",".join(after),
                    page.evaluate(store))
            page.reload()
            page.wait_for_load_state("networkidle")
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            r.check("信用卡：重新整理後順序還在", page.evaluate(names) == after,
                    f"{page.evaluate(names)}")
            # 也要能往上拖（拖到第一張的中線上面）
            drag_card(after[1], over=0)
            up = page.evaluate(names)
            r.check("信用卡：把手也能往上拖", up == [after[1], after[0], after[2]], f"{after} → {up}")
            # 不用拖也要能排：把手聚焦後按上鍵
            page.evaluate("""(n) => [...document.querySelectorAll('#main .cc')]
                      .find(e => e.dataset.card === n).querySelector('[data-grip]').focus()""", after[0])
            page.keyboard.press("ArrowUp")
            page.wait_for_timeout(300)
            r.check("信用卡：上下鍵也能排（拖曳的替代方式）", page.evaluate(names) == after,
                    f"{page.evaluate(names)}")
            # 重畫整頁會讓焦點掉回 body，連按好幾下就得重新找把手
            focus = page.evaluate("() => ({ grip: document.activeElement.hasAttribute('data-grip'),"
                                  " card: document.activeElement.dataset.card || '' })")
            r.check("信用卡：移完焦點留在把手（可以連按）",
                    focus["grip"] and focus["card"] == after[0], str(focus))

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
            r.check("購物：商品的群組有實付金額與價格標籤",
                    page.locator('.shop-g[data-group="藥妝店"] .shop-amt').count() == 1
                    and page.locator('.shop-g[data-group="藥妝店"] .sr-tag').count() == 1)
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

            # 新增群組（自己的分類）
            page.locator('.shop-addg [name="g"]').fill("超市")
            page.locator('.shop-addg button[type="submit"]').click()
            page.wait_for_timeout(400)

            # 新增項目：最上面只有一個新增欄位（使用者要「一直新增、不用自己找分類」）
            form = page.locator("#shop-new")
            r.check("購物：新增欄位在最上面（第一個群組之前）",
                    page.evaluate("""() => { const f = document.getElementById('shop-new'),
                                       g = document.querySelector('#main .shop-g');
                                     return !!f && !!g && !!(f.compareDocumentPosition(g) & Node.DOCUMENT_POSITION_FOLLOWING); }"""))
            r.check("購物：群組底下不再各有一個新增欄位", page.locator(".shop-add").count() == 0)
            form.locator("#sn-n").fill("自己加的東西")
            form.locator("#sn-g").select_option("藥妝店")
            form.locator('[name="w"]').fill("同事")
            form.locator('button[type="submit"]').click()
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
            # 連結只收 http／https。這兩個收在「附圖片或連結」裡
            PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
            idb_keys = lambda: page.evaluate("""() => new Promise(ok => {
                const r = indexedDB.open('trip:swaptest-2027:img');
                r.onsuccess = () => { const db = r.result;
                  if (!db.objectStoreNames.contains('img')){ db.close(); return ok([]); }
                  const c = db.transaction('img').objectStore('img').getAllKeys();
                  c.onsuccess = () => { ok(c.result); db.close(); }; };
                r.onerror = () => ok(null); })""")
            idb_count = lambda: len(idb_keys() or [])
            form = page.locator("#shop-new")
            form.locator("#sn-n").fill("附圖的東西")
            form.locator("#sn-g").select_option("藥妝店")
            form.locator(".sn-more summary").click()
            form.locator('[name="u"]').fill("www.example.com/coupon")
            form.locator('[name="f"]').set_input_files({"name": "c.png", "mimeType": "image/png", "buffer": PNG})
            r.check("附圖片：選了圖，按鈕變成「已選圖片」", "已選圖片" in form.locator(".shop-file").inner_text())
            form.locator('button[type="submit"]').click()
            page.wait_for_timeout(800)
            mine = page.locator('.shop-g[data-group="藥妝店"] .shop-i', has_text="附圖的東西")
            src = lambda: mine.locator("img.shop-img").get_attribute("src") or ""
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
            # 不合格的連結擋下來：javascript: 點了會在網站裡執行程式。錯誤寫在欄位底下；
            # 同時確認沒有任何對話框跳出來——這裡填的就是 javascript:alert(1)，有對話框就代表網址被執行了
            dialogs = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            form.locator("#sn-n").fill("壞連結")
            form.locator(".sn-more summary").click()
            form.locator('[name="u"]').fill("javascript:alert(1)")
            form.locator('button[type="submit"]').click()
            page.wait_for_timeout(500)
            said = page.locator("#sn-u-err").inner_text() if page.locator("#sn-u-err").is_visible() else ""
            r.check("附連結：javascript: 這種網址擋下來、不新增、錯誤寫在欄位底下",
                    page.locator(".shop-i", has_text="壞連結").count() == 0 and "網址" in said and not dialogs
                    and page.locator("#sn-u").get_attribute("aria-invalid") == "true",
                    said + ("／對話框：" + "、".join(dialogs) if dialogs else ""))
            # 優惠券也從同一個欄位加：分類選「優惠券」，內容／數量／幫誰買用不到，先收起來
            form.locator("#sn-n").fill("自己加的券")
            form.locator("#sn-g").select_option("優惠券")
            r.check("優惠券：選了優惠券，內容／數量／幫誰買收起來", form.locator(".sn-goods").is_hidden())
            form.locator('[name="u"]').fill("https://example.com/my-coupon")
            form.locator('button[type="submit"]').click()
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

            # 已經在清單裡的項目（這裡用 Excel 給的那一項）之後再補圖、換圖、移除圖。
            # 「加商品圖片」收在鉛筆打開的編輯欄裡
            seed = page.locator('.shop-i[data-id="藥妝店:托買的東西"]')
            seed.locator("[data-sedit]").click()
            r.check("補圖：沒有圖的項目，編輯欄裡有「加商品圖片」", seed.locator(".shop-addimg").count() == 1)
            seed.locator("[data-simg]").set_input_files({"name": "c.png", "mimeType": "image/png", "buffer": PNG})
            try: page.wait_for_function(blob_ready, timeout=5000)
            except Exception: pass
            seed_src = lambda: seed.locator("img.shop-img").get_attribute("src") or "" if seed.locator("img.shop-img").count() else ""
            r.check("補圖：選了圖就出現在列上、「加商品圖片」收起來",
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
            # 換圖是非同步的：等列上出現「新的」圖片網址
            try: page.wait_for_function("""o => { const i = document.querySelector('.shop-i[data-id="藥妝店:托買的東西"] img.shop-img');
                                                  return !!i && i.src.startsWith('blob:') && i.src !== o; }""", arg=before, timeout=5000)
            except Exception: pass
            r.check("補圖：換一張後放大視窗關掉、列上還是有圖",
                    page.locator("#zoom[open]").count() == 0 and seed_src().startswith("blob:") and idb_count() == 1)
            seed.locator("img.shop-img").click()
            page.wait_for_timeout(300)
            acts.locator("[data-zdel]").click()
            page.wait_for_timeout(500)
            seed.locator("[data-sedit]").click()
            r.check("補圖：移除圖片後回到「加商品圖片」、IndexedDB 也清掉",
                    seed.locator("img.shop-img").count() == 0 and seed.locator(".shop-addimg").count() == 1 and idb_count() == 0,
                    f"還剩 {idb_count()} 張")
            seed.locator("[data-scancel]").click()
            r.check("編輯：取消會關掉編輯欄、焦點回到鉛筆",
                    seed.locator(".sr-editbox").count() == 0
                    and page.evaluate("document.activeElement.dataset.sedit || ''") == "藥妝店:托買的東西")
            page.locator('.shop-img[src*="img/"]').first.click()
            page.wait_for_timeout(300)
            r.check("補圖：Excel 給的圖放大後不能換、不能移除",
                    page.locator("#zoom .zoom-acts").inner_html().strip() == "")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

            # 自動分類：打名稱就先選好。比到最長的關鍵字（「杯麵」不是杯子、「衣架」不是衣服），
            # 清單裡已經有同名的就跟著它現在的分類
            wrong = {}
            for word, want in [("面膜", "藥妝店"), ("吹風機", "電器 3C"), ("博多通りもん", "伴手禮"),
                               ("UNIQLO 發熱衣", "衣服鞋包"), ("杯麵", "零食食品"), ("衣架", "生活用品"),
                               ("電動牙刷", "電器 3C"), ("Donki 優惠券", "優惠券"), ("托買的東西", "藥妝店"),
                               # 這幾個是上線後在手機上實際打進去才發現會掉到「其他」的
                               ("面速力達母", "藥妝店"), ("龍角散", "藥妝店"), ("口罩", "藥妝店"),
                               ("牙刷", "藥妝店"), ("溫泉粉", "藥妝店"), ("空氣清淨機", "電器 3C"),
                               ("指甲剪", "生活用品"), ("銅鑼燒", "零食食品"),
                               # 單字詞會帶錯組，所以關鍵字表裡刻意不放「鍋」「書」
                               ("火鍋料", "零食食品"), ("電鍋", "電器 3C"),
                               ("完全猜不到的東西", "其他")]:
                form.locator("#sn-n").fill(word)
                g = form.locator("#sn-g").input_value()
                if g != want:
                    wrong[word] = g
            r.check("自動分類：打名稱就選好分類", not wrong, str(wrong))
            form.locator("#sn-n").fill("面")
            form.locator("#sn-g").select_option("伴手禮")
            form.locator("#sn-n").fill("面膜")
            r.check("自動分類：自己改過分類，再打字不會被蓋掉", form.locator("#sn-g").input_value() == "伴手禮",
                    form.locator("#sn-g").input_value())
            # 連續新增最快的路：打名稱直接按 Enter（手機鍵盤上那顆要是「完成」不是換行）
            form.locator("#sn-g").select_option("藥妝店")
            form.locator("#sn-n").fill("按 Enter 加的")
            form.locator("#sn-n").press("Enter")
            page.wait_for_timeout(500)
            r.check("購物：名稱欄按 Enter 就新增，游標留在名稱",
                    page.locator(".shop-row", has_text="按 Enter 加的").count() == 1
                    and page.evaluate("document.activeElement.id") == "sn-n"
                    and form.locator("#sn-n").get_attribute("enterkeyhint") == "done")

            # 內容、數量、幫誰買一次填好；新增後停在原地（原本每加一項就被拉回最上面）
            # 名稱剛才被 Enter 送出清掉了，重新打一次
            form.locator("#sn-n").fill("面膜")
            form.locator("#sn-g").select_option("藥妝店")
            form.locator('[name="d"]').fill("肌研 50ml")
            form.locator('[name="q"]').fill("2")
            form.locator('[name="w"]').fill("媽媽")
            add_btn = form.locator('button[type="submit"]')
            add_btn.scroll_into_view_if_needed()
            y0 = page.evaluate("scrollY")
            add_btn.click()
            page.wait_for_timeout(600)
            y1 = page.evaluate("scrollY")
            row = page.locator('.shop-g[data-group="藥妝店"] .shop-row', has_text="面膜")
            r.check("購物：新增後項目出現在選好的分類", row.count() == 1)
            r.check("購物：內容、數量、幫誰買都顯示在項目上",
                    row.count() == 1 and all(x in row.inner_text() for x in ("肌研 50ml", "數量 2", "幫媽媽買")),
                    row.inner_text() if row.count() else "")
            r.check("購物：新增後停在原地、不跳回最上面", y0 > 200 and abs(y1 - y0) < 5, f"{y0} → {y1}")
            r.check("購物：新增後欄位清空、游標回到名稱，可以接著打下一個",
                    form.locator("#sn-n").input_value() == "" and form.locator('[name="w"]').input_value() == ""
                    and page.evaluate("document.activeElement.id") == "sn-n")
            r.check("購物：新增後提示加到哪一類", "藥妝店" in page.locator("#toast").inner_text(),
                    page.locator("#toast").inner_text())
            add_btn.click()
            page.wait_for_timeout(200)
            r.check("購物：新增欄位沒填名稱，錯誤寫在欄位底下",
                    page.locator("#sn-n-err").is_visible() and page.locator("#sn-n").get_attribute("aria-invalid") == "true")

            # 代購結算：「幫誰買」有填的，照人加總實付金額，打金額的時候就地更新
            row.locator(".shop-amt").fill("2560")
            page.wait_for_timeout(200)
            settle = page.locator("#shop-settle").inner_text()
            r.check("代購結算：照人加總實付金額（托買的東西 1,600＋面膜 2,560）",
                    "媽媽" in settle and "AUD 4,160" in settle and "約 TWD 89,440" in settle, settle.replace("\n", " ")[:120])
            r.check("代購結算：還沒填金額的列出來", "同事" in settle and "還沒填實付金額：自己加的東西" in settle,
                    settle.replace("\n", " ")[:160])
            r.check("代購結算：兩個人以上有總計", "全部代購" in settle)
            r.check("代購結算：打金額時輸入框不失焦", page.evaluate("document.activeElement.classList.contains('shop-amt')"))

            # 鉛筆：改內容、數量、幫誰買、分類。存了才重畫（分類變了要搬家）
            row.locator("[data-sedit]").click()
            box = row.locator(".sr-editbox")
            r.check("編輯：鉛筆打開編輯欄，帶入目前的值",
                    box.count() == 1 and box.locator('[name="d"]').input_value() == "肌研 50ml"
                    and box.locator('[name="q"]').input_value() == "2" and box.locator('[name="w"]').input_value() == "媽媽"
                    and row.locator("[data-sedit]").get_attribute("aria-expanded") == "true")
            box.locator('[name="n"]').fill("")
            box.locator('button[type="submit"]').click()
            r.check("編輯：名稱清空不能存，錯誤寫在欄位底下",
                    box.locator(".f-err").is_visible() and box.locator('[name="n"]').get_attribute("aria-invalid") == "true")
            box.locator('[name="n"]').fill("面膜")
            box.locator('[name="q"]').fill("3")
            box.locator('[name="g"]').select_option("伴手禮")
            box.locator('button[type="submit"]').click()
            page.wait_for_timeout(400)
            moved = page.locator('.shop-g[data-group="伴手禮"] .shop-row', has_text="面膜")
            r.check("編輯：改分類後搬到新的群組，實付金額跟著走",
                    moved.count() == 1 and page.locator('.shop-g[data-group="藥妝店"] .shop-row', has_text="面膜").count() == 0
                    and moved.locator(".shop-amt").input_value() == "2560" and "數量 3" in moved.inner_text())
            r.check("編輯：存完焦點回到那一項的鉛筆",
                    page.evaluate("!!document.activeElement.closest('.shop-row')?.innerText.includes('面膜') && document.activeElement.matches('[data-sedit]')"))

            # 價格標籤：拍一張，縮成 44px 的縮圖，點了放大。照片存 IndexedDB，key 是「id#tag」
            moved.locator("[data-stag]").set_input_files({"name": "tag.png", "mimeType": "image/png", "buffer": PNG})
            try: page.wait_for_function("""() => [...document.querySelectorAll('.sr-tag.has img')].some(i => i.src.startsWith('blob:'))""", timeout=5000)
            except Exception: pass
            tag_img = moved.locator(".sr-tag.has img")
            r.check("價格標籤：拍了之後變成縮圖、有替代文字",
                    tag_img.count() == 1 and (tag_img.get_attribute("src") or "").startswith("blob:")
                    and "價格標籤" in (tag_img.get_attribute("alt") or ""))
            r.check("價格標籤：照片存在 IndexedDB、key 是「項目 id#tag」",
                    any(str(k).endswith("#tag") for k in (idb_keys() or [])), str(idb_keys()))
            r.check("價格標籤：實付金額沒有被洗掉", moved.locator(".shop-amt").input_value() == "2560")
            tag_img.click()
            page.wait_for_timeout(300)
            r.check("價格標籤：點了放大，可以換一張或移除",
                    page.locator("#zoom[open]").count() == 1 and acts.locator("[data-zdel]").count() == 1)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(600)
            r.check("價格標籤：重新整理後還在", moved.locator(".sr-tag.has img").count() == 1)
            # 刪掉這一項：價格標籤的照片也要等復原時限過了一起刪
            moved.locator(".shop-x").click()
            page.wait_for_function("!document.querySelector('#toast')?.classList.contains('show')", timeout=9000)
            page.wait_for_timeout(500)
            r.check("價格標籤：刪掉項目，復原時限過了照片也一起刪",
                    not any(str(k).endswith("#tag") for k in (idb_keys() or [])), str(idb_keys()))

            # 刪除也停在原地
            page.locator(".shop-row").last.scroll_into_view_if_needed()
            y0 = page.evaluate("scrollY")
            page.locator(".shop-row").last.locator(".shop-x").click()
            page.wait_for_timeout(500)
            y1 = page.evaluate("scrollY")
            r.check("購物：刪除後停在原地、不跳回最上面", y0 > 200 and abs(y1 - y0) < 60, f"{y0} → {y1}")
            page.locator("#toast .toast-act").click()
            page.wait_for_timeout(400)

            # 收起來的群組（十幾張的優惠券）重畫、重新整理後維持收起來
            page.locator('.shop-g[data-group="優惠券"] .shop-gn').click()
            page.wait_for_timeout(200)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            page.locator(".nav-item").nth(1).click()
            page.wait_for_timeout(400)
            closed = page.evaluate("""() => !document.querySelector('.shop-g[data-group="優惠券"]').open""")
            page.locator('.shop-g[data-group="優惠券"] .shop-gn').click()
            page.wait_for_timeout(200)
            r.check("購物：收起來的群組重新整理後維持收起來", closed)

            # 改版前的資料（大家手機上已經加的東西）：「數量／備註」在 edits.note、金額在 edits.amount。
            # 要照樣畫得出來，打開編輯欄時「內容」帶入原本的備註，存了之後字不會不見
            octx = browser.new_context()
            op = octx.new_page()
            op.goto(swap)
            op.wait_for_load_state("networkidle")
            op.evaluate("""() => localStorage.setItem('trip:swaptest-2027', JSON.stringify({ shopping: {
                added: [ { id: '+old1', group: '藥妝店', name: '舊的面膜' },
                         { id: '+old2', group: '超市', name: '舊的明太子', url: 'https://example.com/x' } ],
                edits: { '+old1': { done: true, note: '2 盒 幫媽媽', amount: 1280 }, '藥妝店:托買的東西': { note: '舊備註' } },
                removed: [], groups: ['超市'] } }))""")
            op.reload(wait_until="domcontentloaded")
            op.wait_for_timeout(1200)
            op.locator(".nav-item").nth(1).click()
            op.wait_for_timeout(400)
            old = op.locator('.shop-g[data-group="藥妝店"] .shop-row', has_text="舊的面膜")
            r.check("舊資料：改版前加的東西都在、在原本的群組",
                    old.count() == 1 and op.locator('.shop-g[data-group="超市"] .shop-row', has_text="舊的明太子").count() == 1)
            r.check("舊資料：勾選、金額、「數量／備註」都還在",
                    old.locator("input[type=checkbox]").is_checked() and old.locator(".shop-amt").input_value() == "1280"
                    and "2 盒 幫媽媽" in old.inner_text() and "舊備註" in op.locator('.shop-i[data-id="藥妝店:托買的東西"]').inner_text(),
                    old.inner_text() if old.count() else "")
            old.locator("[data-sedit]").click()
            r.check("舊資料：編輯時「內容」帶入原本的備註",
                    old.locator('.sr-editbox [name="d"]').input_value() == "2 盒 幫媽媽")
            old.locator('.sr-editbox button[type="submit"]').click()
            op.wait_for_timeout(300)
            ed = op.evaluate("JSON.parse(localStorage.getItem('trip:swaptest-2027')).shopping")
            a1 = next(a for a in ed["added"] if a["id"] == "+old1")
            r.check("舊資料：存了之後備註搬進「內容」，勾選與金額不動",
                    a1.get("desc") == "2 盒 幫媽媽" and "note" not in ed["edits"]["+old1"]
                    and ed["edits"]["+old1"].get("done") is True and ed["edits"]["+old1"].get("amount") == 1280, str(ed["edits"]["+old1"]))
            octx.close()

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
                unlock_wallet(page)
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
            unlock_wallet(page)
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

            # 2026-11-17 03:35Z = 東京 12:35，上色見熊野座神社（11:30 起、停留 95 分）進行中
            ctx, page = at("2026-11-17T03:35:00Z")
            nowc = page.locator("#main .card.is-now")
            r.check("目前行程：剛好標出一張卡片", nowc.count() == 1, f"{nowc.count()} 張")
            r.check("目前行程：標在上色見熊野座神社上", "上色見熊野座神社" in nowc.inner_text())
            line = page.locator("#main .card.is-now .now-line").inner_text()
            # 這一行只留它獨有的那半句（還剩多久）。幾點離開改印在卡片上，兩個地方都寫
            # 就會在同一張卡片上出現兩次 13:05
            r.check("目前行程：顯示還剩多久", "還有 30 分" in line, f"「{line}」")
            r.check("目前行程：離開時刻改由卡片本身顯示，標記列不重複",
                    nowc.locator(".stay.leave").inner_text() == "離開 13:05" and "13:05" not in line,
                    f"卡片「{nowc.locator('.stay.leave').inner_text()}」／標記列「{line}」")
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
            # 旅行當天的「在 Google 地圖開啟今天的路線」：不帶起點（Google 地圖就從裝置的現在位置出發），
            # 已經過去的站不帶——跟上面「現在進行到哪」同一套判斷。進行中的那一站還要帶（可能還在路上）
            from urllib.parse import urlparse as _up, parse_qs as _pq
            link = page.locator(".daymap-open")
            href = link.get_attribute("href") if link.count() else ""
            lq = _pq(_up(href).query)
            r.check("旅行當天：今天的路線從現在位置出發（不帶起點）",
                    link.count() == 1 and "origin" not in lq and lq.get("travelmode") == ["driving"], href[:90])
            r.check("旅行當天：跳過已經過去的站，從進行中的那一站開始",
                    lq.get("waypoints", [""])[0].startswith("上色見熊野座神社")
                    and "金鱗湖" not in href and "草千里" not in href, lq.get("waypoints", [""])[0][:60])
            r.check("旅行當天：終點還是今晚的住宿", lq.get("destination") == ["亀の井ホテル 阿蘇, 熊本縣阿蘇市"],
                    str(lq.get("destination")))
            ctx.close()

            # 東京 06:00，D4 還沒開始：從現在位置出發，原本的第一站（前一晚的住宿）不帶，第一個途經點是金鱗湖
            ctx, page = at("2026-11-16T21:00:00Z")
            href = page.locator(".daymap-open").get_attribute("href") or ""
            lq = _pq(_up(href).query)
            r.check("旅行當天（還沒出門）：不帶起點，第一站是當天第一個景點",
                    "origin" not in lq and lq.get("waypoints", [""])[0].startswith("金鱗湖"), href[:120])
            # 別天的分頁：照舊從那天的第一站出發（出發前規劃時現在位置在台灣，規劃不出來）
            page.locator(".nav-item", has_text="D5").first.click(); page.wait_for_timeout(300)
            href = page.locator(".daymap-open").get_attribute("href") or ""
            r.check("旅行當天：看別天的分頁時，那天的連結照舊從第一站出發",
                    _pq(_up(href).query).get("origin") == ["亀の井ホテル 阿蘇, 熊本縣阿蘇市"], href[:120])
            ctx.close()

            # 東京 11:40：已經從草千里出發、還沒到上色見熊野座神社（11:30 出發、交通 50 分）
            ctx, page = at("2026-11-17T02:40:00Z")
            nowc = page.locator("#main .card.is-now")
            line = nowc.locator(".now-line").inner_text() if nowc.count() else ""
            r.check("在路上：標「前往中」與預計抵達時刻",
                    nowc.count() == 1 and "上色見熊野座神社" in nowc.inner_text() and "前往中" in line and "12:20" in line,
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
                # 固定在不是任何一趟行程的日子：旅行當天的連結會改從現在位置出發（第 7c 段另外測），
                # 不固定的話在九州行程期間跑測試，D2 那條會因為「今天就是 D2」而失敗
                pg.clock.set_fixed_time("2026-10-01T03:00:00Z")
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
            r.check("地圖：終點是當天最後一站（住宿），地圖按鈕釘了 place ID 就用 ID 查",
                    qs.get("destination") == ["place_id:TEST_PID_HOTEL"], str(qs.get("destination")))
            r.check("地圖：當天有開車就畫開車路線", qs.get("mode") == ["driving"], str(qs.get("mode")))
            mb, db = page.locator("main").bounding_box(), dm.bounding_box()
            r.check("地圖（寬 1400）：在行程右邊的第三欄", db["x"] > mb["x"] + mb["width"] - 1,
                    f"地圖 x={round(db['x'])}、行程右緣 {round(mb['x'] + mb['width'])}")
            href = page.locator(".daymap-open").get_attribute("href") or ""
            r.check("地圖：「在 Google 地圖開啟」是同一條路線",
                    "maps/dir/?api=1" in href and "travelmode=driving" in href and "Sydney" in href, href[:80])
            hq = parse_qs(urlparse(href).query)
            r.check("地圖：「在 Google 地圖開啟」不是旅行當天，從第一站出發、終點用名稱",
                    hq.get("origin") == ["Sydney Opera House, Bennelong Point"]
                    and hq.get("destination") == ["測試飯店, 1 Test St"], href[:120])

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
            OPERA = "Sydney Opera House, Bennelong Point"
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
            r.check("點卡片：用鍵盤也能選（Enter）；釘了 place ID 的點用 ID 標",
                    map_q() == ("place", "place_id:TEST_PID_HOTEL") and pressed(H), str(map_q()))
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

            # 當天幾點起床：當天第一個行程往前一小時，一條小的緊接在第一段行程上面（不是卡片）。
            # 「在 Google 地圖開啟今天的路線」在它上方，不能夾在起床與第一段行程中間（使用者指定）
            WAKE_SPOT = """() => { const w = document.querySelector('#main .wake-line');
                return !!w && !!w.previousElementSibling?.classList.contains('daymap-open')
                    && !!w.nextElementSibling?.matches('.leg, .card'); }"""
            page.locator(".nav-item").nth(0).click(); page.wait_for_timeout(300)
            wake = page.locator("#main .wake-line")
            r.check("起床時間：緊接在第一段行程上面、地圖連結在它上方，不在卡片裡",
                    wake.count() == 1 and page.evaluate(WAKE_SPOT)
                    and page.locator("#main .card .wake-line").count() == 0,
                    f"{wake.count()} 條")
            r.check("起床時間：第一個行程（10:00）的前一小時",
                    wake.count() == 1 and "09:00" in wake.inner_text(),
                    wake.inner_text() if wake.count() else "")
            page.locator(".nav-item").nth(1).click(); page.wait_for_timeout(300)
            wake = page.locator("#main .wake-line")
            r.check("起床時間：要先出門的日子照出門時間算",
                    wake.count() == 1 and "09:00" in wake.inner_text()
                    and "10:00 出門・戊站" in wake.inner_text(),
                    wake.inner_text() if wake.count() else "")
            r.check("起床時間：要先出門的日子，緊接在交通時間那條上面",
                    page.evaluate(WAKE_SPOT) and page.evaluate(
                        "() => document.querySelector('#main .wake-line').nextElementSibling.classList.contains('leg')"))
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

            unlock_wallet(page)
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
            unlock_wallet(page)
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
                photo_errors, photo_404 = [], []
                page.on("pageerror", lambda e: photo_errors.append(str(e)))
                page.on("response", lambda res: res.status >= 400 and "/img/" in res.url
                        and photo_404.append(f"{res.status} {res.url}"))
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
                # 要比「解析出來的完整網址」，不能只看有沒有包含 img/intro/…：程式搬到 app/ 之後，
                # 寫在 app.css 裡的 var(--photo) 被瀏覽器從 app/ 算起，變成 /app/img/intro/… 而 404，
                # 只比對包含的話照樣會過（踩過）
                want = page.evaluate("s => new URL(s, location.href).href", photo_src)
                r.check("介紹照片：收合時那一條鋪著照片（網址從行程資料夾算起）",
                        bg[0] == f'url("{want}")' and float(bg[1]) > 0, f"{bg}")
                r.check("介紹照片：照片真的載得到，沒有 404", not photo_404, "; ".join(photo_404[:2]))
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

                photo_404.clear()
                photo_failed = []
                page.on("requestfailed", lambda q: "/img/" in q.url and photo_failed.append(q.url))
                ctx.set_offline(True)
                page.reload()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(800)
                r.check("介紹照片：離線時收合那一條的照片也從快取載得到",
                        not photo_404 and not photo_failed, "; ".join((photo_404 + photo_failed)[:2]))
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
            # 配色切換鍵：手機上要跟分頁同一列，不能自己佔一列（旅途工具變成四顆之後，
            # 留在工具後面的話會被擠到下一列，所以搬到「資訊」右邊——使用者選的）
            alone = page.evaluate("""() => { const s = document.getElementById('scheme').getBoundingClientRect();
                return ![...document.querySelectorAll('.nav-aux .nav-item')].some(t => {
                  const r = t.getBoundingClientRect(); return r.top < s.bottom && s.top < r.bottom; }); }""")
            r.check("導覽：手機上配色切換鍵跟分頁在同一列（不會自己佔一列）", not alone)
            seed_board(page)   # 留言板空的話，下面的探針量不到留言列與 ✕
            seed_shop(page)    # 購物清單空的話，量不到一項一列、編輯欄、代購結算
            page.get_by_role("tab", name=re.compile("留言板")).click()
            page.wait_for_timeout(300)
            r.check("留言板：UI 規則量到的是有留言的清單（不是空的）",
                    page.locator("#main .bd-item").count() == len(BOARD_SEED)
                    and page.locator("#main .bd-del").count() == 1
                    and page.locator("#main .bd-del").first.is_visible()
                    # 可疑那一則也要在畫面上，.bd-odd 的對比才量得到
                    and page.locator("#main .bd-item.is-odd").count() == 1
                    and page.locator("#main #bd-add").count() == 1,
                    f"{page.locator('#main .bd-item').count()} 則、"
                    f"{page.locator('#main .bd-del').count()} 顆 ✕、"
                    f"{page.locator('#main .bd-item.is-odd').count()} 則可疑")
            # 剛剛點進留言板會把未讀清掉，但下面的探針要量分頁上那顆徽章的對比，所以還原
            page.evaluate("localStorage.setItem('trip:kyushu-fukuoka-2026:bdseen', '0')")
            page.reload()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            r.check("留言板：UI 規則也量得到分頁上的未讀數",
                    page.locator("#tab-board .bd-badge").is_visible())
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

            def ux_pass(tab):
                page.evaluate("document.querySelectorAll('#main details').forEach(d => d.open = true)")
                if page.locator("#main [data-sedit]").count():   # 購物：鉛筆打開的編輯欄也要量
                    page.locator("#main [data-sedit]").first.click()
                    page.wait_for_timeout(100)
                    page.evaluate("window.scrollTo(0, 0)")
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

            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(300)
                locked = page.locator("#w-pass").count() > 0
                ux_pass(tab + "（密碼鎖）" if locked else tab)
                if locked:          # 記帳：鎖著的密碼框與解開後的表單都要過同一組規則
                    unlock_wallet(page)
                    ux_pass(tab)
            # 信用卡「修改總額」的鉛筆鍵要貼在「已計入」右邊，金額大了也一樣。
            # 那一行原本會折行：金額到千位，文字＋44px 的鍵就超過 375px，鍵掉到下一行，
            # 每張卡片多 30 幾 px。只用 $0 量的話看不出來（踩過一次），所以刻意加到六位數
            page.locator("#tab-info").click()
            page.wait_for_timeout(300)
            cc_big = page.locator("#main .cc:has(.cc-add-input)").first
            if cc_big.count():
                cc_big.locator(".cc-add-input").fill("123456")
                cc_big.locator(".cc-add-btn").click()
                page.wait_for_timeout(200)
                pen = cc_big.locator(".cc-fix").bounding_box()
                used = cc_big.locator(".cc-used").bounding_box()
                same_row = (pen and used and pen["y"] < used["y"] + used["height"]
                            and used["y"] < pen["y"] + pen["height"])
                r.check("信用卡：金額六位數時，鉛筆鍵仍然跟「已計入」同一行（375px）", bool(same_row),
                        f"已計入 y={used and round(used['y'])}–{used and round(used['y'] + used['height'])}，"
                        f"鍵 y={pen and round(pen['y'])}–{pen and round(pen['y'] + pen['height'])}")
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
                unlock_wallet(page)
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
            seed_board(page)
            seed_shop(page)
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
                  // 兩張卡片中間的交通標籤不在卡片裡，跟頁面背景比
                  const card = el.matches('.card') || el.closest('.leg') ? document.body : el.closest('.card');
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
                if page.locator("#main [data-sedit]").count():
                    page.locator("#main [data-sedit]").first.click()
                    page.wait_for_timeout(100)
                if page.locator("#w-pass").count():   # 記帳：鎖著與解開後都量
                    dark_fail += [f"{tab}（密碼鎖）：{x}" for x in page.evaluate(UX_PROBE)["contrast"]]
                    dark_flat += [f"{tab}（密碼鎖）：{x}" for x in page.evaluate(FLAT_PROBE)]
                    unlock_wallet(page)
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
            seed_board(page)
            unlabeled = []
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(300)
                if page.locator("#w-pass").count():   # 記帳：鎖著與解開後都量
                    unlabeled += [f"{tab}（密碼鎖）：{x}" for x in page.evaluate(UX_PROBE)["label"]]
                    unlock_wallet(page)
                unlabeled += [f"{tab}：{x}" for x in page.evaluate(UX_PROBE)["label"]]
            r.check("UI 規則：電腦版輸入框也都有看得見的標籤", not unlabeled, "；".join(sorted(set(unlabeled))[:4]) or "各分頁都過")
            ctx.close()

            # 配色：⚠️ 開頭的句子拆成自己的提醒框（.note-box.warn），一般說明框裡不能再混著提醒。
            # 用圖示判斷：⚠ 是唯一會換成 warning 圖示的 emoji（EMOJI_IC），文字比對換了行程就不準
            ctx = browser.new_context(viewport={"width": 375, "height": 812})
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            mixed, warn_boxes, warn_bad, dup, order = [], 0, [], [], []
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(200)
                unlock_wallet(page)
                res = page.evaluate("""() => {
                  const out = { mixed: 0, boxes: 0, bad: 0, dup: [], order: [] };
                  out.mixed = document.querySelectorAll('#main .note-box:not(.warn) > .ic-warn').length;
                  for (const w of document.querySelectorAll('#main .note-box.warn')){
                    if (w.checkVisibility()) out.boxes++;
                    if (!(w.firstElementChild && w.firstElementChild.matches('.ic-warn'))) out.bad++;
                  }
                  // 卡片層級的說明框：直接掛在 .card 底下的那些（交通卡每一站自己的提醒不算）
                  for (const card of document.querySelectorAll('#main .card')){
                    const own = [...card.children].filter(e => e.matches('.note-box'));
                    const plain = own.filter(e => !e.classList.contains('warn'));
                    const warn = own.filter(e => e.classList.contains('warn'));
                    const name = (card.querySelector('.subject') || {}).textContent || '(無標題)';
                    if (plain.length > 1 || warn.length > 1)
                      out.dup.push(`${name.trim().slice(0, 14)} 說明 ${plain.length}／提醒 ${warn.length}`);
                    // 順序固定：說明在上、提醒在下
                    if (plain.length && warn.length && own.indexOf(plain[0]) > own.indexOf(warn[0]))
                      out.order.push(name.trim().slice(0, 14));
                  }
                  return out;
                }""")
                if res["mixed"]: mixed.append(f"{tab} {res['mixed']} 句")
                if res["bad"]: warn_bad.append(f"{tab} {res['bad']} 個")
                warn_boxes += res["boxes"]
                dup += [f"{tab}：{x}" for x in res["dup"]]
                order += [f"{tab}：{x}" for x in res["order"]]
            r.check("配色：⚠️ 提醒拆成獨立的提醒框，不混在一般說明裡", not mixed and not warn_bad and warn_boxes > 0,
                    f"提醒框 {warn_boxes} 個" + (f"；混在說明裡：{'、'.join(mixed)}" if mixed else "")
                    + (f"；提醒框開頭不是提醒圖示：{'、'.join(warn_bad)}" if warn_bad else ""))
            # 使用者明確不要：同一張卡片出現兩個說明框。說明與提醒各最多一個，說明在上
            r.check("說明框：一張卡片最多一個說明框＋一個提醒框", not dup,
                    "；".join(sorted(set(dup))[:4]) or "各分頁都過")
            r.check("說明框：提醒框一律在說明框下面", not order,
                    "；".join(sorted(set(order))[:4]) or "各分頁都過")

            # 起床那一條：寫出來的日子都要是「出發前整整一小時」，而且出發在中午前。
            # 中午以後才開始的日子不寫——抵達日的第一個行程是晚上落地，往前推會變成
            # 「19:00 起床」，那天根本不是在飯店醒來的。這條會抓到把判斷拿掉的改動
            wake_bad, wake_seen = [], 0
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(200)
                w = page.locator("#main .wake-line")
                if not w.count():
                    continue
                wake_seen += 1
                txt = w.inner_text()
                hhmm = __import__("re").findall(r"\d{1,2}:\d{2}", txt)
                if len(hhmm) < 2:
                    wake_bad.append(f"{tab}：讀不到兩個時間（{txt}）")
                    continue
                mins = [int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in hhmm[:2]]
                if (mins[1] - mins[0]) % 1440 != 60:
                    wake_bad.append(f"{tab}：{hhmm[0]} → {hhmm[1]} 不是整整一小時")
                if mins[1] >= 12 * 60:
                    wake_bad.append(f"{tab}：出發 {hhmm[1]} 已經過中午，不該寫起床")
            r.check("起床時間：每天都是出發前一小時，而且只寫早上開始的日子",
                    not wake_bad and wake_seen > 0,
                    "；".join(wake_bad[:3]) or f"{wake_seen} 天有寫")

            # 預計離開時間：真的行程每一天、每一張卡片都掃一次。
            # 一、卡片自己要自洽：離開 − 抵達 ＝ 卡片上那個停留（交通卡是總耗時）。
            #     time + stay 跟 (time+travel) + (stay−travel) 是同一個數，所以這條等於在驗
            #     「交通有沒有被重複加一次」——漏算或多算交通的話，這裡會差掉整段車程。
            # 二、往後接得上：離開之後再走 .leg 的交通時間，就該是下一張卡片的抵達時間。
            #     行程表允許中間留一點空檔，所以只抓「倒退超過 5 分」；D5 的 18:32 還完車、
            #     晚餐卡片寫 18:30 出發，是資料本身排的 2 分鐘重疊，不是算錯。
            LEAVE_SWEEP = r"""() => {
              const hhmm = t => { const m = /(\d{1,2}):(\d{2})/.exec(t || ""); return m ? +m[1] * 60 + +m[2] : null; };
              const dur  = t => { const s = t || "", h = /(\d+)\s*小時/.exec(s),
                                  n = /(\d+)\s*分/.exec(s.replace(/\d+\s*小時/, ""));
                                  return (h ? +h[1] * 60 : 0) + (n ? +n[1] : 0); };
              const out = { bad: [], gaps: [], seen: 0 };
              let prev = null;                     // 上一張卡片的 { leave, name }
              for (const c of document.querySelectorAll("#main .card")){
                const name = ((c.querySelector(".subject") || {}).textContent || "(無標題)").trim().slice(0, 12);
                const tEl = c.querySelector(".time"), lEl = c.querySelector(".stay.leave"),
                      sEl = c.querySelector(".stay:not(.leave)");
                const sib = c.previousElementSibling;
                const leg = sib && sib.classList.contains("leg") ? dur(sib.textContent) : 0;
                if (!tEl){ prev = null; continue; }
                const arrive = hhmm(tEl.textContent);
                if (!lEl){
                  // 沒有離開時間的，一定是因為算不出來（沒填停留）——有停留卻沒有就是漏畫了
                  if (sEl && !/總耗時/.test(sEl.textContent))
                    out.bad.push(`${name}：有「${sEl.textContent.trim()}」卻沒有離開時間`);
                  prev = null;
                  continue;
                }
                out.seen++;
                const leave = hhmm(lEl.textContent) + (/隔天/.test(lEl.textContent) ? 1440 : 0);
                const stay  = dur(sEl ? sEl.textContent : "");
                if (!sEl || leave - arrive !== stay)
                  out.bad.push(`${name}：抵達 ${tEl.textContent} ＋「${(sEl || {}).textContent || "沒有停留"}」`
                               + ` 對不上「${lEl.textContent}」`);
                if (prev !== null && prev.leave + leg !== arrive)
                  out.gaps.push({ over: prev.leave + leg - arrive, from: prev.name, to: name,
                                  at: tEl.textContent });
                prev = { leave, name };
              }
              return out;
            }"""
            leave_bad, leave_seen, leave_over = [], 0, []
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(200)
                res = page.evaluate(LEAVE_SWEEP)
                leave_bad += [f"{tab}：{x}" for x in res["bad"]]
                leave_seen += res["seen"]
                leave_over += [g for g in res["gaps"] if g["over"] > 0]
            r.check("離開時間：離開 − 抵達 就是卡片上的停留（交通只算一次）",
                    not leave_bad and leave_seen > 0,
                    "；".join(leave_bad[:3]) or f"{leave_seen} 張卡片都對得起來")
            worst = max((g["over"] for g in leave_over), default=0)
            where = "、".join(f"{g['from']} → {g['to']}（{g['at']}）" for g in leave_over if g["over"] == worst)
            r.check("離開時間：接得上下一張卡片的抵達時間（不會倒退）", worst <= 5,
                    f"最多重疊 {worst} 分：{where}" if leave_over else "每一段都剛好接上")

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

            # 13 ── 卡片上不顯示當地語（日文）名稱 ──────────────
            # 使用者的決定：全部行程都只留中文名稱。資料裡的 titleLocal／placeLocal／
            # nameLocal 仍然要留著——地圖按鈕與電腦版當日路線靠「當地語名稱, 地點」去查。
            # 所以這裡掃的是真實行程的每一個分頁、讀畫面，不是看資料。
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(base)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            jp_names = local_names(site_dir / "index.html")
            jp_shown, jp_spans = [], 0
            tabs = page.locator(".nav-item")
            for i in range(tabs.count()):
                tab = tabs.nth(i).inner_text().split("\n")[0]
                tabs.nth(i).click()
                page.wait_for_timeout(200)
                jp_spans += page.locator("#main .lang-local").count()
                # 只看「名字會出現的地方」：卡片標題、交通卡站名、二選一的店名。
                # 整個 #main 一起看的話，說明與營業時間本來就會提到當地語
                # （「推薦ダコメッカドッグ」、「マイング 9:00–21:00」），那是資料要顯示的字。
                # 被拿掉的「地點」那一行跟著地點欄位走，由置換資料那一段精確地驗
                txt = "\n".join(page.locator(
                    "#main .subject, #main .route-body strong, #main .choice-main > div:first-child"
                ).all_inner_texts())
                jp_shown += [f"{tab}：{n}" for n in jp_names if n in txt]
            # 第二條防的是空測試：資料哪天不填當地語了，上面那條會永遠通過
            r.check("當地語：真實行程裡確實有當地語名稱可以顯示（不是空測試）",
                    len(jp_names) >= 10, f"{len(jp_names)} 個")
            r.check("當地語：卡片上一個都看不到", not jp_shown and jp_spans == 0,
                    "；".join(sorted(set(jp_shown))[:4]) or f"{jp_spans} 個 .lang-local")
            ctx.close()

            # 14 ── 記帳的密碼鎖 ───────────────────────────────
            # 使用者要求記帳要密碼才看得到（0000）。這是一道簾子、不是鎖：repo 是公開的，
            # 密碼在原始碼裡讀得到，Firestore 的規則也只看「匿名登入＋tripId」。所以這裡
            # 驗的是「沒輸入密碼就看不到帳」與「解開之後記在這台裝置」，不是安全性。
            ctx = browser.new_context()
            page = ctx.new_page()
            lock_err, lock_said = [], []
            page.on("pageerror", lambda e: lock_err.append(str(e)))
            page.on("dialog", lambda d: (lock_said.append(d.message), d.dismiss()))
            page.goto(base)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            wallet_tab = page.get_by_role("tab", name=re.compile("記帳"))
            r.check("密碼鎖：記帳分頁照樣在導覽列上（鎖的是內容，不是入口）",
                    wallet_tab.count() == 1 and wallet_tab.is_visible())
            wallet_tab.click()
            page.wait_for_timeout(300)
            r.check("密碼鎖：沒輸入密碼時看不到記帳表單",
                    page.locator("#w-pass").is_visible()
                    and page.locator("#e-add").count() == 0
                    and "新增花費" not in page.locator("#main").inner_text())

            page.locator("#w-form button[type=submit]").click()   # 空白
            page.wait_for_timeout(200)
            r.check("密碼鎖：沒填密碼時錯誤寫在欄位底下",
                    page.locator("#w-err").is_visible()
                    and "請輸入密碼" in page.locator("#w-err").inner_text())
            page.locator("#w-pass").fill("1234")
            page.locator("#w-form button[type=submit]").click()
            page.wait_for_timeout(200)
            r.check("密碼鎖：密碼不對時不開，而且不跳 alert",
                    page.locator("#e-add").count() == 0 and not lock_said
                    and "密碼不對" in page.locator("#w-err").inner_text(),
                    "／".join(lock_said))
            r.check("密碼鎖：錯的欄位標成 aria-invalid、用 describedby 綁住錯誤",
                    page.locator("#w-pass").get_attribute("aria-invalid") == "true"
                    and page.locator("#w-pass").get_attribute("aria-describedby") == "w-err")

            unlock_wallet(page)
            r.check("密碼鎖：輸入 0000 之後記帳表單出現", page.locator("#e-add").count() == 1)
            page.locator("#e-item").fill("密碼鎖測試")
            page.locator("#e-local").fill("9000")
            page.locator("#e-add").click()
            page.wait_for_timeout(600)
            r.check("密碼鎖：解開之後記帳照常可用",
                    "密碼鎖測試" in page.locator("#main").inner_text())

            page.reload()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            page.get_by_role("tab", name=re.compile("記帳")).click()
            page.wait_for_timeout(300)
            r.check("密碼鎖：重新整理後這台裝置還記得（使用者選的）",
                    page.locator("#e-add").count() == 1 and page.locator("#w-pass").count() == 0)

            page.locator("#e-lock").click()
            page.wait_for_timeout(300)
            r.check("密碼鎖：按「上鎖」鎖回去，帳也跟著從畫面上消失",
                    page.locator("#w-pass").is_visible()
                    and "密碼鎖測試" not in page.locator("#main").inner_text())
            page.reload()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            page.get_by_role("tab", name=re.compile("記帳")).click()
            page.wait_for_timeout(300)
            r.check("密碼鎖：上鎖之後重新整理還是鎖著", page.locator("#w-pass").is_visible())

            page.locator(".nav-item").first.click()   # 行前
            page.wait_for_timeout(300)
            r.check("密碼鎖：其他分頁不受影響", page.locator("#main .chk").count() > 0)
            r.check("密碼鎖：整段沒有 JS 例外", not lock_err, "; ".join(lock_err[:2]))
            ctx.close()

            # 15 ── 程式只有一份，在 app/ ─────────────────────────
            # 程式與樣式從 index.html 搬到 repo 根目錄的 app/app.js、app/app.css（使用者要的：
            # 程式碼短一點、改一個地方就好）。index.html 只剩資料。要守的有三件：
            #   一、兩個 HTML 都是引用，不再內嵌——不然又變回兩份，改一邊另一邊會退回舊版
            #   二、Service Worker 預先快取它們——離線要能開
            #   三、而且走「網路優先」——快取優先的話，開過網站的手機會一直用舊程式，
            #       新的 index.html 配上舊的 app.js 甚至可能直接打不開
            idx_src = (site_dir / "index.html").read_text(encoding="utf-8")
            tpl_src = (REPO / "builder" / "assets" / "site-template.html").read_text(encoding="utf-8")
            sw_src = (site_dir / "sw.js").read_text(encoding="utf-8")
            for name, html in (("index.html", idx_src), ("site-template.html", tpl_src)):
                r.check(f"共用程式：{name} 引用 ../app/ 的程式與樣式，自己不再內嵌",
                        '<script type="module" src="../app/app.js"></script>' in html
                        and '<link rel="stylesheet" href="../app/app.css">' in html
                        and "邏輯層 —— 以下皆不需修改" not in html and "<style>" not in html)
            r.check("共用程式：Service Worker 預先快取 app.js 與 app.css",
                    '"../app/app.js"' in sw_src and '"../app/app.css"' in sw_src)

            ctx = browser.new_context()
            page = ctx.new_page()
            app_err = []
            page.on("pageerror", lambda e: app_err.append(str(e)))
            page.goto(base)
            page.wait_for_load_state("networkidle")
            page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=15000)
            page.reload()
            page.wait_for_load_state("networkidle")
            # 搬家之後相對路徑有兩個基準：app.js 組出來、放進 HTML 的網址從 index.html 算起，
            # 但 app.css 裡的 url()——包括透過 var() 帶進去的——從 app/ 算起。每個分頁走一遍，
            # 同網域的檔案一個都不能失敗（踩過：介紹照片的淡底變成 /app/img/intro/… 而 404）
            broken = []
            page.on("response", lambda res: res.url.startswith(f"http://127.0.0.1:{port}/")
                    and res.status >= 400 and broken.append(f"{res.status} {res.url}"))
            for i in range(page.locator(".nav-item").count()):
                page.locator(".nav-item").nth(i).click()
                page.wait_for_timeout(250)
            page.wait_for_load_state("networkidle")
            r.check("共用程式：每個分頁走一遍，同網域的檔案沒有一個載入失敗",
                    not broken, "; ".join(broken[:3]))
            # 驗的是 SW 的決定：程式走網路優先。快取優先的話會一直拿到預先快取的舊檔
            #（故意改壞成快取優先實測過，這條會失敗）。
            # 不能用「改了檔案、同一個分頁重新整理、看新程式有沒有跑」來驗：伺服器回
            # Cache-Control: max-age（GitHub Pages 是 600 秒），Chrome 重新整理時會直接從
            # 記憶體快取拿 app.js、根本不問 SW——用 CDP 確認過。所以同一個分頁在部署後
            # 10 分鐘內重新整理，可能還是上一版的程式（見 CLAUDE.md 第三節）。
            # cache:'no-store' 讓頁面這一端不碰 HTTP 快取，結果完全由 SW 決定
            app_js = tmp / "app" / "app.js"
            original = app_js.read_bytes()
            try:
                app_js.write_bytes(original + b"\nwindow.__appProbe = 'updated';\n")
                got = page.evaluate("fetch('../app/app.js', { cache: 'no-store' }).then(r => r.text())")
                r.check("共用程式：Service Worker 對 app.js 走網路優先（改了會拿到新版，不會卡在快取）",
                        "__appProbe" in got)
            finally:
                app_js.write_bytes(original)
            ctx.set_offline(True)
            page.reload()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1500)
            r.check("共用程式：離線重新整理，程式與樣式從快取載入、畫面照常",
                    page.locator(".nav-item").count() > 2
                    and page.locator("#main").inner_text().strip() != ""
                    # 樣式有載入：--fs-md 只定義在 app.css 裡
                    and page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--fs-md').trim()") != "",
                    f"{page.locator('.nav-item').count()} 個分頁")
            r.check("共用程式：圖示表有注入（圖示畫得出來）",
                    page.evaluate("!!document.getElementById('i-x') && document.body.firstElementChild.tagName === 'svg'"))
            r.check("共用程式：整段沒有 JS 例外", not app_err, "; ".join(app_err[:2]))
            ctx.close()

            # 16 ── 公積金 ─────────────────────────────────
            # 大家先各拿一筆錢放在一起，共同的花費從這裡付。使用者要的：記每個人給多少、錢花在哪。
            # 都用日圓（當地貨幣，旁邊附約多少台幣）、大家同步（進同步佇列）、跟記帳共用 0000 那道鎖
            fund_url = f"http://127.0.0.1:{port}/fund/"
            ctx = browser.new_context(viewport={"width": 375, "height": 812})
            page = ctx.new_page()
            said, fund_err = [], []
            page.on("dialog", lambda d: (said.append(d.message), d.dismiss()))
            page.on("pageerror", lambda e: fund_err.append(str(e)))
            page.goto(fund_url)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            r.check("公積金：分頁在「旅途工具」那一組",
                    page.locator("#nav-tools #tab-fund").count() == 1 and page.locator("#nav-prep #tab-fund").count() == 0)
            r.check("公積金：沒解鎖前只有密碼欄，標題寫公積金",
                    page.locator("#w-pass").count() == 1 and page.locator("#f-add-in").count() == 0
                    and "公積金" in page.locator("#main .subject").first.inner_text())
            unlock_wallet(page)
            r.check("公積金：輸入 0000 解開", page.locator("#f-add-in").count() == 1)
            page.locator("#tab-wallet").click()
            page.wait_for_timeout(300)
            r.check("公積金：跟記帳共用同一道鎖（解開公積金，記帳也開了）",
                    page.locator("#w-pass").count() == 0 and page.locator("#e-add").count() == 1)
            page.locator("#tab-fund").click()
            page.wait_for_timeout(300)

            page.locator("#f-add-in").click()
            page.wait_for_timeout(200)
            r.check("公積金：沒選人、沒填金額，錯誤寫在欄位底下、焦點到第一個",
                    page.locator("#f-who-err").is_visible() and page.locator("#f-give-err").is_visible()
                    and page.locator("#f-give").get_attribute("aria-invalid") == "true"
                    and page.evaluate("document.activeElement.name") == "fwho")
            page.locator("#f-add-out").click()
            page.wait_for_timeout(200)
            r.check("公積金：花費沒填項目、金額，錯誤也寫在欄位底下",
                    page.locator("#f-item-err").is_visible() and page.locator("#f-spend-err").is_visible()
                    and page.locator("#f-item").get_attribute("aria-invalid") == "true")

            for who, amt in [("A", "10000"), ("B", "10000")]:
                page.locator("#f-who label.chip", has_text=who).click()
                page.locator("#f-give").fill(amt)
                page.locator("#f-add-in").click()
                page.wait_for_timeout(300)
            r.check("公積金：記下來有提示", "B 給了" in page.locator("#toast").inner_text(), page.locator("#toast").inner_text())
            page.locator("#f-item").fill("加油")
            page.locator("#f-spend").fill("3200")
            page.locator("#f-add-out").click()
            page.wait_for_timeout(300)
            left = page.locator(".fund-left").inner_text()
            r.check("公積金：還剩＝收到－花掉，附約多少台幣",
                    "JPY 16,800" in left and "約 TWD 3,696" in left, left.replace("\n", " "))
            sums = page.locator(".fund-sum").inner_text()
            r.check("公積金：收到與花掉的合計", "JPY 20,000" in sums and "JPY 3,200" in sums, sums.replace("\n", " "))
            people = {x.split("\n")[0]: x.split("\n")[-1] for x in page.locator(".fund-p").all_inner_texts()}
            r.check("公積金：每個人給了多少（沒給的是 0）",
                    people == {"A": "JPY 10,000", "B": "JPY 10,000", "C": "JPY 0", "D": "JPY 0"}, str(people))
            first = page.locator("#main .exp-item").first.inner_text()
            r.check("公積金：明細新的在上面，收入／支出寫出來",
                    "加油" in first and "支出" in first and page.locator("#main .exp-item", has_text="收入").count() == 2, first)
            r.check("公積金：進同步佇列（type=fund），大家都看得到",
                    "fund" in page.evaluate("(JSON.parse(localStorage.getItem('trip:fundtest')||'{}').queue||[]).map(q => q.type)"))

            # 另一個表單打到一半的字，重畫之後還在（雲端有更新時也會整頁重畫）
            page.locator("#f-item").fill("停車費")
            page.locator("#f-who label.chip", has_text="C").click()
            page.locator("#f-give").fill("5000")
            page.locator("#f-add-in").click()
            page.wait_for_timeout(300)
            r.check("公積金：另一個表單打到一半的字，重畫後還在", page.locator("#f-item").input_value() == "停車費")
            page.locator("#f-spend").fill("800")
            page.locator("#f-add-out").scroll_into_view_if_needed()
            y0 = page.evaluate("scrollY")
            page.locator("#f-add-out").click()
            page.wait_for_timeout(400)
            y1 = page.evaluate("scrollY")
            r.check("公積金：新增後停在原地、不跳回最上面", y0 > 200 and abs(y1 - y0) < 5, f"{y0} → {y1}")
            row = page.locator("#main .exp-item", has_text="停車費")
            row.locator(".exp-del").click()
            page.wait_for_timeout(300)
            gone = page.locator("#main .exp-item", has_text="停車費").count() == 0
            page.locator("#toast .toast-act").click()
            page.wait_for_timeout(300)
            r.check("公積金：刪除後可以按「復原」拿回來",
                    gone and page.locator("#main .exp-item", has_text="停車費").count() == 1)
            page.reload()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            r.check("公積金：重新整理後紀錄還在、不會再鎖起來",
                    page.locator("#w-pass").count() == 0 and page.locator("#main .exp-item").count() == 5)
            page.locator("#f-item").fill("大餐")
            page.locator("#f-spend").fill("50000")
            page.locator("#f-add-out").click()
            page.wait_for_timeout(300)
            r.check("公積金：花超過時還剩是負數、標紅", page.locator(".fund-left b.is-neg").count() == 1
                    and "-" in page.locator(".fund-left b").inner_text(), page.locator(".fund-left b").inner_text())

            # UI 規則：真實行程加了公積金分頁之後第 12 段也會量到，這裡先量測試副本
            page.evaluate("document.querySelectorAll('#main details').forEach(d => d.open = true)")
            page.evaluate("window.scrollTo(0, 0)")   # 量的是版面，不是捲到一半疊在置頂日期列底下的樣子
            got = page.evaluate(UX_PROBE)
            bad = [f"{k}：{x}" for k in ["contrast", "target", "spacing", "alt", "heading", "emoji", "label"] for x in got[k]]
            if got["overflow"] > 0: bad.append(f"overflow：{got['overflow']}px")
            r.check("公積金：UI 規則（對比、觸控、間距、標題、標籤、不橫向捲動）", not bad, "；".join(bad[:4]))
            page.locator("#f-lock").click()
            page.wait_for_timeout(300)
            locked_ux = page.evaluate(UX_PROBE)
            page.locator("#tab-wallet").click()
            page.wait_for_timeout(300)
            r.check("公積金：上鎖之後公積金與記帳一起鎖", page.locator("#w-pass").count() == 1)
            r.check("公積金：密碼頁也過 UI 規則",
                    not any(locked_ux[k] for k in ["contrast", "target", "spacing", "label"]), str({k: locked_ux[k] for k in ["contrast", "target", "spacing", "label"]}))
            r.check("公積金：沒有 alert、沒有 JS 例外", not said and not fund_err, "; ".join((said + fund_err)[:2]))
            ctx.close()

            ctx = browser.new_context(viewport={"width": 375, "height": 812}, color_scheme="dark")
            page = ctx.new_page()
            page.goto(fund_url)
            page.wait_for_load_state("networkidle")
            unlock_wallet(page)
            page.locator("#f-who label.chip", has_text="A").click()
            page.locator("#f-give").fill("10000")
            page.locator("#f-add-in").click()
            page.wait_for_timeout(300)
            dark = page.evaluate(UX_PROBE)["contrast"] + page.evaluate(FLAT_PROBE)
            r.check("公積金：深色模式的對比、區塊分得開", not dark, "；".join(dark[:4]))
            ctx.close()

            # 17 ── 公積金的同步：存在雲端的另一份文件 ─────────────
            # 用假的 Firebase（FAKE_FB）讓兩支「手機」共用一份雲端，驗三件事：
            #   一、A 記一筆公積金，B 看得到
            #   二、還沒更新的舊版程式（不知道有公積金）整份覆寫記帳那份文件，公積金不會被洗掉
            #   三、只動了公積金的手機，不會拿本機（離線時沒收到別人的帳）的記帳去蓋雲端
            store, ver = {}, [0]

            def fb_set(path, data):
                ver[0] += 1
                store[path] = (ver[0], data)

            def fb_get(path):
                v = store.get(path)
                return {"v": v[0], "data": v[1]} if v else None

            def fake_ctx():
                c = browser.new_context(viewport={"width": 375, "height": 812}, service_workers="block")
                c.expose_function("__fbSet", fb_set)
                c.expose_function("__fbGet", fb_get)
                c.route("https://www.gstatic.com/firebasejs/**", lambda route: route.fulfill(
                    status=200, body=FAKE_FB[route.request.url.rsplit("/", 1)[1]],
                    headers={"content-type": "text/javascript", "access-control-allow-origin": "*"}))
                return c

            sync_url = f"http://127.0.0.1:{port}/sync/"
            STATE, FUND = "trips/synctest/shared/state", "trips/synctest/shared/fund"
            fund_items = lambda: json.loads(store[FUND][1])["items"] if FUND in store else []
            state_exp = lambda: json.loads(store[STATE][1])["expenses"] if STATE in store else []
            ca, cb = fake_ctx(), fake_ctx()
            pa, pb = ca.new_page(), cb.new_page()
            sync_err = []
            for pg in (pa, pb):
                pg.on("pageerror", lambda e: sync_err.append(str(e)))
                pg.goto(sync_url)
                pg.wait_for_load_state("domcontentloaded")
                pg.wait_for_timeout(800)
                unlock_wallet(pg)
            r.check("同步：假的雲端連上了（顯示已同步）", pa.locator("#sync").inner_text() == "已同步",
                    pa.locator("#sync").inner_text())
            pa.locator("#f-who label.chip", has_text="A").click()
            pa.locator("#f-give").fill("10000")
            pa.locator("#f-add-in").click()
            try: pb.wait_for_function("document.querySelector('.fund-left')?.innerText.includes('10,000')", timeout=5000)
            except Exception: pass
            r.check("同步：A 記的公積金，B 的畫面也出現", "10,000" in pb.locator(".fund-left").inner_text())
            r.check("同步：公積金存在另一份文件（shared/fund），記帳那份沒有它",
                    len(fund_items()) == 1 and "fund" not in json.loads(store.get(STATE, (0, "{}"))[1]))
            # 二：舊版程式整份覆寫 shared/state（只有記帳、留言、登記）
            fb_set(STATE, json.dumps({"expenses": [expense("A", 100, ["A", "B"])], "board": [], "owners": {}, "updatedAt": 1}))
            pb.wait_for_timeout(1000)
            r.check("同步：舊版程式覆寫記帳那份文件，公積金不受影響",
                    len(fund_items()) == 1 and "10,000" in pb.locator(".fund-left").inner_text())
            # 三：A 離線時記一筆公積金；B 這時記了一筆帳。A 上線後只寫公積金那份
            ca.set_offline(True)
            pa.locator("#f-item").fill("加油")
            pa.locator("#f-spend").fill("3000")
            pa.locator("#f-add-out").click()
            pa.wait_for_timeout(300)
            pb.locator("#tab-wallet").click()
            pb.wait_for_timeout(300)
            pb.locator("#e-item").fill("B 記的晚餐")
            pb.locator("#e-local").fill("2000")
            pb.locator("#e-add").click()
            pb.wait_for_timeout(600)
            before = len(state_exp())
            ca.set_offline(False)
            try: pa.wait_for_function("document.getElementById('sync').textContent === '已同步'", timeout=5000)
            except Exception: pass
            pa.wait_for_timeout(800)
            r.check("同步：A 上線後只寫公積金那份，B 剛記的帳沒有被蓋掉",
                    before == 2 and len(state_exp()) == 2 and len(fund_items()) == 2,
                    f"記帳 {before} → {len(state_exp())} 筆、公積金 {len(fund_items())} 筆")
            r.check("同步：A 上線後也收到 B 的帳",
                    len(pa.evaluate("JSON.parse(localStorage.getItem('trip:synctest')).expenses")) == 2)
            pb.locator("#tab-fund").click()
            try: pb.wait_for_function("document.querySelector('#main')?.innerText.includes('加油')", timeout=5000)
            except Exception: pass
            r.check("同步：B 也看到 A 離線時記的公積金", "加油" in pb.locator("#main").inner_text())
            r.check("同步：沒有 JS 例外", not sync_err, "; ".join(sync_err[:2]))
            ca.close()
            cb.close()

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
