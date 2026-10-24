#!/usr/bin/env node
/* 把門鎖密碼、訂單編號這類私人資料加密後，掛到行程網站對應的卡片上。

   repo 與 GitHub Pages 都是公開的，所以網站上只能放「密文」：
   TRIP_DATA.vault 是整包密文，卡片只帶一個 secret 鍵與按鈕上的提示文字。
   在手機上輸入通關密語，才在那台裝置本機解開，每張卡片換成自己那一段，離線也能用。
   明文只存在本機的 <行程>/<行程>-vault.json，已列入 .gitignore。

   明文檔格式：
     { "passphrase": "…",                        ← 留空會自動產生一組並寫回
       "sections": [
         { "key": "origo", "day": "d1", "card": "HOTEL ORIGO HAKATA - Gion -",
           "hint": "預訂號碼與驗證碼",             ← 鎖著時按鈕上的字，會公開
           "items": [ { "label": "預訂號碼", "value": "…" },
                      { "label": "入住指南", "url": "https://…" },
                      { "note": "…" } ] } ] }
   day 是 TRIP_DATA.days 的 id，card 用卡片標題比對。

   用法：
     node tools/encrypt-vault.mjs fukuoka-2026
     node tools/encrypt-vault.mjs --seal 明文.json    只印出密文（TRIP_DATA.vault），測試用

   卡片上的 secret 鍵會跟著行程區塊一起被重新產生掉，之後要再跑一次：
     · 用 builder 重新產生網站（TRIP_DATA 整段換掉）
     · 任何會重寫行程區塊的步驟
   明文刻意不放進 Excel：Excel 會上傳到雲端硬碟分享。

   寫完會做幾道檢查，任一道失敗就以非零結束：
     · 重新序列化的格式要跟原檔一致，否則 diff 會變成整段重寫
     · 每一段都要對得到剛好一張卡片
     · 密文要能用同一組密語解回原本的內容
     · 明文裡的值、網址、備註與密語本身，都不能出現在 index.html 任何地方 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const subtle = globalThis.crypto.subtle;
const getRandomValues = a => globalThis.crypto.getRandomValues(a);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/* 金鑰推導的參數。網站端讀密文上記錄的值，所以之後調高也不會讓舊密文打不開。
   60 萬次是 OWASP 對 PBKDF2-SHA256 的建議值；手機上大約要零點幾秒到一兩秒。 */
const ITERATIONS = 600000;

/* 通關密語正規化：大小寫、空白、連字號都不計較。
   手機鍵盤會自動把第一個字母改成大寫，連字號也有全形半形之分，
   打錯這些不該被當成密語錯誤。網站端 site-template.html 用的是同一條規則。 */
const normPass = p => String(p ?? "").normalize("NFKC").toLowerCase()
  .replace(/[\s\-‐-―]/g, "");

/* 產生的密語避開 0/o、1/l/i 這類容易看錯的字，16 字約 79 bits。 */
const ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789";
function newPassphrase(){
  const out = [];
  while (out.length < 16){
    const [b] = getRandomValues(new Uint8Array(1));
    if (b < 248) out.push(ALPHABET[b % ALPHABET.length]);   // 248 = 31×8，丟掉尾數避免偏差
  }
  return out.join("").match(/.{4}/g).join("-");
}

const b64 = u8 => Buffer.from(u8).toString("base64");
const unb64 = s => new Uint8Array(Buffer.from(s, "base64"));

async function deriveKey(pass, salt, iterations, usage){
  const base = await subtle.importKey("raw", new TextEncoder().encode(normPass(pass)),
                                      "PBKDF2", false, ["deriveKey"]);
  return subtle.deriveKey({ name:"PBKDF2", hash:"SHA-256", salt, iterations },
                          base, { name:"AES-GCM", length:256 }, false, [usage]);
}

/* 密文裡只放各段的內容；哪張卡片、按鈕上寫什麼是公開的，留在卡片上 */
const payloadOf = src => ({ sections: Object.fromEntries(src.sections.map(s => [s.key, { items: s.items }])) });

