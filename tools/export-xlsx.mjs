#!/usr/bin/env node
/* 把已產出的行程網站反推成一份 Excel，給使用者繼續編輯。

   用法：
     npm install exceljs
     node tools/export-xlsx.mjs fukuoka-2026 out.xlsx

   刻意「不」寫回自動產生的內容，否則重新產生網站時會重複或凍結成舊值：
     · 機器翻譯的當地語（_mt）→ 留空，讓產生器重新翻譯並重新標記
     · map / route 連結 → 留空，由產生器依標題重新產生
     · 通用打包物品、通用片語、各國緊急電話 → 留空，由產生器重新帶入

   寫完會自動把產物丟回 parse.js 做一次往返比對，對不上就以非零結束。
   沒有這道檢查的話，交出去的可能是一份看起來正常、實際上會掉資料的檔案。 */
import ExcelJS from "exceljs";
import fs from "node:fs";

import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TRIP = process.argv[2] || "fukuoka-2026";
const SITE = path.join(ROOT, TRIP, "index.html");
const TPL  = path.join(ROOT, "builder", "travel-template.xlsx");
const OUT  = process.argv[3] || path.join(ROOT, TRIP + "-source.xlsx");

const html = fs.readFileSync(SITE, "utf8");
const s = html.indexOf("const TRIP_DATA = ");
let i = html.indexOf("{", s), d = 0, e = -1;
for (; i < html.length; i++) { if (html[i] === "{") d++; else if (html[i] === "}") { d--; if (!d) { e = i + 1; break; } } }
const T = JSON.parse(html.slice(html.indexOf("{", s), e));

const MODE_ZH = { transit: "大眾運輸", driving: "自駕", walking: "步行", bicycling: "單車" };
const KIND_ZH = { sight: "景點", meal: "餐飲", shop: "購物" };
const AUTO_PACK_CATS = new Set(["證件與金錢", "電子用品", "藥品與清潔", "其他",
  "禦寒衣物", "換季衣物（早晚溫差大）", "消暑衣物", "雪地裝備", "雨具"]);

const addMin = (hhmm, m) => {
  const [h, mi] = hhmm.split(":").map(Number);
  const t = h * 60 + mi + m;
  return String(Math.floor(t / 60) % 24).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0");
};

const wb = new ExcelJS.Workbook();
await wb.xlsx.readFile(TPL);
const clear = (ws, from) => { for (let r = ws.rowCount; r >= from; r--) ws.spliceRows(r, 1); };
const styleFrom = (ws, src, dst, cols) => {
  for (let c = 1; c <= cols; c++) ws.getRow(dst).getCell(c).style = { ...ws.getRow(src).getCell(c).style };
};

/* ── 基本設定 ── */
const meta = wb.getWorksheet("基本設定");
const setMeta = (label, v) => {
  for (let r = 4; r <= meta.rowCount; r++)
    if (String(meta.getRow(r).getCell(1).value || "").trim() === label) { meta.getRow(r).getCell(2).value = v; return; }
  throw new Error("基本設定找不到：" + label);
};
setMeta("網站標題", T.meta.title);
setMeta("副標題", T.meta.subtitle);
setMeta("行程代號", T.meta.tripId);
setMeta("主題", T.meta.theme);
setMeta("出發日", T.meta.startDate);
setMeta("回程日", T.meta.endDate);
setMeta("城市（英文）", T.meta.place.name);
setMeta("城市（當地語）", T.meta.place.nameLocal || "");
setMeta("緯度", ""); setMeta("經度", ""); setMeta("時區", "");
setMeta("交通方式", MODE_ZH[T.meta.travelMode] || "自駕");
setMeta("本國幣別", T.meta.money.home);
setMeta("當地幣別", ""); setMeta("匯率", "");

/* ── 成員 ── */
const mem = wb.getWorksheet("成員");
clear(mem, 5);
T.meta.members.forEach((m, n) => {
  const r = 5 + n;
  styleFrom(mem, 5, r, 2);
  mem.getRow(r).getCell(1).value = m;
});

