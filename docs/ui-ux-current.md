# 福岡 2026 行程網站 —— UI／UX 現況

讀自 `fukuoka-2026/index.html`，狀態是 2026-10-25 做完第九節「已處理」那七項、
並把背景飄落的裝飾裝回去之後。
`<style>` 區段與 `builder/assets/site-template.html` **逐行相同**（870 行，`diff` 無差異），
所以這份文件同時描述模板；下面寫的每一條改動都要改兩處（CLAUDE.md 第一節）。

---

## 一、整站骨架

單一 HTML，三層：設計層 `<style>`（19–888）、資料層 `TRIP_DATA`（993–3247）、程式層（3248–4969）。
沒有外部 CSS／JS／字型，圖示是內嵌的 Phosphor regular SVG `<symbol>`。

```
.sync                連線狀態（position:fixed 左下角，z-index 200）
.app                 max-width 由斷點決定
  header             h1 標題 ・ 副標 ・ 目前天氣條 .wx-now ・ 更新時間 .wx-stamp
  nav.nav-bar        role=tablist，11 顆 .nav-item（sticky top:0，z-index 100）
  aside#daymap       當日路線地圖（只有 ≥1024px 出現）
  main#main          當天的卡片；切分頁時整段 innerHTML 重畫
  footer             #edit-bar（只有「行前」分頁顯示）＋ Open-Meteo 授權
#toast               提示訊息（第一次用到才建立，role=status）
#zoom                圖片放大 <dialog>（第一次用到才建立）
```

### 11 個分頁

| 分頁 | 日期 | 內容（block 型別） |
|---|---|---|
| 📝 行前 | — | `weather`（行程期間天氣）、`checklist` ×2 |
| ℹ️ 資訊 | — | `cards`（信用卡回饋額度）、`checklist`、`phrases`、`emergency` |
| 🛍️ 購物 | — | `shopping` |
| 💰 記帳 | — | `expenses` |
| D1 博多 | 11/14 | `flight`、`transit`、`options`、`stop`… |
| D2 小倉 | 11/15 | `stop`、`lodging`… |
| D3 由布院 | 11/16 | 同上 |
| D4 高千穗 | 11/17 | 同上 |
| D5 柳川 | 11/18 | 同上 |
| D6 天神 | 11/19 | `stop`、`options`、`lodging`… |
| D7 回程 | 11/20 | `stop`、`flight` |

四顆沒有日期的分頁（行前／資訊／購物／記帳）排在行程前面，375px 的第一屏剛好全部看得到，
不用橫向捲過七天才找得到。順序由 `builder/assets/parse.js` 的 `days: [...]` 決定。

分頁名稱開頭的 emoji 由 `iconText()` 在渲染時換成 Phosphor 圖示，資料裡不用改。
有日期的分頁在按鈕第二行多一條 `.nav-sub`（`11/14 六`），今天那一顆再補「・今天」。

全站共 61 個 block，用掉 `RENDERERS` 的全部 12 種型別：
`stop` 41、`lodging` 6、`checklist` 3、`options` 2、`flight` 2，其餘 6 種各 1。

---

## 二、設計 token

全部定義在 `:root`，元件裡不寫死 hex（唯一例外是 `.wx-grid div + div` 的 `rgba(0,0,0,.06)` 分隔線）。

### 語意色

| token | 值（maple 主題） | 用在哪 |
|---|---|---|
| `--c-bg` | `#FAF8F5` | 頁面底色 |
| `--c-surface` | `#FFFFFF` | 卡片底色 |
| `--c-text` | `#3B3733` | 正文 |
| `--c-text-dim` | `#6B635B` | 次要文字（在五個主題的底色上最低 5.1:1，**不要調淡**） |
| `--c-line` | `#E7E1D8` | 框線、分隔線、路線的灰段 |
| `--c-accent` | `#F5D2C8` | 淡色底：選取中的分頁、標籤、`.time`、「現在」卡片的底 |
| `--c-accent-soft` | `#FCF0EC` | 更淡的底：說明框、天氣格、預報卡 |
| `--c-on-accent` | `#A33A2A` | 疊在 `--c-accent` 上的字；也是小標題、連結文字的顏色 |
| `--c-accent-strong` | `= --c-on-accent` | 線、圓點、進度條（沒有文字疊上去的地方） |
| `--c-pop` | `#1F7A73` | 主題色的互補色，`.btn-theme` 用 |
| `--c-warn` / `--c-warn-text` / `--c-warn-soft` | `#E0A458` / `#9A6210` / `#FDF4E7` | 提醒框左邊線 / 提醒文字 / 提醒底色 |
| `--c-danger` / `--c-ok` | `#C9302C` / `#2A7F52` | 刪除、額度用完 / 同步正常 |
| `--c-naver` `--c-booking` `--c-map` | `#027A38` `#A33A2A` `#1A73E8` | 外部服務按鈕（固定，不隨主題） |
| `--c-on-solid` | `#FFFFFF` | 深色實心按鈕上的字 |

