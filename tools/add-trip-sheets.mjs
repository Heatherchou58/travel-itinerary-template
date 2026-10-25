/* ══════════════════════════════════════════════════
   替 Excel 模板補上「購物清單」與「信用卡」兩張工作表，
   並替「打包清單」加一欄「分頁」（空白＝行前，填「資訊」就放到資訊分頁）。

   用法：node tools/add-trip-sheets.mjs [模板路徑]

   可以重跑：已經有的工作表與欄位會原樣跳過。
   改完會逐張比對其他工作表的儲存格值，確認沒被動到。
   ══════════════════════════════════════════════════ */
import ExcelJS from "exceljs";

const FILE = process.argv[2] || "builder/travel-template.xlsx";

/* 樣式一律從「打包清單」抄，不要自己寫色碼——
   模板的配色之後若調整，這支腳本不會跟著走鐘。 */
const STYLE_FROM = "打包清單";

const SHOP = {
  name: "購物清單",
  desc: "想買的東西與優惠券。「圖片」填 img/ 底下的檔名，產生的網站上點圖就會放大給店員看，"
      + "離線也打得開；要現場生成的券改填「連結」，會變成一顆跳轉按鈕。"
      + "「類型」填「券」的那一組不會有數量與金額欄位——券是用掉的，不是買到的；"
      + "整組看同一個值，填在哪一列都算。整張留空就不會有購物分頁。",
  headers: ["群組", "類型", "項目", "誰要的", "圖片", "連結"],
  widths: [18, 10, 26, 14, 28, 52],
  /* 填入的欄位要上奶油色底，跟其他工作表一致 */
  fillFrom: [1, 1, 1, 1, 1, 1],
  rows: [
    ["優惠券", "券", "鶴羽藥妝", "", "img/tsuruha.jpg", ""],
    ["優惠券", "券", "唐吉訶德 Donki", "", "", "https://japanportal.donki-global.com/coupon/cp001_zhtw.html"],
    ["藥妝店", "", "資生堂吸油面紙", "媽媽", "", ""],
  ],
};

const CARD = {
  name: "信用卡",
  desc: "刷卡攻略。重點不是「有哪些卡」，而是「這張還能刷多少才到上限」——"
      + "「加碼額度」填回饋上限 ÷ 加碼%（例如每月上限 500 點、加碼 8% → 6250），留空代表沒有上限、網站就不畫進度條。"
      + "整張留空就不會有這張卡片。",
  headers: ["卡別", "回饋%", "適用地區", "活動到期日", "加碼額度", "需要登錄", "查證日期", "備註"],
  widths: [24, 10, 34, 14, 12, 12, 14, 60],
  fillFrom: [1, 1, 1, 1, 1, 1, 1, 1],
  rows: [
    ["中國信託 uniopen 卡", 11, "日本、韓國、泰國、新加坡、美洲", "2026-12-31", 6250, "否", "2026-10-23",
     "海外實體最高 11% OPENPOINT：一般 2% ＋ 實體 1% ＋ 加碼 8%。\n8% 加碼每月上限 500 點，所以每月海外實體刷到 NT$6,250 就拿滿。"],
    ["台新 Richart 卡", 3.3, "海外實體", "2027-03-31", "", "是", "2026-10-23",
     "切換「玩旅刷」模式，無上限。主力無腦刷。"],
  ],
};

/* 「說明」工作表的「各工作表用途」表要跟著補兩列 */
const DOC_ROWS = [
  ["購物清單", "想買的東西與優惠券", "選填"],
  ["信用卡",   "刷卡回饋與額度追蹤",  "選填"],
];
const DOC_ANCHOR = "緊急資訊";   // 插在這一列後面

const wb = new ExcelJS.Workbook();
await wb.xlsx.readFile(FILE);

