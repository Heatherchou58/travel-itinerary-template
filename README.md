# 旅遊行程網站

一趟旅行 = 一個 HTML 檔。手機上開網址即可使用，可加入主畫面、離線瀏覽，同行者之間即時同步。

## 線上網址

| | 網址 |
|---|---|
| 九州福岡 2026 | https://heatherchou58.github.io/travel-itinerary-template/fukuoka-2026/ |
| 網站產生器 | https://heatherchou58.github.io/travel-itinerary-template/builder/ |

部署方式是 GitHub Pages：推上 `main` 分支後 1～2 分鐘自動發佈。

## 資料夾

```
builder/              Excel／Google 試算表 → 網站的產生器（純前端，檔案不上傳）
├─ index.html         產生器介面
├─ travel-template.xlsx   給使用者下載的 Excel 模板
└─ assets/
   ├─ parse.js        資料 → TRIP_DATA
   ├─ sheets.js       Google 試算表 → 二維陣列
   ├─ localdata.js    各國插座、緊急電話、季節打包清單、通用片語
   ├─ deploy.js       用 GitHub API 推檔（一次 commit）
   ├─ site-template.html  網站模板，注入 TRIP_DATA 用
   └─ sw.js           原樣打包進輸出

fukuoka-2026/         已產出的行程網站
├─ index.html         行程本體（唯一需要編輯的檔案）
├─ sw.js              離線快取
├─ manifest.json      App 名稱與圖示
└─ icons/             各尺寸圖示

.nojekyll             GitHub Pages 用，停用 Jekyll 處理
```

## 建立新的一趟旅行

1. 開[產生器](https://heatherchou58.github.io/travel-itinerary-template/builder/)，下載 Excel 模板
2. 填好行程，拖回產生器頁面（或改用 Google 試算表，貼網址線上讀取）
3. 下載 zip，或直接用產生器的「一鍵部署」推上 GitHub
4. 部署後到 Firebase Console → Authentication → Settings → 授權網域，確認網域在清單裡

第 4 步漏掉的症狀很迷惑：本機測一切正常，一上線同步就壞。

## 架構

`index.html` 內部固定切成三層：

```
<style>        設計層  ── 幾乎不改
TRIP_DATA      資料層  ── 換旅程時「只改這裡」
渲染 + 同步 JS  邏輯層  ── 幾乎不改
```

判斷是否符合規範的標準：把 `TRIP_DATA` 換成另一趟旅行的資料後，網站要能完整運作，不需要動 HTML 或 CSS 任何一行。

想新增顯示形式時，做法是新增一個 block 型別（資料 + 對應的 renderer + CSS），不是在 HTML 裡寫死一段行程。

## 外部服務

| 服務 | 用途 | 需要金鑰 |
|---|---|---|
| [Open-Meteo](https://open-meteo.com/) | 天氣預報（未來 16 天） | 不需要 |
| Firebase Firestore | 打包清單與記帳的多人同步 | 前端可公開，安全性靠 Security Rules |
| Google Sheets API v4 | 讀取線上試算表 | 需要，限制只勾 Sheets API |
| MyMemory | 自動翻譯當地語 | 不需要 |

Firebase 沒設定時會自動退回純 localStorage 的「單機模式」，網站照常可用。