三者的關係要一起算：`--c-accent` 是底色、`--c-on-accent` 是疊在上面的字（≥ 4.5:1），
`--c-accent-strong` 是同一色的深版，給 4px 的線這種白底上看不見淡色的地方。

### 圖示語意色

`ic("名稱")` 查 `IC_CAT` 決定類別，CSS 給顏色。實心按鈕／選取中的分頁／標籤裡要 `color:inherit`。

| 類別 | 色 | 代表 |
|---|---|---|
| `.ic-food` | `#B23A1F` | 吃 |
| `.ic-move` | `#1D5FA8` | 行（車／走／搭車／騎車） |
| `.ic-stay` | `#7A4FA3` | 住 |
| `.ic-shop` | `#1F7A73` | 買 |
| `.ic-see` | `#2F7D32` | 看 |
| `.ic-warn` | `#9A6210` | 提醒 |
| `.ic-time` | `#6B635B` | 時間（不上色，跟文字走） |

### 五個主題

`<html data-theme>`。這趟是 `maple`。`:root` 的預設值等於 `snow`。

| 主題 | accent | on-accent | pop | bg | 裝飾 | 動畫 |
|---|---|---|---|---|---|---|
| `snow` | `#74B9FF` | `#0F4670` | `#B85C00` | `#F5F2ED` | 白色圓點 | drift（已關） |
| `sakura` | `#F4A6C0` | `#6E2338` | `#04855A` | `#FBF6F4` | 花瓣 | drift（已關） |
| **`maple`** | `#F5D2C8` | `#A33A2A` | `#1F7A73` | `#FAF8F5` | 楓葉 | drift（已關） |
| `ocean` | `#6FCBD5` | `#0C4A50` | `#C2410C` | `#F2F7F7` | 氣泡 | rise（已關） |
| `city` | `#A9A4E0` | `#322B70` | `#8A6410` | `#F5F4F8` | 天際線（底部 repeat-x） | 無 |
| `none` | — | — | — | — | 無 | 無 |

裝飾鋪在 `body::before`／`::after` 兩層（`opacity` 0.13–0.55），**目前是靜態的**——
`@keyframes driftA/driftB/riseA/riseB` 還留著但沒有選擇器在跑（純裝飾的動畫違反 motion-meaning）。

`theme` 填 `auto` 或留空時由 `resolveTheme()` 推：12／1／2 月 → snow，3–5 → sakura，
6–8 → ocean，9–11 → maple（南半球月份 +6）。`city` 只能手動指定。

### 字級

只有七階，不另外寫死 rem。根字級 `clamp(16px, 4.2vw, 18px)`，電腦版固定 17px。

| token | rem | 16px 根 | 用在哪 |
|---|---|---|---|
| `--fs-xs` | .75 | 12px | 標籤、時間膠囊、次要說明、頁尾 |
| `--fs-sm` | .85 | 13.6px | 按鈕、`.meta`、清單項目、分頁文字 |
| `--fs-md` | 1 | 16px | 正文、`.secret-val`、介紹內文 |
| `--fs-lg` | 1.15 | 18.4px | `.subject`（卡片標題）、天氣低溫 |
| `--fs-xl` | 1.35 | 21.6px | 航班時刻、天氣高溫、預報圖示 |
| `--fs-2xl` | 1.5 | 24px | `h1`（電腦版降一階到 xl） |
| `--fs-3xl` | 2.4 | 38.4px | 目前溫度 |

`.note-box` 刻意寫死 16px（不用 `--fs-md`，免得大螢幕長到 18px；也不能更小，
小於 16px iPhone 一點輸入框就放大畫面）。`.wx-emoji` 寫死 2.6rem——它是圖不是字。