/* ── 先拍一份快照，最後比對有沒有誤傷 ─────────── */
const snap = () => {
  const out = {};
  for (const ws of wb.worksheets){
    const cells = [];
    for (let r = 1; r <= ws.rowCount; r++)
      for (let c = 1; c <= ws.columnCount; c++){
        const v = ws.getCell(r, c).value;
        if (v !== null && v !== undefined) cells.push(`${r},${c}=${JSON.stringify(v)}`);
      }
    out[ws.name] = cells;
  }
  return out;
};
const before = snap();

/* 公式一旦存在就不能亂插欄，先確認 */
for (const ws of wb.worksheets)
  for (let r = 1; r <= ws.rowCount; r++)
    for (let c = 1; c <= ws.columnCount; c++){
      const v = ws.getCell(r, c).value;
      if (v && typeof v === "object" && v.formula && ws.name === STYLE_FROM){
        console.error(`「${STYLE_FROM}」有公式（${r},${c}），加欄前要先人工確認引用範圍`);
        process.exit(1);
      }
    }

const src = wb.getWorksheet(STYLE_FROM);
if (!src){ console.error(`找不到「${STYLE_FROM}」`); process.exit(1); }
const S = {
  title:  src.getCell(1, 1).style,
  desc:   src.getCell(2, 1).style,
  header: src.getCell(4, 1).style,
  cell:   src.getCell(5, 1).style,   // 一般資料格
  fill:   src.getCell(5, 3).style,   // 要填寫的格（奶油底）
};

let changed = 0;
/* 刻意重建的工作表，最後比對時不當成誤傷 */
const rebuilt = new Set();

/* ── ① 打包清單加「分頁」欄 ─────────────────── */
const packHeaders = [];
for (let c = 1; c <= src.columnCount; c++) packHeaders.push(String(src.getCell(4, c).value ?? ""));
if (!packHeaders.includes("分頁")){
  const c = src.columnCount + 1;
  src.getCell(4, c).value = "分頁";
  src.getCell(4, c).style = S.header;
  for (let r = 5; r <= src.rowCount; r++) src.getCell(r, c).style = S.cell;
  src.getColumn(c).width = 12;
  console.log(`✓ 「打包清單」加上「分頁」欄（第 ${c} 欄）`);
  changed++;
} else {
  console.log("－ 「打包清單」已經有「分頁」欄");
}

/* ── ② 兩張新工作表 ─────────────────────── */
for (const spec of [SHOP, CARD]){
  const old = wb.getWorksheet(spec.name);
  if (old){
    /* 標頭對得上就不動。對不上代表欄位改過了——這兩張表是這支腳本自己
       建的、只有範例資料，直接重建比插欄安全。 */
    const have = [];
    for (let c = 1; c <= old.columnCount; c++) have.push(String(old.getCell(4, c).value ?? ""));
    if (have.join("|") === spec.headers.join("|")){
      console.log(`－ 「${spec.name}」已存在且欄位相同`);
      continue;
    }
    console.log(`  「${spec.name}」欄位變了：${have.join("／")} → ${spec.headers.join("／")}，重建`);
    wb.removeWorksheet(old.id);
    rebuilt.add(spec.name);
  }
  const ws = wb.addWorksheet(spec.name);
  ws.getRow(1).height = src.getRow(1).height;
  ws.getRow(2).height = src.getRow(2).height;
  ws.getRow(4).height = src.getRow(4).height;

  ws.getCell(1, 1).value = spec.name;  ws.getCell(1, 1).style = S.title;
  ws.getCell(2, 1).value = spec.desc;  ws.getCell(2, 1).style = S.desc;

  spec.headers.forEach((h, i) => {
    ws.getCell(4, i + 1).value = h;
    ws.getCell(4, i + 1).style = S.header;
    ws.getColumn(i + 1).width = spec.widths[i];
  });

  /* 範例列 + 一段空白列，兩者都先鋪好框線與底色，使用者直接往下填 */
  const BLANK = 24;
  for (let r = 5; r < 5 + spec.rows.length + BLANK; r++){
    ws.getRow(r).height = src.getRow(5).height;
    const data = spec.rows[r - 5];
    spec.headers.forEach((_, i) => {
      const cell = ws.getCell(r, i + 1);
      cell.style = spec.fillFrom[i] ? S.fill : S.cell;
      if (data && data[i] !== "" && data[i] !== undefined) cell.value = data[i];
    });
  }
  ws.views = JSON.parse(JSON.stringify(src.views));
  console.log(`✓ 新增「${spec.name}」（${spec.headers.length} 欄，含 ${spec.rows.length} 列範例）`);
  changed++;
}

