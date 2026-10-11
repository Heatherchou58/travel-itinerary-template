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

## 七、改 Excel 模板

用 **ExcelJS**，不要用 SheetJS（會把星際平面的 emoji 解碼錯誤）。

插入欄位前先確認公式引用的欄位都在插入點**左側**。時間欄的公式只引用 `$A`/`$C`/`$D`。改完要逐條比對公式字串確認沒被動到，並檢查其他工作表未受影響。

這台沒有 LibreOffice，所以無法做公式重算驗證。若改動了公式本身（不只是插欄），要請使用者實際開 Excel 確認。

## 八、這個 repo 是公開的

行程內容（飯店、日期、同行者姓氏）任何人都查得到。所有頁面都有 `<meta name="robots" content="noindex, nofollow">`——**不要移除**。

專案站無法用 `robots.txt`，爬蟲只讀網域根目錄那一份。

Firebase 的 config 寫在前端是設計如此，安全性靠 Firestore Security Rules。規則限定只有登入（匿名）且 `tripId` 在白名單內才能讀寫。