行高：正文 1.6、`.note-box` 1.7、`.intro-body` 1.75。
數字欄（`.time`、`.stay`、`.exp-item .amt`、電話）用 `font-variant-numeric:tabular-nums`。

### 間距、圓角、陰影、層級

| | 值 |
|---|---|
| 間距 | `--sp-1..6` = 4 / 8 / 12 / 16 / 24 / 32px |
| 圓角 | `--r-card` 14px（卡片）、`--r-pill` 20px（膠囊）、10px（按鈕、說明框、選項）、8px / 6px（小標籤） |
| 陰影 | 兩階：`--shadow` = `0 2px 8px rgba(0,0,0,.06)`（卡片、天氣條）、`--shadow-pop` = `0 4px 14px rgba(0,0,0,.12)`（浮在內容之上的提示與連線狀態）。放大對話框不用陰影——它的 `::backdrop` 已經把底下壓暗 |
| z-index | 裝飾 0 → `.app` 1 → `.route-dot` 2 → `.nav-bar` 100 → `.sync` 200 → `.toast` 300 |
| 字體 | 系統字（`-apple-system` / `Segoe UI` / `Noto Sans TC`），不載外部字型；`.secret-val` 用等寬 |

---

## 三、版面與斷點

| 寬度 | 版面 |
|---|---|
| < 600px | 單欄，`--w-app` 480px，`main` 左右各 16px 內距 |
| ≥ 600px | 單欄加寬到 600px，內距移到 `body` |
| ≥ 1024px | 兩欄 grid `300px / 1fr`，上限 960px。左欄＝標題＋天氣＋直向分頁（sticky），右欄＝行程。根字級固定 17px，卡片內距加大到 24px |
| ≥ 1280px | 有嵌入金鑰時三欄 `300 / 720 / 360+`，上限 1500px，地圖 sticky 滿版高 |
| ≥ 1440px | 沒有地圖時兩欄上限 1100px（`.app.map-on` 兩個 class 的優先序較高，所以有地圖時仍是 1500px 三欄） |

`map-on`（網站有金鑰＋電腦寬度）與 `has-map`（這一頁有地圖）**刻意分開**：
否則切到沒有地圖的分頁時版面會從 1500px 縮回 960px 重新置中，左欄橫向跳將近 200px。

安全區：`body` 補 `env(safe-area-inset-top/bottom)`，`.nav-bar` 的 `top` 與 `.sync`、`.toast`
的 `bottom` 也各自算進去。`html` 的 `scroll-padding-top:80px` 讓取得焦點的元素不被 sticky 分頁列蓋住。

---

## 四、卡片元件（12 種 block）

全部是 `.card`（白底、14px 圓角、一階陰影、16px 內距、底部 12px 間距），左邊預留
`border-left:4px solid transparent` 給「現在進行到哪」用——預留才不會套用時整張卡片位移。