/* ── ③ 說明：各工作表用途補兩列 ───────────── */
const doc = wb.getWorksheet("說明");
if (doc){
  let anchor = -1,已有 = false;
  for (let r = 1; r <= doc.rowCount; r++){
    const v = String(doc.getCell(r, 2).value ?? "");
    if (v === DOC_ANCHOR) anchor = r;
    if (v === "購物清單") 已有 = true;
  }
  if (已有){
    console.log("－ 「說明」已經列出新工作表");
  } else if (anchor < 0){
    console.error(`「說明」裡找不到「${DOC_ANCHOR}」那一列`); process.exit(1);
  } else {
    const style = [2, 3, 4].map(c => doc.getCell(anchor, c).style);
    const h = doc.getRow(anchor).height;
    doc.spliceRows(anchor + 1, 0, ...DOC_ROWS.map(() => []));
    DOC_ROWS.forEach((cols, i) => {
      const r = anchor + 1 + i;
      doc.getRow(r).height = h;
      cols.forEach((v, j) => { doc.getCell(r, j + 2).value = v; doc.getCell(r, j + 2).style = style[j]; });
    });
    console.log(`✓ 「說明」的各工作表用途補上 ${DOC_ROWS.length} 列（第 ${anchor + 1} 列起）`);
    changed++;
  }
}

/* 刻意換掉的舊值：最後比對時不算「遺失」 */
const gone = { 行程: new Set(), 說明: new Set(), 打包清單: new Set() };
const replaceText = (ws, r, c, from, to) => {
  const cell = ws.getCell(r, c);
  const old = String(cell.value ?? "");
  if (old.includes(to)) return false;
  if (!old.includes(from)) { console.error(`「${ws.name}」${r},${c} 找不到：${from.slice(0, 30)}`); process.exit(1); }
  gone[ws.name].add(JSON.stringify(cell.value));
  cell.value = old.replace(from, to);
  return true;
};

