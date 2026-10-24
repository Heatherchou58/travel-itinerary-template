# 旅遊行程網站

一趟旅行 = 一個 HTML 檔。正式站在 GitHub Pages，`builder/` 是把 Excel 轉成網站的純前端產生器。

整體架構與設計規範見使用者的 `travel-site-builder` skill。這份檔案只記**實際踩過、光讀程式碼看不出來**的坑。

---

## 一、改一個地方要改兩處

網站的程式碼同時存在兩個檔案：

| 檔案 | 身分 |
|---|---|
| `builder/assets/site-template.html` | 模板，之後產生的新網站用這份 |
| `fukuoka-2026/index.html` | 已經產出的網站 |

**只改其中一份，另一份就會退回舊行為。** 改模板但不改已產出的網站，使用者現在看不到；改已產出的網站但不改模板，下次用 builder 重新產生就全部消失。

CSS、renderer、同步邏輯都適用。只有 `TRIP_DATA` 是該趟專屬的，不用同步。

## 二、驗證部署結果的正確方法

**`curl` 和頁面內的 `fetch()` 都會騙你。**

| 方法 | 問題 |
|---|---|
| `curl` 網址 | 繞過 Service Worker，看到的是伺服器版本，不是使用者拿到的版本 |
| 頁面內 `fetch('index.html')` | 子資源請求走 `cacheFirst`，拿到快取的舊版 |
| 頁面內 `fetch('manifest.json')` | 同上 |

正確做法：**重新整理頁面直接看畫面**，或在 console 檢查 `document.documentElement.outerHTML`。

這個坑在同一輪裡踩了兩次，兩次都差點做出錯誤結論。

## 三、Service Worker 的快取分界

| 資源 | 策略 | 改了要升 `VERSION` 嗎 |
|---|---|---|
| `index.html` | 網路優先 | 不用 |
| `manifest.json`、`icons/` | **快取優先** | **要** |
| `sw.js` 本身 | 不在 PRECACHE，瀏覽器自行偵測 | 不用 |

改了 `manifest.json` 卻沒升版，已經開過網站的人**永遠拿不到新版**，重新部署也救不回來。

另外：GitHub Pages 對 HTML 回 `Cache-Control: max-age=600`，所以 `networkFirst` 用的是 `fetch(req, { cache: "no-cache" })` 來繞過瀏覽器自己的 HTTP 快取。**不要改回 `fetch(req)`**，那會讓部署後 10 分鐘內拿到的還是舊檔。

## 四、金額一律整數

`shares(e)` 是每人分攤的**唯一來源**，`settle()` 與兩個 CSV 匯出都必須走它。

浮點數除法（`e.home / e.split.length`）各處再各自四捨五入，加總會湊不回來——1000 三人均分會變成「A 應收 667、B 和 C 各應付 333」，差 1 元。

## 五、同步的範圍

| 資料 | 同步嗎 |
|---|---|
| 記帳（`expenses`） | 會 |
| 打包清單（`checks`） | **不會，只存本機** |

每個人的打包進度本來就不一樣。`flush()` 不要把 `checks` 寫上去，`onSnapshot` 也不要用遠端的值覆蓋本機的。

## 六、測試

```bash
python tests/offline_test.py
```

寫測試時**不要用 `page.content()` 比對字串**。這個網站是單一 HTML、JS 內嵌，原始碼裡本來就含有 `TRIP_DATA` 和所有樣板字串——查 `"CI51"` 就算 renderer 根本沒畫出來也會通過。一律讀 `#main` 的 `innerText`。

判斷 `<details>` 內容是否可見也不要用 `getBoundingClientRect()`，收合狀態下它仍會回報高度。用 `checkVisibility()` 或 Playwright 的 `is_visible()`。

**自己設了 `display` 的元素，用 `hidden` 屬性藏不掉。** 瀏覽器預設的 `[hidden]{display:none}` 優先序輸給任何自訂的 `display`，所以要補一條 `選擇器[hidden]{ display:none }`。

這個坑踩過兩次：`.now-line{display:inline-flex}`（「現在進行到哪」的標記藏不起來）、`#edit-bar{display:flex}`（編輯入口在每個分頁都出現）。兩次都是看截圖才發現——測試若只查 `count()` 會過，因為元素確實在 DOM 裡、`hidden` 屬性也確實設上去了。查可見性要用 `is_visible()`。

## 七、改 Excel 模板

用 **ExcelJS**，不要用 SheetJS（會把星際平面的 emoji 解碼錯誤）。

插入欄位前先確認公式引用的欄位都在插入點**左側**。時間欄的公式只引用 `$A`/`$C`/`$D`。改完要逐條比對公式字串確認沒被動到，並檢查其他工作表未受影響。

這台沒有 LibreOffice，所以無法做公式重算驗證。若改動了公式本身（不只是插欄），要請使用者實際開 Excel 確認。