| 型別 | 長相 |
|---|---|
| `stop` | 時間膠囊 ・ 車程／停留 ・ `.subject`（吃／看／買圖示＋標題＋當地語） ・ 地點 ・ 營業時間／費用／電話各一列 ・ 說明框 ・ 私人資料 ・ 看介紹 ・ 按鈕列 |
| `lodging` | `.card.lodge`（`--c-accent-soft` 底＋1px accent 框） ・「住宿」標籤 ・ 標題 ・ 地址 ・ `.info` 虛線分隔的備註 ・ 私人資料 ・ 按鈕 |
| `flight` | 1px accent 框 ・ 航空公司＋班號／日期一列 ・ 三欄 grid：出發時刻／機場／航廈 — 飛機圖示（虛線穿過） — 抵達 |
| `transit` | 標題 ・ 路線 ・ 總耗時 ・ `.route` 垂直時間軸：`.route-dot`（起終點實心、經過站空心且字淡一階）、站間 4px 線由上一站畫到下一站、出口說明、`.note-box.warn` |
| `options` | 二選一：標題（結尾括號補充字較小）＋每個選項一塊 `.choice`（10px 框、A／B 標籤、名稱、備註、按鈕） |
| `checklist` | 標題＋`done/total` ・ 每組一個 `h2` ・ 每項 `<label class="chk">`（18px 原生勾選框，勾了文字變淡加刪除線）；有連結的項目把 `<a>` 放在 label **外面**，點連結不會順便打勾 |
| `cards` | 每張信用卡一塊：卡名＋回饋率膠囊 ・ 已刷金額輸入框 ・ 6px 進度條 ・ 剩餘額度（用完變紅） ・ 登錄狀態；可隱藏／還原 |
| `shopping` | 可收合的群組（`<details>`，標題＋件數＋刪群組） ・ 兩欄 grid，每格左上一顆 ✕、120px 封面圖（點了放大，沒圖時改成「加圖片」）。購物項目是勾選框＋品名（＋誰要的）＋「數量／備註」與「金額」兩個輸入框；優惠券群組只有品名與「開啟優惠券」，沒有勾選框也沒有輸入框。底部是「加一項」與「加一組」表單 |
| `weather` | `.wx-strip` 橫向捲動，每天一張 76px 寬的 `.wx-day`（日期／圖示／高／低／降雨機率） |
| `phrases` | `.ph` 逐列：當地語（可帶羅馬拼音）＋中文 |
| `emergency` | `.phrase` 兩端對齊：名稱在左、電話在右（不折行、可點撥號） |
| `expenses` | 四張卡片：我是誰（chip 單選）／新增花費（日期、付款人、分攤對象、項目、兩種幣別、分類）／花費明細（可刪）／結清建議＋兩顆匯出 CSV |

未知型別或渲染失敗都不會讓整頁中斷，會在原位畫一塊 `.err`（警示底＋左邊紅線）寫出是哪個型別、
或是哪一行錯誤訊息，後面的區塊照常渲染。

### 卡片上的共用零件

| 零件 | 說明 |
|---|---|
| `.time` | 淡底圓角膠囊，12px 粗體 |
| `.stay` / 車程 | 時間旁邊的灰字，`停留` 是扣掉 `交通(分)` 之後的數字 |
| `.subject` | 18.4px 粗體，前面一個語意上色的圖示。**是 `div` 不是標題元素** |
| `.meta` | 13.6px 灰字一列；電話是 `a.tel`，虛線底線，上下 12px padding ＋ −12px margin 撐出 44px 熱區 |
| `.note-box` | 淡底 10px 圓角、16px 字；`.warn` 版本換成警示底色＋左邊 3px 線 |
| `.intro` | 原生 `<details>`，預設收合，標題列「看介紹」44px 高。有照片時收合那一條鋪 0.25 透明度的照片（展開後收掉），照片放在文字下面、照原比例完整顯示，底下附作者與授權 |
| `.secret` | 鎖著時只有一顆 44px 的虛線框按鈕（寫明這張卡有什麼）；解開後標籤在上、值在下（等寬字、`user-select:all`），「上鎖」在右上角 |
| `.btn` | 44px 高、10px 圓角、深色實心（`--c-text`，白字 11.8:1）＝主要動作；服務色 `.btn-naver` `.btn-booking` `.btn-map` `.btn-theme`；`.btn-plain` 是次要動作的外框樣式（白底＋`--c-line` 框＋`--c-text` 字）；`.btn-route` 也是外框但帶主題色，專指「看路線」；`.btn-full` 是整列寬的送出鍵（順便把 `.btn` 的右邊距歸零，不然滿版時會被推出容器） |

---

## 五、非卡片的介面

