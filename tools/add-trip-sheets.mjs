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
      + "離線也打得開；要現場生成的券改填「連結」，會變成一顆跳轉按鈕。整張留空就不會有購物分頁。",
  headers: ["群組", "項目", "誰要的", "圖片", "連結"],
  widths: [18, 26, 14, 28, 52],
  /* 填入的欄位要上奶油色底，跟其他工作表一致 */
  fillFrom: [1, 1, 1, 1, 1],
  rows: [
    ["折價券", "鶴羽藥妝", "", "img/tsuruha.jpg", ""],
    ["折價券", "唐吉訶德 Donki", "", "", "https://japanportal.donki-global.com/coupon/cp001_zhtw.html"],
    ["藥妝店", "資生堂吸油面紙", "媽媽", "", ""],
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
  if (wb.getWorksheet(spec.name)){ console.log(`－ 「${spec.name}」已存在`); continue; }
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
  if (name === STYLE_FROM || name === "說明"){
    /* 這兩張是刻意改的，只檢查原本的內容都還在 */
    const missing = before[name].filter(x => !after[name].includes(x));
    if (name === "說明"){
      /* 說明插了列，行號會位移，改成比對「值的集合」 */
      const val = s => s.replace(/^\d+,/, "");
      const b = before[name].map(val), a = new Set(after[name].map(val));
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
