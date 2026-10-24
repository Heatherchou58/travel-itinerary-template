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
/* 原始試算表跟產出的網站放一起，不要掉到 repo 根目錄 */
const OUT  = process.argv[3] || path.join(ROOT, TRIP, TRIP + "-source.xlsx");

const html = fs.readFileSync(SITE, "utf8");
const s = html.indexOf("const TRIP_DATA = ");
let i = html.indexOf("{", s), d = 0, e = -1;
for (; i < html.length; i++) { if (html[i] === "{") d++; else if (html[i] === "}") { d--; if (!d) { e = i + 1; break; } } }
const T = JSON.parse(html.slice(html.indexOf("{", s), e));

const MODE_ZH = { transit: "大眾運輸", driving: "自駕", walking: "步行", bicycling: "單車" };
const KIND_ZH = { sight: "景點", meal: "餐飲", shop: "購物" };
/* 哪些打包項目是 builder 自動補的——這些不寫回 Excel，重新產生時會再補。
   以前是整個分類跳過，但現在使用者的項目跟自動的共用同一套分類（同一種
   東西擺在一起，重複才看得到），整組跳過會把使用者自己的也丟掉，所以改成
   逐項比對。比對鍵跟 builder 去重用的是同一支 packNorm。 */
const { BASE_PACK, CLIMATE_PACK, packText, packNorm, mergePack, climateOf } =
  await import(pathToFileURL(path.join(ROOT, "builder", "assets", "localdata.js")).href);
const AUTO_PACK = new Set([
  ...BASE_PACK.flatMap(g => g.items),
  ...Object.values(CLIMATE_PACK).flatMap(k => k.groups.flatMap(g => g.items)),
  "轉接頭",                                   // 規格依國家產生，括號拿掉後都是「轉接頭」
  "駕照與國際駕照", "盥洗包",                  // 舊版自動項目的寫法，舊網站匯出時還認得
].map(packNorm));

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

/* 行程表的欄數：1–14 是原本的，15–18 是交通(分)、營業時間、費用、電話 */
const PLAN_COLS = 18;
const overrides = [];
let row = 5, curMode = T.meta.travelMode, dayNo = 0;