/* ── 行程 ── */
const plan = wb.getWorksheet("行程");
const formula = plan.getRow(8).getCell(4).value?.formula;
if (!formula) throw new Error("取不到時間公式");
clear(plan, 5);

const overrides = [];
let row = 5, curMode = T.meta.travelMode, dayNo = 0;

for (const day of T.days) {
  if (!/^D\d+/.test(day.nav)) continue;
  dayNo++;
  const items = (day.blocks || []).filter(b => b.type === "stop" || b.type === "lodging");
  let first = true, clock = "", prevStay = 0;

  for (const b of items) {
    const R = plan.getRow(row);
    styleFrom(plan, 5, row, 14);
    R.getCell(1).value = dayNo;
    if (first) R.getCell(2).value = day.nav.replace(/^D\d+\s*/, "");
    R.getCell(3).value = b.stay ?? "";

    /* 第一列填實際時間；其餘用公式，但要帶快取結果，否則沒被試算軟體
       打開過的檔案讀不到值。若公式算出來的與現況不符，代表那一列原本
       就是手動指定的時間，改填實際值（公式本來就設計成可被覆寫）。 */
    if (first) {
      R.getCell(4).value = b.time || "";
      clock = b.time || "";
    } else {
      const expect = clock ? addMin(clock, prevStay) : "";
      if (expect && expect === (b.time || "")) {
        R.getCell(4).value = {
          formula: formula.replace(/\d+/g, m => String(Number(m) + (row - 8))),
          result: b.time
        };
        clock = b.time;
      } else {
        R.getCell(4).value = b.time || "";
        clock = b.time || expect;
        overrides.push(day.nav + " " + b.title + "（公式算 " + (expect || "—") + "、實際 " + (b.time || "—") + "）");
      }
    }
    prevStay = b.stay || 0;

    if (b.mode && b.mode !== curMode) { R.getCell(5).value = MODE_ZH[b.mode]; curMode = b.mode; }
    R.getCell(6).value = b.type === "lodging" ? "住宿" : (KIND_ZH[b.kind] || "景點");
    R.getCell(7).value = b.title;
    R.getCell(8).value = b._mt ? "" : (b.titleLocal || "");
    R.getCell(9).value = b.place || b.address || "";
    R.getCell(10).value = (b.notes || []).join("\n");
    R.getCell(11).value = b.intro || "";

    const keep = (b.links || []).filter(l => l.style !== "map" && l.style !== "route");
    if (keep[0]) {
      R.getCell(12).value = keep[0].label;
      R.getCell(13).value = keep[0].url;
      R.getCell(14).value = keep[0].style;
    }
    row++; first = false;

    for (const extra of keep.slice(1)) {
      const X = plan.getRow(row);
      styleFrom(plan, 5, row, 14);
      X.getCell(1).value = dayNo;
      X.getCell(6).value = "→連結";
      X.getCell(12).value = extra.label;
      X.getCell(13).value = extra.url;
      X.getCell(14).value = extra.style;
      row++;
    }
  }
}

/* ── 航班 ── */
const fl = wb.getWorksheet("航班");
clear(fl, 5);
let fr = 5;
for (const day of T.days) {
  if (!/^D\d+/.test(day.nav)) continue;
  const n = Number(day.nav.match(/^D(\d+)/)[1]);
  for (const b of (day.blocks || [])) {
    if (b.type !== "flight") continue;
    const R = fl.getRow(fr);
    styleFrom(fl, 5, fr, 10);
    R.getCell(1).value = n;
    R.getCell(2).value = day.blocks.indexOf(b) === 0 ? "當天最前" : "當天最後";
    R.getCell(3).value = b.airline;
    R.getCell(4).value = b.code;
    R.getCell(5).value = b.from;
    R.getCell(6).value = b.fromTerminal || "";
    R.getCell(7).value = b.to;
    R.getCell(8).value = b.toTerminal || "";
    R.getCell(9).value = b.depart;
    R.getCell(10).value = b.arrive;
    fr++;
  }
}

