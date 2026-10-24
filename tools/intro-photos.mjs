#!/usr/bin/env node
/* 介紹照片：壓縮成 ASCII 檔名的 JPEG，並掛到行程網站對應的卡片上。

   用法：
     node tools/intro-photos.mjs fukuoka-2026                  只把照片掛到卡片上
     node tools/intro-photos.mjs fukuoka-2026 --src <原圖資料夾>  先重新壓縮，再掛上去

   清單在 <行程>/intro-photos.json，每張一筆：
     { "day": "d2", "card": "門司港懷舊區", "file": "mojiko-retro.jpg",
       "title": "Commons 上的檔名", "author": "…", "license": "CC BY-SA 4.0",
       "page": "https://commons.wikimedia.org/wiki/File:…" }
   照片只用 Wikimedia Commons 的自由授權（CC0／CC BY／CC BY-SA）——repo 與網站都是公開的，
   官網或搜尋引擎找到的照片有版權，不能放。CC BY／BY-SA 要求標示作者與授權，
   網站上每張照片底下都會顯示，所以 author、license、page 缺一不可。

   --src 的資料夾裡，原圖檔名要跟清單的 file 一樣。壓縮規則：限寬 800、品質 76。
   跟優惠券（tools/optimise-coupons.mjs）不同，這些照片沒有 QR 要掃，可以壓得比較狠。

   卡片上的 photo 欄位跟私人資料的 secret 鍵一樣，用 builder 重新產生網站、
   或任何重寫行程區塊的步驟都會被洗掉，之後要再跑一次（不加 --src 就好）。
   照片放在 trip-img 快取（網頁開啟時預先抓），換照片不用升 sw.js 的 VERSION。

   檢查：TRIP_DATA 格式一致、每張都對到剛好一張「有介紹」的卡片、輸出檔都存在。 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TRIP = process.argv[2];
if (!TRIP){ console.error("用法：node tools/intro-photos.mjs <行程資料夾> [--src <原圖資料夾>]"); process.exit(2); }
const srcArg = process.argv.indexOf("--src");
const SRC  = srcArg > 0 ? path.resolve(process.argv[srcArg + 1]) : null;
const SITE = path.join(ROOT, TRIP, "index.html");
const LIST = path.join(ROOT, TRIP, "intro-photos.json");
const DST  = path.join(ROOT, TRIP, "img", "intro");
const MAX_W = 800, QUALITY = 76;

const list = JSON.parse(fs.readFileSync(LIST, "utf8"));
const FREE = /^(CC0|CC BY(-SA)? [0-9.]+|Public domain)$/i;
const bad = [];
for (const p of list){
  if (!/^[a-z0-9-]+\.jpg$/.test(p.file || "")) bad.push(`檔名只能是小寫英數與連字號的 .jpg：${p.file}`);
  if (!FREE.test(p.license || "")) bad.push(`「${p.card}」的授權不是自由授權：${p.license}`);
  if (!p.author || !/^https:\/\/commons\.wikimedia\.org\//.test(p.page || "")) bad.push(`「${p.card}」缺作者或 Commons 出處`);
}
if (new Set(list.map(p => p.file)).size !== list.length) bad.push("清單裡有重複的檔名");
if (bad.length){ console.error(bad.join("\n")); process.exit(1); }

/* ── 壓縮 ── */
if (SRC){
  const { default: sharp } = await import("sharp");
  fs.mkdirSync(DST, { recursive: true });
  let before = 0, after = 0;
  for (const p of list){
    const from = path.join(SRC, p.file);
    if (!fs.existsSync(from)){ console.error("找不到原圖：" + from); process.exit(1); }
    const meta = await sharp(from).metadata();
    await sharp(from)
      .resize({ width: Math.min(MAX_W, meta.width), withoutEnlargement: true })
      .jpeg({ quality: QUALITY, mozjpeg: true })
      .toFile(path.join(DST, p.file));
    before += fs.statSync(from).size;
    after  += fs.statSync(path.join(DST, p.file)).size;
  }
  console.log(`壓縮 ${list.length} 張：${(before / 1024).toFixed(0)} KB → ${(after / 1024).toFixed(0)} KB`);
}

const missingFiles = list.filter(p => !fs.existsSync(path.join(DST, p.file))).map(p => p.file);
if (missingFiles.length){ console.error("img/intro 裡缺這些檔：" + missingFiles.join("、")); process.exit(1); }

/* ── 掛到卡片上 ── */
const html = fs.readFileSync(SITE, "utf8");
const s = html.indexOf("const TRIP_DATA = ");
const start = html.indexOf("{", s);
let d = 0, end = -1;
for (let i = start; i < html.length; i++){
  if (html[i] === "{") d++;
  else if (html[i] === "}"){ d--; if (!d){ end = i + 1; break; } }
}
const raw = html.slice(start, end);
const T = JSON.parse(raw);
if (JSON.stringify(T, null, 2) !== raw){
  console.error("TRIP_DATA 的格式跟 JSON.stringify(…, null, 2) 不一致，停止——寫回去會改寫整段。");
  process.exit(1);
}

/* 先全部清掉再重掛，清單刪掉的照片才不會留在卡片上 */
for (const day of T.days) for (const b of day.blocks || []) delete b.photo;
const miss = [];
for (const p of list){
  const day = T.days.find(x => x.id === p.day);
  const hits = (day ? day.blocks || [] : []).filter(b => b.type === "stop" && b.title === p.card);
  if (hits.length !== 1){ miss.push(`${p.day}「${p.card}」找到 ${hits.length} 張`); continue; }
  if (!hits[0].intro){ miss.push(`${p.day}「${p.card}」沒有介紹，照片沒地方放`); continue; }
  hits[0].photo = { src: `img/intro/${p.file}`, credit: p.author, license: p.license, href: p.page };
}
if (miss.length){ console.error("對不到卡片：\n  " + miss.join("\n  ")); process.exit(1); }

const out = html.slice(0, start) + JSON.stringify(T, null, 2) + html.slice(end);
if (out !== html) fs.writeFileSync(SITE, out);
const kb = list.reduce((n, p) => n + fs.statSync(path.join(DST, p.file)).size, 0) / 1024;
console.log(`OK  ${list.length} 張照片掛到卡片上（img/intro 共 ${kb.toFixed(0)} KB）${out === html ? "，內容沒變" : ""}`);