for (const day of T.days) {
  if (!/^D\d+/.test(day.nav)) continue;
  dayNo++;
  const items = (day.blocks || []).filter(b => b.type === "stop" || b.type === "lodging" || b.type === "options");
  let first = true, clock = "", prevStay = 0, afterSub = false;

  for (const b of items) {
    const R = plan.getRow(row);
    styleFrom(plan, 5, row, PLAN_COLS);
    R.getCell(1).value = dayNo;
    if (first) R.getCell(2).value = day.nav.replace(/^D\d+\s*/, "");
    R.getCell(3).value = b.stay ?? "";

    /* 第一列填實際時間；其餘用公式，但要帶快取結果，否則沒被試算軟體
       打開過的檔案讀不到值。若公式算出來的與現況不符，代表那一列原本
       就是手動指定的時間，改填實際值（公式本來就設計成可被覆寫）。 */
    if (first) {
      R.getCell(4).value = b.time || "";
      clock = b.time || "";
    } else if (afterSub) {
      /* 時間公式只看上一列。上一列是「→選項」「→連結」這種沒有時間的附屬列時，
         在 Excel 或 Google 試算表重新計算會變成空白，所以這一列直接填實際時間。 */
      R.getCell(4).value = b.time || "";
      clock = b.time || (clock ? addMin(clock, prevStay) : "");
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
    R.getCell(6).value = b.type === "lodging" ? "住宿" : b.type === "options" ? "二選一" : (KIND_ZH[b.kind] || "景點");
    R.getCell(7).value = b.title;
    R.getCell(8).value = b._mt ? "" : (b.titleLocal || "");
    R.getCell(9).value = b.place || b.address || "";
    R.getCell(10).value = (b.notes || []).join("\n");
    R.getCell(11).value = b.intro || "";

    /* 自動產生的地圖與路線按鈕不寫回去，重新產生時會再補；但自己指定的地圖連結
       （例如 maps.app.goo.gl 短網址）要留著，否則重新產生時會被換回自動查的版本。 */
    const autoMap = l => l.style === "map" && /\/maps\/search\//.test(l.url || "");
    const keep = (b.links || []).filter(l => l.style !== "route" && !autoMap(l));
    if (keep[0]) {
      R.getCell(12).value = keep[0].label;
      R.getCell(13).value = keep[0].url;
      R.getCell(14).value = keep[0].style;
    }
    R.getCell(15).value = b.travel ?? "";
    R.getCell(16).value = b.hours || "";
    R.getCell(17).value = b.fee || "";
    R.getCell(18).value = b.tel || "";
    row++; first = false;

    for (const extra of keep.slice(1)) {
      const X = plan.getRow(row);
      styleFrom(plan, 5, row, PLAN_COLS);
      X.getCell(1).value = dayNo;
      X.getCell(6).value = "→連結";
      X.getCell(12).value = extra.label;
      X.getCell(13).value = extra.url;
      X.getCell(14).value = extra.style;
      row++;
    }
    afterSub = keep.length > 1;

    /* 二選一的每個選項一列「→選項」。parse.js 把「→連結」接到二選一本身而不是選項上，
       所以每個選項只能帶一個連結（就寫在「→選項」那一列）。 */
    if (b.type === "options") for (const c of b.choices || []) {
      const ck = (c.links || []).filter(l => l.style !== "route" && !autoMap(l));
      if (ck.length > 1) throw new Error(`${day.nav}「${c.title}」：二選一的選項只能保留一個連結`);
      const X = plan.getRow(row);
      styleFrom(plan, 5, row, PLAN_COLS);
      X.getCell(1).value = dayNo;
      X.getCell(6).value = "→選項";
      X.getCell(7).value = c.title;
      X.getCell(8).value = c._mt ? "" : (c.titleLocal || "");
      X.getCell(10).value = c.note || "";
      if (ck[0]) {
        X.getCell(12).value = ck[0].label;
        X.getCell(13).value = ck[0].url;
        X.getCell(14).value = ck[0].style;
      }
      row++; afterSub = true;
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

/* ── 打包清單：只寫使用者自己的項目 ── */
const pk = wb.getWorksheet("打包清單");
clear(pk, 5);
let pr = 5, skipped = 0;
for (const day of T.days) for (const b of (day.blocks || [])) {
  if (b.type !== "checklist") continue;
  /* 放在資訊分頁的清單（免稅規定、電子支付那類）要把「分頁」寫回去，
     否則重新產生時會全部掉回行前，把打包清單撐長。 */
  const page = day.id === "info" ? "資訊" : "";
  for (const g of b.groups || []) {
    for (const it of g.items) {
      if (AUTO_PACK.has(packNorm(it))) { skipped++; continue; }
      const R = pk.getRow(pr);
      styleFrom(pk, 5, pr, 5);
      R.getCell(1).value = b.title;
      R.getCell(2).value = g.name;
      R.getCell(3).value = typeof it === "string" ? it : it.text;
      R.getCell(4).value = typeof it === "string" ? "" : (it.url || "");
      R.getCell(5).value = page;
      pr++;
    }
  }
}

/* ── 購物清單與優惠券 ── */
/* clear() 會把第 5 列一起刪掉，styleFrom 就抄不到樣式了，所以先存起來 */
const grabStyle = (ws, cols) => {
  const out = [];
  for (let c = 1; c <= cols; c++) out.push({ ...ws.getRow(5).getCell(c).style });
  return out;
};
const putStyle = (ws, r, st) => st.forEach((v, n) => { ws.getRow(r).getCell(n + 1).style = v; });

const blocksOf = type => T.days.flatMap(d => d.blocks || []).filter(b => b.type === type);

const sh = wb.getWorksheet("購物清單");
if (sh) {
  const st = grabStyle(sh, 6);
  clear(sh, 5);
  let sr = 5;
  for (const b of blocksOf("shopping"))
    for (const g of b.groups || []) for (const it of g.items || []) {
      const R = sh.getRow(sr);
      putStyle(sh, sr, st);
      R.getCell(1).value = g.name;
      /* 券不給數量與金額欄位，旗標在群組層級，所以整組每一列都寫 */
      R.getCell(2).value = g.coupon ? "券" : "";
      R.getCell(3).value = it.name || "";
      R.getCell(4).value = it.who || "";
      R.getCell(5).value = it.img || "";
      R.getCell(6).value = it.url || "";
      sr++;
    }
}

/* ── 信用卡 ── */
const cd = wb.getWorksheet("信用卡");
if (cd) {
  const st = grabStyle(cd, 8);
  clear(cd, 5);
  let cr = 5;
  /* 查證日期藏在 block 的說明裡——那句話是 parse.js 依日期生成的。
     把日期抓回來寫進每一列，重新產生時整句就會一字不差地重建。 */
  const checked = (blocksOf("cards")[0]?.notes || [])
    .map(n => (/(\d{4}-\d{2}-\d{2})/.exec(n) || [])[1]).find(Boolean) || "";
  for (const b of blocksOf("cards")) for (const c of b.items || []) {
    const R = cd.getRow(cr);
    putStyle(cd, cr, st);
    R.getCell(1).value = c.name || "";
    R.getCell(2).value = c.rate ?? "";
    R.getCell(3).value = c.regions || "";
    R.getCell(4).value = c.until || "";
    R.getCell(5).value = c.quota || "";
    R.getCell(6).value = c.register ? "是" : "否";
    R.getCell(7).value = checked;
    R.getCell(8).value = c.note || "";
    cr++;
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

for (const f of ["title", "type", "kind", "stay", "mode", "time", "intro", "notes",
                 "travel", "hours", "fee", "tel"]) {
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
const customMap = X => flat(X).flatMap(x => (x.links || [])
  .filter(l => l.style === "map" && !/\/maps\/search\//.test(l.url || "")).map(l => l.url));
ok("自訂地圖連結保留", customMap(T).join() === customMap(B).join());
const opts = X => (X.days || []).filter(d => /^D\d+/.test(d.nav)).flatMap(d => (d.blocks || [])
  .filter(b => b.type === "options")
  .map(b => JSON.stringify([b.time, b.title, b.place || "", b.stay ?? null, (b.choices || []).map(c =>
    [c.label, c.title, c.titleLocal || "", c.note || "",
     (c.links || []).filter(l => !/\/maps\/search\//.test(l.url || "")).map(l => l.url)])])));
ok("二選一一致", opts(T).join("\n") === opts(B).join("\n"), opts(T).length + " 張");
ok("機器翻譯的當地語已留空",
   flat(B).filter((x, n) => a[n]?._mt).every(x => !x.titleLocal));

const blockOf = (X, t) => X.days.flatMap(d => d.blocks || []).find(b => b.type === t);
const shopRows = X => (blockOf(X, "shopping")?.groups || [])
  .flatMap(g => g.items.map(i =>
    [g.name, g.coupon ? "券" : "", i.name, i.who || "", i.img || "", i.url || ""].join("/")));
ok("購物清單一致", shopRows(T).join("|") === shopRows(B).join("|"),
   shopRows(T).length + " 項 → " + shopRows(B).length + " 項");

const cardRows = X => (blockOf(X, "cards")?.items || []).map(c =>
  [c.name, c.rate ?? "", c.regions || "", c.until || "", c.quota || "",
   c.register ? 1 : 0, c.note || ""].join("/"));
ok("信用卡一致", cardRows(T).join("|") === cardRows(B).join("|"),
   cardRows(T).length + " 張 → " + cardRows(B).length + " 張");

/* 說明不是寫在表裡的，是 parse.js 依內容生成的。這兩項若不過，
   代表生成規則跟現有內容對不上，重新產生時說明會變。 */
const notesOf = (X, t) => (blockOf(X, t)?.notes || []).join(" ⏎ ");
for (const t of ["shopping", "cards"])
  ok(t + " 的說明重建一致", notesOf(T, t) === notesOf(B, t),
     notesOf(T, t).slice(0, 40) + " ≠ " + notesOf(B, t).slice(0, 40));

const infoLists = X => (X.days.find(d => d.id === "info")?.blocks || [])
  .filter(b => b.type === "checklist").map(b => b.title);
ok("資訊分頁的清單沒掉回行前", infoLists(T).join() === infoLists(B).join(),
   "[" + infoLists(T).join() + "] → [" + infoLists(B).join() + "]");

/* 打包清單：Excel 只存使用者自己的項目，讀回來之後用 builder 同一支 mergePack
   補上自動項目，要跟網站上現在的一模一樣。這一項不過，代表下次重新產生網站，
   清單會長成別的樣子（重複的又跑回來、分類又散掉）。 */
const packOf = X => ((X.days.find(d => d.id === "d0") || {}).blocks || []).filter(b => b.type === "checklist")
  .map(b => b.title + "｜" + b.groups.map(g => g.name + "：" + g.items.map(packText).join("／")).join("；"));
{
  const lists = ((B.days.find(d => d.id === "d0") || {}).blocks || []).filter(b => b.type === "checklist");
  const target = lists.find(b => /行李|打包|裝備/.test(b.title || "")) || lists[lists.length - 1];
  const kinds = climateOf(Number(String(T.meta.startDate).slice(5, 7)) || 1, T.meta.place.lat);
  const auto = [
    ...BASE_PACK.map(g => ({ name: g.name, items: [...g.items] })),
    ...kinds.flatMap(k => CLIMATE_PACK[k].groups.map(g => ({ name: g.name, items: [...g.items] }))),
  ];
  /* 轉接頭規格是 builder 依國家查出來的，TRIP_DATA 沒存國碼——直接沿用網站上那一項 */
  const plug = packOf(T).join("").match(/轉接頭（[^）]*）/);
  if (plug) auto.find(g => g.name === "電子產品").items.push(plug[0]);
  if (target) mergePack(lists, target, auto);
  const a = packOf(T), b = packOf(B);
  const at = a.findIndex((x, n) => x !== b[n]);
  ok("打包清單重新產生後一致", at < 0,
     at < 0 ? "" : (a[at] || "").slice(0, 60) + " ≠ " + (b[at] || "").slice(0, 60));
}

if (bad) { console.error("\n往返驗證失敗 " + bad + " 項，這份檔案不要用。"); process.exit(1); }
console.log("\n往返驗證全部通過：" + OUT);