async function seal(src){
  const salt = getRandomValues(new Uint8Array(16));
  const iv   = getRandomValues(new Uint8Array(12));
  const key  = await deriveKey(src.passphrase, salt, ITERATIONS, "encrypt");
  const data = await subtle.encrypt({ name:"AES-GCM", iv }, key,
                                    new TextEncoder().encode(JSON.stringify(payloadOf(src))));
  return { v:2, iterations:ITERATIONS, salt:b64(salt), iv:b64(iv), data:b64(new Uint8Array(data)) };
}

async function open(vault, pass){
  const key = await deriveKey(pass, unb64(vault.salt), vault.iterations, "decrypt");
  const plain = await subtle.decrypt({ name:"AES-GCM", iv:unb64(vault.iv) }, key, unb64(vault.data));
  return JSON.parse(new TextDecoder().decode(plain));
}

function readSource(file, needCard){
  const src = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!Array.isArray(src.sections) || !src.sections.length) throw new Error(`${file} 沒有 sections`);
  const keys = new Set();
  for (const s of src.sections){
    if (!/^[A-Za-z0-9_-]+$/.test(s.key || "")) throw new Error("每一段都要有 key（英數字、底線或連字號）");
    if (keys.has(s.key)) throw new Error(`key 重複：${s.key}`);
    keys.add(s.key);
    if (needCard && (!s.day || !s.card)) throw new Error(`「${s.key}」要填 day 與 card`);
    if (!Array.isArray(s.items) || !s.items.length) throw new Error(`「${s.key}」沒有 items`);
    for (const it of s.items){
      if (it.url && !/^https?:\/\//i.test(it.url)) throw new Error(`網址必須是 http(s)：${it.label || ""}`);
      if (!it.value && !it.url && !it.note) throw new Error(`「${s.key}」有一項沒有 value、url 或 note`);
    }
  }
  return src;
}

/* 要確認沒外洩的字串：每個值、網址與備註的完整內容、值裡面含數字的長字串（訂單編號、密碼）、
   以及密語本身。標籤與按鈕提示（「訂單編號」「房間」）本來就公開，不檢查。 */
function secretsOf(src){
  const out = new Set();
  const add = s => { s = String(s || "").trim(); if (s.length >= 4) out.add(s); };
  for (const s of src.sections) for (const it of s.items){
    add(it.value); add(it.url); add(it.note);
    for (const tok of String(it.value || "").match(/[A-Za-z0-9]{4,}/g) || [])
      if (/\d/.test(tok)) add(tok);
  }
  add(src.passphrase); add(normPass(src.passphrase));
  return [...out];
}
const mask = s => s.slice(0, 2) + "…（" + s.length + " 字）";

