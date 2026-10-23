#!/usr/bin/env node
/* 優惠券圖片：改成 ASCII 檔名、限制寬度、輸出 JPEG。

   用法：
     npm install sharp
     node tools/optimise-coupons.mjs fukuoka-2026

   會讀 <行程>/優惠券/ 裡的原圖，輸出到 <行程>/img/。

   兩個刻意的選擇：

   · **限寬而不是限長邊。** 優惠券常是長截圖（PARIS MIKI 那張是 2918×10332），
     限長邊 1200 會讓寬度只剩 339px，字全部糊掉。手機是寬度受限、垂直捲動，
     限寬才對。

   · **品質 88 而不是 80。** 這些圖上有 QR 與條碼，要在櫃檯掃得到。
     壓太狠會掃不出來，省那幾十 KB 不值得。

   檔名一律 ASCII——Linux 的 zip 預設不設 UTF-8 旗標，Windows 內建解壓縮
   會用 Big5 解讀中文檔名並**直接跳過該檔，而且不會報錯**。顯示用的中文
   名稱放在 TRIP_DATA 裡，不靠檔名。 */
import sharp from "sharp";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TRIP = process.argv[2] || "fukuoka-2026";
const SRC  = path.join(ROOT, TRIP, "優惠券");
const DST  = path.join(ROOT, TRIP, "img");
const MAX_W = 1080, QUALITY = 88;

/* 原檔名 → [ASCII 檔名, 顯示名稱]。加新券時在這裡補一列。 */
const NAMES = {
  "Alpen.png":                  ["alpen.jpg",                    "Alpen 運動用品"],
  "Bigcamera_salonia.jpg":      ["biccamera-salonia.jpg",        "BicCamera／SALONIA"],
  "Drug 11.jpeg":               ["drug11.jpg",                   "Drug 11"],
  "SUGI藥妝.jpg":                ["sugi.jpg",                     "SUGI 藥妝"],
  "Snow Peak.jpg":              ["snowpeak.jpg",                 "Snow Peak"],
  "YAMADA.jpg":                 ["yamada.jpg",                   "YAMADA 電機"],
  "bigCamera.jpg":              ["biccamera.jpg",                "BicCamera／KOJIMA"],
  "sundrug.jpg":                ["sundrug.jpg",                  "SUN DRUG"],
  "大賀藥局.jpg":                ["daiga-drug.jpg",               "大賀藥局"],
  "天神COSMOS.jpg":              ["tenjin-cosmos.jpg",            "天神 COSMOS"],
  "愛電王.jpg":                   ["aidenwang.jpg",                "愛電王"],
  "福岡機場免稅店.jpg":           ["fukuoka-airport-dutyfree.jpg", "福岡機場免稅店"],
  "連鎖眼鏡店 PARIS MIKI.png":    ["paris-miki.jpg",               "PARIS MIKI 眼鏡"],
  "鶴羽藥妝.jpg":                 ["tsuruha.jpg",                  "鶴羽藥妝"],
};

if (!fs.existsSync(SRC)) { console.error("找不到來源資料夾：" + SRC); process.exit(1); }
fs.mkdirSync(DST, { recursive: true });

/* 來源有、對照表沒有的檔案要擋下來，不然會安靜漏掉 */
const onDisk = fs.readdirSync(SRC).filter(f => !f.startsWith("."));
const missing = onDisk.filter(f => !NAMES[f]);
if (missing.length) {
  console.error("這些檔案不在對照表裡，請先補進 NAMES：\n  " + missing.join("\n  "));
  process.exit(1);
}

const rows = [];
let before = 0, after = 0;

for (const [src, [dst, label]] of Object.entries(NAMES)) {
  const from = path.join(SRC, src);
  if (!fs.existsSync(from)) { console.error("找不到：" + src); process.exit(1); }
  const meta = await sharp(from).metadata();
  const srcSize = fs.statSync(from).size;

  await sharp(from)
    .resize({ width: Math.min(MAX_W, meta.width), withoutEnlargement: true })
    .jpeg({ quality: QUALITY, mozjpeg: true })
    .toFile(path.join(DST, dst));

  const out = await sharp(path.join(DST, dst)).metadata();
  const dstSize = fs.statSync(path.join(DST, dst)).size;
  before += srcSize; after += dstSize;
  rows.push({ label, file: dst,
    from: `${meta.width}×${meta.height} ${(srcSize / 1024).toFixed(0)}K`,
    to:   `${out.width}×${out.height} ${(dstSize / 1024).toFixed(0)}K` });
}

const w = Math.max(...rows.map(r => r.label.length));
for (const r of rows)
  console.log(`${r.label.padEnd(w)}  ${r.file.padEnd(30)} ${r.from.padStart(18)} → ${r.to}`);
console.log(`\n共 ${rows.length} 張，${(before / 1048576).toFixed(1)}MB → ${(after / 1048576).toFixed(1)}MB`);