## 八、這個 repo 是公開的

行程內容（飯店、日期、同行者姓氏）任何人都查得到。所有頁面都有 `<meta name="robots" content="noindex, nofollow">`——**不要移除**。

專案站無法用 `robots.txt`，爬蟲只讀網域根目錄那一份。

Firebase 的 config 寫在前端是設計如此，安全性靠 Firestore Security Rules。規則限定只有登入（匿名）且 `tripId` 在白名單內才能讀寫。

## 九、卡片上的私人資料

門鎖密碼、訂單編號只能以**密文**進 TRIP_DATA：整包密文在 `TRIP_DATA.vault`，卡片上只帶 `secret` 鍵與按鈕提示 `secretHint`。明文在 `<行程>/<行程>-vault.json`（已列入 `.gitignore`），每一段用 `day` ＋ `card`（卡片標題）對到一張卡片。

```bash
node tools/encrypt-vault.mjs fukuoka-2026
```

| 坑 | 說明 |
|---|---|
| 重新產生行程區塊後，卡片上的私人資料會消失 | builder 重新產生網站、或任何重寫行程區塊的步驟，都會把 `secret` 鍵洗掉。產生完要再跑一次上面的指令。明文刻意不放進 Excel——Excel 會傳上雲端分享 |
| 卡片改了標題，私人資料就對不上 | 工具用標題比對卡片，對不到會直接報錯；改標題時要一起改明文檔的 `card` |
| 通關密語的正規化規則寫在兩個地方 | `normPass` 在 `tools/encrypt-vault.mjs` 與網站程式各一份（大小寫、空白、連字號不計較），只改一邊就解不開 |
| 前端只「檢查」密碼擋不住任何人 | repo 是公開的，原始碼誰都讀得到。一定要用密語「加密」，而且密語要夠長——密文永久留在 git 紀錄裡，可以被離線慢慢猜 |

工具每次都會把密文解回來比對，並掃描 index.html 有沒有明文外洩；Excel 與其他檔案要自己注意。

## 十、介紹照片

景點的「看介紹」可以帶一張照片：收合時那一條鋪淡淡的照片，展開後最上面是清楚的照片、底下附作者與授權。清單在 `<行程>/intro-photos.json`，壓縮後的檔案在 `<行程>/img/intro/`。

```bash
node tools/intro-photos.mjs fukuoka-2026                  # 只把照片掛回卡片
node tools/intro-photos.mjs fukuoka-2026 --src <原圖資料夾>  # 先重新壓縮再掛
```

| 坑 | 說明 |
|---|---|
| 重新產生行程區塊後，照片會消失 | 跟私人資料的 `secret` 一樣，`photo` 欄位不在 Excel 裡，重新產生完要再跑一次（不加 `--src`） |
| 只能用 Wikimedia Commons 的自由授權照片 | repo 與網站都公開，官網或搜尋引擎找到的照片有版權。工具只接受 CC0／CC BY／CC BY-SA／公有領域 |
| 作者與授權一定要顯示 | CC BY／BY-SA 的條件。渲染程式把出處印在照片底下，不能拿掉，也不能只存在 TRIP_DATA 不顯示 |
| 換照片不用升 `sw.js` 的 `VERSION` | 照片跟優惠券一樣放在 `trip-img` 快取，由網頁開啟時預先抓，不在 PRECACHE 裡 |
| 只放景點 | 餐廳找不到店本身的自由授權照片，放別家的照片會誤導，所以不放 |

## 十一、停留(分) 是總時間，不是停留

`停留(分)` 裝的是「這一列總共佔掉多久」，**開車或走過去的時間也算在裡面**——時間欄的公式（`上一列時間 ＋ 上一列停留`）與 `parse.js` 的時間推算都靠它往下推。

真正花在路上的另外記在 `交通(分)`（`b.travel`），網站顯示時自己扣：停留 230、交通 80 → 卡片顯示「🚗 車程 1 小時 20 分」「⏳ 停留 2 小時 30 分」。

| 坑 | 說明 |
|---|---|
| 不要把 `停留(分)` 改成「純停留」 | 時間公式要跟著改成加兩欄，而這台沒有 LibreOffice 驗不了公式重算（第七節） |
| `交通(分)` 只拿來顯示 | 不參與時間推算，否則 Excel 與網站會推出不同的時間 |
| 改 `mode` 要連「從上一站」按鈕一起改 | 按鈕網址的 `travelmode=` 與標籤圖示是 builder 產生時寫死在 TRIP_DATA 裡的，光改 `mode` 修不到它 |

說明欄該寫什麼、不該寫什麼，規則寫在 Excel 模板「說明」工作表最下方的「說明／備註 的寫法」。那裡是使用者會看的地方，不要在這裡重抄一份。