| 元件 | 現況 |
|---|---|
| 目前天氣條 `.wx-now` | 白卡：日期＋當地時鐘一列；2.6rem 圖示 ・ 38.4px 溫度 ・ 天氣描述 ・ 右側高／低溫；底下一條淡底 `.wx-grid` 三格（體感、濕度、降雨），再下面是更新時間（距上次抓取超過 10 分鐘才出現） |
| 分頁列 `.nav-bar` | sticky，`color-mix` 92% 底色＋10px backdrop blur，橫向捲動（捲軸隱藏）。每顆 44px 高膠囊、13.6px 粗體；選取中換成 accent 底＋on-accent 字。切分頁時 `revealTab()` 會把選取的那一顆捲到中間（直接捲 `#nav` 自己，不用 `scrollIntoView`——電腦版是直向的，會把整頁一起捲走）；`navEdges()` 依捲動位置在還有分頁的那一邊鋪 `mask-image` 漸層。電腦版變成左欄直向排列，捲不動，所以兩個 class 都不會上 |
| 當日路線地圖 `#daymap` | 只有 ≥1024px。上面一排 34px 高的 `.dm-chip`（全選＋每個地點一顆，名字最寬 6.5em 後截斷、完整名稱在 `title` 與按鈕文字裡），勾選框決定畫不畫進路線、點名字只看那一點；下面是 Maps Embed API 的 iframe。沒有勾任何點時顯示 `.daymap-empty` |
| 連線狀態 `.sync` | 固定左下角膠囊，`--shadow-pop`，四種狀態上色：`ok` 綠、`offline` 褐、`local` 灰、`error` 紅 |
| `.toast` | 底部居中膠囊，深底淺字，`--shadow-pop`，`role="status"`，3.2 秒後淡出（只用 opacity 過場） |
| 放大檢視 `#zoom` | 原生 `<dialog>`，96vw／92vh 上限，`::backdrop` 0.72 黑；底部一顆滿版 44px「關閉」；自己加的圖多兩顆「換一張／拿掉」。點背景或按 Esc 也會關 |
| `#edit-bar` | 只在「行前」分頁顯示（其他分頁 `hidden`）：「編輯行程」外框按鈕＋「重新產生網站」實心按鈕 |
| 焦點樣式 | `.nav-item`、`.btn`、`input`、`select`、`button`、`a`、`summary` 共用 `outline:2px solid var(--c-accent); outline-offset:2px` |

---

## 六、互動與狀態

| 行為 | 怎麼做的 |
|---|---|
| 開啟網站 | 直接跳到「今天」那個分頁（不在行程期間就顯示第一個分頁），再捲到目前進行中的項目（`prefers-reduced-motion` 時不用平滑捲動） |
| 「現在進行到哪」 | 每 30 秒、以及從背景切回來時重算。只改 class 與一行字，不重畫整頁——否則展開到一半的介紹會被收起來。目前項目 `.is-now`（左邊 4px 線＋淡底＋「現在・還有 N 分（預計 HH:MM 離開）」），下一個 `.is-next`（灰線），已結束 `.is-past`（底色換成頁面色、浮起的陰影換成一條細框線，文字對比不變） |
| 切分頁 | 點 `.nav-item` → `renderDay()` 重畫 `#main`、更新 `aria-selected`、重畫地圖、捲回頂端 |
| 展開／收合 | 介紹與購物群組都用原生 `<details>`，沒有 JS；三角由 `summary::before` 畫，`[open]` 時旋轉 90°（.15s） |
| 勾選 | 勾選 key 是「清單:項目文字」（不含群組名稱，換分組不會掉勾；**改項目文字會掉勾**）。只存本機、不同步 |
| 事件 | 全部委派在 `#main`／`#nav`／`#daymap` 上，沒有 inline `onclick` |
| 卡片 ↔ 地圖 | 電腦版有地圖時，景點與住宿卡片 `cursor:pointer`，hover 加 1px 外圈、選中 2px 外圈（不動左邊框——那是「現在」在用的）；Enter／空白鍵也可以 |
| 私人資料 | 按鈕 → 同一張卡片裡長出密語輸入框（可勾「記住」）。解開一次，所有卡片一起顯示；錯了在 `aria-live="polite"` 的 `.vault-msg` 說「通關密語不對」 |
| 自己加的圖 | 縮到長邊 1600px 存進 IndexedDB，渲染後非同步掛上 blob 網址 |
| 更新提示 | Service Worker 換手時 toast「行程已更新，重新開啟即可看到最新版」 |

---

## 七、無障礙現況

### 已經做到的