/* ── ④ 行程：交通(分)、營業時間、費用、電話 ─────── */
const PLAN_COLS = [
  { name: "交通(分)", like: 3, width: 10 },   // 數字，樣式抄「停留(分)」
  { name: "營業時間", like: 9, width: 30 },   // 文字，樣式抄「地點」
  { name: "費用",     like: 9, width: 22 },
  { name: "電話",     like: 9, width: 18 },
];
const plan = wb.getWorksheet("行程");
if (plan){
  const have = [];
  for (let c = 1; c <= plan.columnCount; c++) have.push(String(plan.getCell(4, c).value ?? ""));
  if (have.includes(PLAN_COLS[0].name)){
    console.log("－ 「行程」已經有交通(分) 等欄位");
  } else {
    /* 接在最後面（第 15 欄起）。時間欄的公式只引用 $A/$C/$D，插在中間雖然也
       碰不到，但匯出工具是照欄號寫的，「說明」裡也寫死了「連結樣式（第 14 欄）」。 */
    const first = have.length + 1;
    const head = plan.getCell(4, have.length).style;
    PLAN_COLS.forEach((col, i) => {
      const c = first + i;
      plan.getCell(4, c).value = col.name;
      plan.getCell(4, c).style = head;
      for (let r = 5; r <= plan.rowCount; r++) plan.getCell(r, c).style = plan.getCell(r, col.like).style;
      plan.getColumn(c).width = col.width;
    });
    const last = first + PLAN_COLS.length - 1;
    const L = n => String.fromCharCode(64 + n);

    /* 換天底色原本只蓋到 M 欄，新欄位要是白的，表格會一半有色一半沒有。
       規則公式一字不改，只多套到 N 起（順便補上原本就漏掉的 N 欄）。 */
    const cfs = plan.conditionalFormattings || [];
    let pr = Math.max(0, ...cfs.flatMap(x => x.rules.map(r => r.priority || 0)));
    const rules = cfs.filter(x => /^A5:/.test(x.ref)).flatMap(x => x.rules)
      .map(r => ({ ...JSON.parse(JSON.stringify(r)), priority: ++pr }));
    plan.addConditionalFormatting({ ref: `N5:${L(last)}400`, rules });

    replaceText(plan, 2, 1,
      "「停留(分)」填這個項目要花多久（交通就是移動時間），",
      "「停留(分)」填這一列總共佔掉多久——開車或走過去的時間也算進去，「時間」欄靠它往下推。" +
      "其中花在路上的另外填在「交通(分)」，網站會自己扣掉，卡片上才分得出車程與停留。");
    console.log(`✓ 「行程」加上 ${PLAN_COLS.map(c => c.name).join("、")}（第 ${first}–${last} 欄），換天底色延伸到 ${L(last)} 欄`);
    changed++;
  }
}

/* ── ⑤ 說明：說明欄該寫什麼 ───────────────── */
const RULES_TITLE = "說明／備註 的寫法";
if (doc){
  let has = false, introRow = -1, last = 0;
  for (let r = 1; r <= doc.rowCount; r++){
    const v = String(doc.getCell(r, 2).value ?? "");
    if (v === RULES_TITLE) has = true;
    if (v === "說明／備註") introRow = r;
    if (doc.getCell(r, 2).value || doc.getCell(r, 3).value) last = r;
  }
  if (has){
    console.log("－ 「說明」已經有說明欄的規則");
  } else if (introRow < 0){
    console.error("「說明」裡找不到「說明／備註」那一列"); process.exit(1);
  } else {
    /* 原本這一列自己就叫人把「交通時間、停留多久、開放時間」寫進說明——重複的根源 */
    replaceText(doc, introRow, 3, "當下要知道的操作資訊：交通時間、停留多久、開放時間、要先預約。",
      "站在門口才需要看的：會卡住的事、要先做的事、備案。卡片上已經有的不要再寫（見最下方「說明／備註 的寫法」）。");
    replaceText(doc, introRow + 1, 3, "這地方是什麼、為什麼值得看。",
      "這地方是什麼、為什麼值得看——在家讀完就不用再看的放這裡。");

    const titleStyle = doc.getCell(introRow - 1, 2).style;
    const labelStyle = doc.getCell(introRow, 2).style;
    const textStyle  = doc.getCell(introRow, 3).style;
    const rowH = doc.getRow(introRow).height;
    const SECTIONS = [
      [RULES_TITLE, null],
      ["判斷方法", "站在門口才需要看 → 說明。在家讀完就不用再看 → 介紹。"],
      ["一、卡片上有的不要寫", "時間、停留、交通、地點、營業時間、費用、電話都有自己的欄位，網站會顯示。說明裡再寫一次就是重複。"],
      ["二、一行一件事", "不要用「・」把營業時間、電話、停車擠成一行。照這個順序寫：怎麼去（路線）→ 時間限制 → 要先做的 → ⚠️ 會卡住的 → 備案。"],
      ["三、⚠️ 只給會出事的", "不知道就會白跑一趟、趕不上、進不去，才用 ⚠️。「山頂風大帶外套」是提醒，不用 ⚠️。其他 emoji 不要放在行首。"],
      ["四、不寫資料來源", "不要寫「試算表上的」「重複推薦 2 次」。評分想留就直接寫 4.5★。"],
      [null, null],
      ["新欄位怎麼填", null],
      ["交通(分)", "停留(分) 裡花在路上的分鐘數。停留(分) 照舊填總時間，網站自己扣：停留 230、交通 80 → 卡片顯示「🚗 車程 1 小時 20 分」「⏳ 停留 2 小時 30 分」。車程、步行、搭車看「交通方式」欄。"],
      ["營業時間", "一格寫完，多個設施用「／」分開，例：天神地下街 10:00–20:00／PARCO 10:00–20:30。住宿填 Check-in 與退房時間。"],
      ["費用", "門票、停車、要付的錢，含付款方式，例：¥500（現金）。"],
      ["電話", "只填號碼，網站上點了就能直接撥。"],
    ];
    let r = last + 2;
    for (const [label, text] of SECTIONS){
      if (label !== null || text !== null){
        doc.getCell(r, 2).value = label;
        doc.getCell(r, 2).style = text === null ? titleStyle : labelStyle;
        if (text !== null){
          doc.getCell(r, 3).value = text;
          doc.getCell(r, 3).style = textStyle;
          doc.getRow(r).height = rowH;
        }
      }
      r++;
    }
    console.log(`✓ 「說明」改寫「說明／備註」與「介紹」兩列，並在第 ${last + 2} 列起加上寫法規則`);
    changed++;
  }
}