function findTripData(html){
  const s = html.indexOf("const TRIP_DATA = ");
  if (s < 0) throw new Error("找不到 TRIP_DATA");
  const start = html.indexOf("{", s);
  let d = 0, end = -1;
  for (let i = start; i < html.length; i++){
    if (html[i] === "{") d++;
    else if (html[i] === "}"){ d--; if (!d){ end = i + 1; break; } }
  }
  if (end < 0) throw new Error("找不到 TRIP_DATA 的結尾");
  return { start, end, raw: html.slice(start, end) };
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/* ── --seal：只輸出密文（測試用） ── */
if (process.argv[2] === "--seal"){
  const src = readSource(process.argv[3], false);
  if (!src.passphrase) throw new Error("--seal 模式的明文檔必須自己填 passphrase");
  process.stdout.write(JSON.stringify(await seal(src)));
  process.exit(0);
}

/* ── 一般模式：加密並掛到卡片上 ── */
const TRIP = process.argv[2];
if (!TRIP){ console.error("用法：node tools/encrypt-vault.mjs <行程資料夾>"); process.exit(2); }
const SITE = path.join(ROOT, TRIP, "index.html");
const SRC  = path.join(ROOT, TRIP, `${TRIP}-vault.json`);
if (!fs.existsSync(SRC)){ console.error(`找不到明文檔 ${SRC}`); process.exit(2); }

const src = readSource(SRC, true);
let generated = false;
if (!src.passphrase){
  src.passphrase = newPassphrase();
  fs.writeFileSync(SRC, JSON.stringify(src, null, 2) + "\n");
  generated = true;
}

const html = fs.readFileSync(SITE, "utf8");
const { start, end, raw } = findTripData(html);
const T = JSON.parse(raw);
if (JSON.stringify(T, null, 2) !== raw){
  console.error("TRIP_DATA 的格式跟 JSON.stringify(…, null, 2) 不一致，停止——寫回去會改寫整段。");
  process.exit(1);
}

/* 卡片標記：先全部清掉再重掛，明文檔裡刪掉的段落才不會在卡片上留下空按鈕 */
for (const d of T.days) for (const b of d.blocks || []){ delete b.secret; delete b.secretHint; }
const where = [], missing = [];
for (const s of src.sections){
  const day = T.days.find(d => d.id === s.day);
  const hits = (day ? day.blocks || [] : [])
    .filter(b => (b.type === "stop" || b.type === "lodging") && b.title === s.card);
  if (hits.length !== 1){ missing.push(`${s.day}「${s.card}」找到 ${hits.length} 張`); continue; }
  hits[0].secret = s.key;
  if (s.hint) hits[0].secretHint = s.hint;
  where.push(`${day.nav}・${s.card}`);
}
if (missing.length){ console.error("對不到卡片：\n  " + missing.join("\n  ")); process.exit(1); }

/* 舊版把私人資料集中在一張 type:"vault" 的卡片，改成掛在原卡片後就不用了 */
for (const d of T.days) d.blocks = (d.blocks || []).filter(b => b.type !== "vault");

/* 內容沒變就沿用原本的密文：每次加密的 salt 與 iv 都不同，重跑會讓 diff 無故變動 */
let reused = false;
if (T.vault && T.vault.iterations === ITERATIONS){
  try { reused = same(await open(T.vault, src.passphrase), payloadOf(src)); } catch {}
}
if (!reused) T.vault = await seal(src);

const out = html.slice(0, start) + JSON.stringify(T, null, 2) + html.slice(end);
if (out !== html) fs.writeFileSync(SITE, out);

/* ── 檢查 ── */
const written = fs.readFileSync(SITE, "utf8");
const back = JSON.parse(findTripData(written).raw);
let ok = true;
try {
  if (!same(await open(back.vault, src.passphrase), payloadOf(src))){ ok = false; console.error("FAIL 解回來的內容不一致"); }
} catch { ok = false; console.error("FAIL 寫進去的密文解不開"); }

const marked = back.days.flatMap(d => d.blocks || []).filter(b => b.secret).length;
if (marked !== src.sections.length){ ok = false; console.error(`FAIL 卡片標記 ${marked} 張，應為 ${src.sections.length} 張`); }

const leaks = secretsOf(src).filter(s => written.includes(s));
if (leaks.length){ ok = false; console.error("FAIL 明文出現在 index.html：" + leaks.map(mask).join("、")); }

const items = src.sections.reduce((n, s) => n + s.items.length, 0);
console.log(`${ok ? "OK" : "FAIL"}  ${src.sections.length} 段、${items} 項，掛在：`);
for (const w of where) console.log(`       · ${w}`);
console.log(`     ${reused ? "內容沒變，沿用原本的密文" : "已重新加密"}；解回檢查${ok ? "通過" : "失敗"}；` +
            `明文外洩掃描${leaks.length ? "有 " + leaks.length + " 筆" : "乾淨"}（${secretsOf(src).length} 個字串）。`);
if (generated){
  console.log(`\n已產生新的通關密語，並存進 ${path.relative(ROOT, SRC)} 的 passphrase 欄位：`);
  console.log(`  ${src.passphrase}`);
}
process.exit(ok ? 0 : 1);