- 觸控目標一律 ≥ 44×44px。外觀小的元素用「padding 撐大 ＋ 等量負 margin 抵掉」保持版面不變：電話、照片出處、頁尾連結、`.secret-lock`；`.shop-x` 的 28px 圓圈用 `::after` 撐熱區（測試特別排除它本體）。
- 焦點外框 2px＋2px offset，六類互動元素共用一條規則。
- 不用 emoji 當圖示（只有使用者自己寫的網站標題例外，測試排除 `#app-title`）；圖示都是 `aria-hidden` 的 SVG，旁邊一定有文字。
- 圖示按語意上色，但顏色不是唯一線索（旁邊都有文字）。
- 輸入框都有看得見的 `<label>`（`.f-lbl` 直接包住輸入框，或 `for` 指到 id）；地圖的勾選框用 `aria-labelledby` 指到旁邊的地點按鈕。提示文字只當範例。
- 輸入框 16px 字、44px 高（小於 16px iPhone 會放大畫面）；數字欄用 `inputmode="numeric"`。
- `prefers-reduced-motion` 時裝飾圖層與偽元素動畫都關掉（`*` 不會套到 `::before`／`::after`，所以另外寫）。
- 自己設了 `display` 的元素額外補 `選擇器[hidden]{display:none}`（`.now-line`、`#edit-bar`）。
- 圖片有替代文字（介紹照片用景點名稱、購物縮圖用品名）。
- `viewport` 沒有禁止縮放。

### `tests/offline_test.py` 第 12 段在守的（改壞會直接失敗）

375px 下逐一打開 11 個分頁（並展開所有 `<details>`、按開密語框）量：
文字對比 ≥ 4.5:1（大字 3:1）、觸控目標 ≥ 44×44px、目標間距 ≥ 8px、圖片有 alt、
標題層級不跳級、介面沒有 emoji、輸入框有看得見的標籤、取得焦點的密語框沒被分頁列蓋住、
375px 不會橫向捲動。另外兩個情境：`reduced-motion` 下沒有動畫在跑、1400px 下輸入框也都有標籤。

### 缺口（測試目前量不到的）

| 缺口 | 說明 |
|---|---|
| 分頁沒有完整的 tab 語意 | `nav` 是 `role="tablist"`、按鈕是 `role="tab"` 且有 `aria-selected`，但沒有 `aria-controls`，`<main>` 也沒有 `role="tabpanel"`；`#nav` 只綁 `click`，**方向鍵不能切分頁**（tablist 的慣例是左右鍵移動） |
| 整頁只有兩個標題元素 | 一個 `h1`（網站標題）、一個 `h2`（清單群組）。卡片標題 `.subject` 是 `div`，所以讀螢幕的人沒辦法用標題在一天的行程裡跳 |
| 放大檢視的圖沒有替代文字 | `<img alt="">`，但它是那個對話框的主要內容（縮圖有 alt）；對話框本身也沒有 `aria-label` |
| 沒有跳到主內容的連結 | 頁首加 11 顆分頁按鈕之後才到行程，用鍵盤每切一次都要重新 Tab 過去 |
| 記帳的群組標籤沒有綁定 | 「誰付錢」「分攤對象」是沒有 `for` 的 `<label>`，等於一段純文字（`fieldset`／`legend` 或 `role="group"` 才會被讀出來）。測試過得去是因為每顆 chip 自己被 `<label>` 包住 |
| 沒有深色模式 | `<meta name="color-scheme" content="light">`，CSS 沒有 `prefers-color-scheme` 分支。五個主題都是淺色 |

---

## 八、動態效果

| 還在跑的 | 哪裡 |
|---|---|
| 背景裝飾飄落／上升 | `body::before` 跑 `--anim-a` 24s、`body::after` 跑 `--anim-b` 17s，兩層速度與路徑都不同，避免整片同步平移。maple／snow／sakura 是 `driftA`／`driftB`（由上往下），ocean 是 `riseA`／`riseB`（由下往上），city 與 none 是 `none`。**這是 motion-meaning 的例外，使用者指定要留** |
| 交通卡的流動光點 | `.route-step::after`／`.route-link .bar::after`，1.8s linear 無限循環，表示方向 |
| 展開三角旋轉 | `summary::before` `transform .15s` |
| 分頁選取、信用卡進度條、toast | `background/color .15s`、`width .2s`、`opacity .25s` |
| 捲動 | 切分頁回頂端、第一次捲到目前項目（`behavior:"smooth"`，reduced-motion 時改 `auto`） |

| 已經拿掉的 | 原因 |
|---|---|
| 起終點脈動光暈 | 純裝飾，不表達因果（motion-meaning）。`c024925` 拿掉 `.route-dot.on::after` 的 `animation`，剩下的 `@keyframes pulse` 在 2026-10-25 刪掉 |

背景飄落曾經也在這張表裡：`c024925` 照 motion-meaning 把它關掉，2026-10-25 又刪了留下來的
`@keyframes`，使用者發現楓葉不會動，當天就裝回去了（見上表）。
在 `prefers-reduced-motion` 下兩層裝飾是 `animation:none; opacity:0`，所以「減少動態時沒有動畫在跑」那條測試仍然會過。