/* ── ⑥ 打包清單：分類改用五大類 ───────────── */
/* 原本範例用「個人物品」「這趟專用」——照「誰加的」分，結果使用者的「保暖薄外套」
   跟自動補的「可穿脫的薄外套」分在兩區，重複了看不出來。改成跟 builder 自動補的
   同一套分類（localdata.js 的 PACK_CATEGORIES），同一種東西擺在一起。 */
const PACK_SAMPLE = {
  "隱形眼鏡與藥水": "盥洗與藥品",
  "眼鏡或備用眼鏡": "隨身雜物",
  "慣用的枕頭套或眼罩": "隨身雜物",
  "滑雪手套": "衣物與鞋",
  "泳衣（飯店有溫泉）": "衣物與鞋",
};
{
  let n = 0;
  for (let r = 5; r <= src.rowCount; r++){
    const want = PACK_SAMPLE[String(src.getCell(r, 3).value ?? "")];
    const cell = src.getCell(r, 2);
    if (want && cell.value !== want){ gone.打包清單.add(JSON.stringify(cell.value)); cell.value = want; n++; }
  }
  const desc = "行李的「分類」請用這五個：證件與錢、電子產品、衣物與鞋、盥洗與藥品、隨身雜物——" +
    "跟自動補的擺在同一區，萬一重複了一眼就看得到。已經填過的不會重複加，意思相同的也算" +
    "（例如填了「保暖外套」就不會再補「可穿脫的薄外套」）。「出發前準備」放要做的事，「行李清單」放要帶的東西。";
  const old = String(src.getCell(2, 1).value ?? "");
  if (!old.includes("請用這五個")){
    const from = "已經填過的不會重複加。";
    if (!old.includes(from)){ console.error("「打包清單」第 2 列找不到要替換的那句"); process.exit(1); }
    gone.打包清單.add(JSON.stringify(src.getCell(2, 1).value));
    src.getCell(2, 1).value = old.replace(from, desc);
    n++;
  }
  if (n){ console.log(`✓ 「打包清單」範例改用五大類，第 2 列說明寫上分類規則（${n} 格）`); changed++; }
  else console.log("－ 「打包清單」已經是五大類");
}