/* ── 打包清單：只寫使用者自己的分類 ── */
const pk = wb.getWorksheet("打包清單");
clear(pk, 5);
let pr = 5, skipped = 0;
for (const day of T.days) for (const b of (day.blocks || [])) {
  if (b.type !== "checklist") continue;
  for (const g of b.groups || []) {
    if (AUTO_PACK_CATS.has(g.name)) { skipped += g.items.length; continue; }
    for (const it of g.items) {
      const R = pk.getRow(pr);
      styleFrom(pk, 5, pr, 4);
      R.getCell(1).value = b.title;
      R.getCell(2).value = g.name;
      R.getCell(3).value = typeof it === "string" ? it : it.text;
      R.getCell(4).value = typeof it === "string" ? "" : (it.url || "");
      pr++;
    }
  }
}

/* ── 常用片語與緊急資訊：全部是自動產生的，留空 ── */
clear(wb.getWorksheet("常用片語"), 5);
clear(wb.getWorksheet("緊急資訊"), 5);

await wb.xlsx.writeFile(OUT);
if (overrides.length) {
  console.log("手動指定時間的列（" + overrides.length + "）：");
  overrides.forEach(x => console.log("  " + x));
  console.log("");
}
console.log("行程 " + (row - 5) + " 列、航班 " + (fr - 5) + " 筆、打包清單 " + (pr - 5) + " 項（略過 " + skipped + " 項自動產生的）");

/* ── 往返驗證：把剛寫出的檔案丟回 parse.js，確認能還原成同一份行程 ──
   少了這一步，就可能交出一份看起來正常、實際上會掉資料的檔案。 */
const { parseWorkbook } = await import(
  pathToFileURL(path.join(ROOT, "builder", "assets", "parse.js")).href);

const back = new ExcelJS.Workbook();
await back.xlsx.readFile(OUT);
const { data: B, errors } = parseWorkbook(back);

let bad = 0;
const ok = (n, c, detail = "") => {
  console.log((c ? "  OK   " : "  FAIL ") + n + (detail ? "  — " + detail : ""));
  if (!c) bad++;
};
const flat = X => {
  const o = [];
  for (const day of X.days || []) {
    if (!/^D\d+/.test(day.nav)) continue;
    for (const b of day.blocks || []) if (b.type === "stop" || b.type === "lodging") o.push(b);
  }
  return o;
};
const flights = X => {
  const o = [];
  for (const day of X.days || []) for (const b of day.blocks || [])
    if (b.type === "flight") o.push(b.code + " " + b.depart + "-" + b.arrive);
  return o;
};

console.log("\n往返驗證：");
ok("重新解析沒有錯誤", errors.length === 0,
   errors.slice(0, 3).map(x => x.sheet + "列" + x.row + ": " + x.msg).join(" / "));

const a = flat(T), b2 = flat(B);
ok("景點／住宿數量一致", a.length === b2.length, a.length + " → " + b2.length);

for (const f of ["title", "type", "kind", "stay", "mode", "time", "intro", "notes"]) {
  const diff = [];
  for (let n = 0; n < Math.min(a.length, b2.length); n++) {
    const x = JSON.stringify(a[n][f] ?? (f === "notes" ? [] : ""));
    const y = JSON.stringify(b2[n][f] ?? (f === "notes" ? [] : ""));
    if (x !== y && diff.length < 2) diff.push(a[n].title + ": " + x.slice(0, 30) + " ≠ " + y.slice(0, 30));
  }
  ok(f + " 一致", diff.length === 0, diff.join(" / "));
}
ok("航班一致", flights(T).join() === flights(B).join());

const booking = X => flat(X).flatMap(x => (x.links || []).filter(l => l.style === "booking").length);
ok("訂房連結保留", booking(T).join() === booking(B).join());
ok("機器翻譯的當地語已留空",
   flat(B).filter((x, n) => a[n]?._mt).every(x => !x.titleLocal));

if (bad) { console.error("\n往返驗證失敗 " + bad + " 項，這份檔案不要用。"); process.exit(1); }
console.log("\n往返驗證全部通過：" + OUT);