---

## 九、可以調整的點

照影響面排，不含上面那張無障礙缺口表。編號沿用第一版，做完的移到上面那張表。

### 已處理（2026-10-25）

| # | 原本的問題 | 改成什麼 |
|---|---|---|
| 1 | `.btn-plain` 沒有對應的 CSS 規則，記帳分頁三顆按鈕長得一模一樣 | `.btn-plain` 定義成次要動作的外框樣式（`--c-surface` 底、1px `--c-line` 框、`--c-text` 字 11.8:1；`padding:9px` 把邊框的厚度讓出來，高度維持 44px）。「新增花費」回到深色實心的主要動作，兩顆匯出 CSV 都降為 `.btn-plain`——一個畫面只剩一顆主要動作 |
| 2 | 記帳分頁有 4 處 inline `style` | 收進兩個 class：`.btn-full`（整列寬的送出鍵）與 `.meta-foot`（卡片最後的補充說明，`margin-top:var(--sp-2)`）。全檔剩 3 處 inline style，都是非寫在元素上不可的動態值：圖示 sprite 的 `display:none`、介紹照片的 `--photo`、信用卡進度條的 `width:${pct}%` |
| 3 | `@keyframes pulse` 與四組 `drift`／`rise` 沒人用 | 連同主題裡的 `--anim-a`／`--anim-b`、以及 reduced-motion 裡已經是空操作的 `animation:none` 一起刪掉（兩個檔案各 −19 行）。**注意：背景飄落的部分當天又裝回去了**，見第八節 |
| 4 | 陰影只有一階，卡片與浮在最上層的東西共用 | 分兩階：`--shadow` 留給卡片與天氣條，新增 `--shadow-pop`（`0 4px 14px rgba(0,0,0,.12)`）給 `.toast` 與 `.sync` 這些浮在內容之上的。放大對話框刻意不加——它的 `::backdrop` 把底下壓暗 0.72，再加影子也看不出來 |
| 5 | 分頁列捲到後面時看不出還有分頁，選取中的那顆也不會自動捲進視野 | 加 `revealTab()`：切分頁時把選取的那顆捲到中間（開啟網站會直接跳到今天，以前那顆常常留在畫面外，看起來像停在第一個分頁）。加 `navEdges()`＋`mask-image`：哪一邊還有捲不到的分頁就在那一邊鋪漸層，捲到底就收起來 |
| 6 | `.card.is-past` 的 `opacity:.62` 把正文對比壓到 3.82:1、次要文字壓到 2.67:1 | 改成只降容器的層次：`background:var(--c-bg)` ＋ `box-shadow:0 0 0 1px var(--c-line)`（住宿卡本來就有自己的 1px 邊框，所以改成調淡邊框、拿掉陰影）。文字回到頁面底色上的 11.1:1／5.6:1，最深的主題底色 `#F5F2ED` 也還有 10.6:1／5.3:1 |
| 7 | 「購物」「記帳」排在 11 顆分頁的最尾端 | 四顆沒有日期的分頁改成擺在一起（行前／資訊／購物／記帳），放在 D1–D7 前面，375px 的第一屏剛好四顆全部看得到。改在 `builder/assets/parse.js` 的 `days: [...]`，已產出的網站則重排 `TRIP_DATA.days`（11 個分頁、61 個區塊、密文與照片都原封不動）|

留一個尾巴：`.btn-plain` 的邊框 `--c-line` 對白底只有 **1.3:1**，低於 WCAG 1.4.11 對介面邊界的 3:1。
全站的 `.input`、`.chip`、`#edit-bar a`、`.cc-x` 都是同一條線色，單獨把這顆加深會跟同一張卡片裡的
輸入框不一致，所以先沿用現況；真要處理是整組 `--c-line` 一起調。

### 還沒做

第一版列的七項都做完了。剩下的是第七節那張**無障礙缺口表**——分頁的 tab 語意與方向鍵、
卡片標題不是標題元素、放大檢視的圖沒有替代文字、沒有跳到主內容的連結、記帳的群組標籤沒有綁定、
沒有深色模式。那六條測試目前都量不到，要動要另外排。