/* ── ⑦ 基本設定：Google 地圖嵌入金鑰 ─────────── */
/* 電腦版右側的當日路線地圖要用。接在最後一列後面，不插在中間——
   export-xlsx 是用「項目」名稱找列，不是用列號，但模板的資料驗證（B7 主題）
   綁的是固定列號，插在中間會讓它對到別的欄位。 */
{
  const ws = wb.getWorksheet("基本設定");
  const LABEL = "Google 地圖嵌入金鑰";
  let last = 0, has = false;
  for (let r = 1; r <= ws.rowCount; r++){
    const v = String(ws.getCell(r, 1).value ?? "");
    if (v) last = r;
    if (v === LABEL) has = true;
  }
  if (has){
    console.log("－ 「基本設定」已經有地圖嵌入金鑰");
  } else {
    const r = last + 1;
    for (let c = 1; c <= 3; c++) ws.getCell(r, c).style = ws.getCell(last, c).style;
    ws.getRow(r).height = ws.getRow(last).height;
    ws.getCell(r, 1).value = LABEL;
    ws.getCell(r, 3).value = "選填。電腦版右側會顯示當天的路線地圖，留空就不顯示。" +
      "到 Google Cloud 啟用 Maps Embed API（要綁帳單帳戶，但這個 API 不收費），建一把新金鑰並加兩個限制：" +
      "API 限制只勾 Maps Embed API、網站限制只填自己的網域（例如 heatherchou58.github.io/*）。" +
      "這把金鑰會公開在網站上，加了限制就沒有風險。";
    console.log(`✓ 「基本設定」第 ${r} 列加上「${LABEL}」`);
    changed++;
  }
}

if (!changed){ console.log("\n沒有需要改的地方。"); process.exit(0); }

await wb.xlsx.writeFile(FILE);

/* ── 驗證：其他工作表的值不能變 ───────────── */
const wb2 = new ExcelJS.Workbook();
await wb2.xlsx.readFile(FILE);
const after = {};
for (const ws of wb2.worksheets){
  const cells = [];
  for (let r = 1; r <= ws.rowCount; r++)
    for (let c = 1; c <= ws.columnCount; c++){
      const v = ws.getCell(r, c).value;
      if (v !== null && v !== undefined) cells.push(`${r},${c}=${JSON.stringify(v)}`);
    }
  after[ws.name] = cells;
}

console.log("\n── 比對其他工作表 ──");
let bad = 0;
for (const name of Object.keys(before)){
  if (rebuilt.has(name)){ console.log(`  ${name}：刻意重建，略過比對`); continue; }
  if (name === STYLE_FROM || name === "說明" || name === "行程" || name === "基本設定"){
    /* 這幾張是刻意改的，只檢查原本的內容都還在（刻意換掉的那幾格除外） */
    const valueOf = x => x.slice(x.indexOf("=") + 1);
    const expected = x => (gone[name] || new Set()).has(valueOf(x));
    const missing = before[name].filter(x => !after[name].includes(x) && !expected(x));
    if (name === "說明"){
      /* 說明插了列，行號會位移，改成比對「值的集合」 */
      const val = s => s.replace(/^\d+,/, "");
      const b = before[name].filter(x => !expected(x)).map(val), a = new Set(after[name].map(val));
      const lost = b.filter(x => !a.has(x));
      console.log(`  說明：原有 ${b.length} 格，遺失 ${lost.length} 格`, lost.slice(0, 3));
      if (lost.length) bad++;
    } else {
      console.log(`  ${name}：原有 ${before[name].length} 格，遺失 ${missing.length} 格`);
      if (missing.length) bad++;
    }
    continue;
  }
  const same = JSON.stringify(before[name]) === JSON.stringify(after[name]);
  console.log(`  ${name}：${same ? "未變動 ✓" : "⚠️ 有變動"}`);
  if (!same) bad++;
}
console.log(bad ? `\n⚠️ 有 ${bad} 張工作表不如預期` : "\n其他工作表都沒被動到 ✓");
process.exit(bad ? 1 : 0);
