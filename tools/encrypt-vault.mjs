#!/usr/bin/env node
/* 把門鎖密碼、訂單編號這類私人資料加密後放進行程網站。

   repo 與 GitHub Pages 都是公開的，所以網站上只能放「密文」：
   在手機上輸入通關密語，才在那台裝置本機解開，離線也能用。
   明文只存在本機的 <行程>/<行程>-vault.json，已列入 .gitignore。

   用法：
     node tools/encrypt-vault.mjs fukuoka-2026
         讀 fukuoka-2026/fukuoka-2026-vault.json，加密後放進 fukuoka-2026/index.html。
         明文檔的 passphrase 留空會自動產生一組，並寫回明文檔。
     node tools/encrypt-vault.mjs --block 明文.json
         只把加密後的 block 印到 stdout，不改任何檔案（測試用）。

   用 builder 重新產生網站時 TRIP_DATA 會整段換掉，私人區塊也會跟著消失——
   重新產生之後要再跑一次這支。明文刻意不放進 Excel：Excel 會上傳到雲端硬碟分享。

   寫完會做三道檢查，任一道失敗就以非零結束：
     · 重新序列化的格式要跟原檔一致，否則 diff 會變成整段重寫
     · 寫進去的密文要能用同一組密語解回原本的內容
     · 明文裡的每個值、網址與密語本身，都不能出現在 index.html 任何地方 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const subtle = globalThis.crypto.subtle;
const getRandomValues = a => globalThis.crypto.getRandomValues(a);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/* 金鑰推導的參數。網站端讀 block 上記錄的值，所以之後調高也不會讓舊密文打不開。
   60 萬次是 OWASP 對 PBKDF2-SHA256 的建議值；手機上大約要一兩秒。 */
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

async function seal(src){
  const payload = { sections: src.sections };
  const salt = getRandomValues(new Uint8Array(16));
  const iv   = getRandomValues(new Uint8Array(12));
  const key  = await deriveKey(src.passphrase, salt, ITERATIONS, "encrypt");
  const data = await subtle.encrypt({ name:"AES-GCM", iv }, key,
                                    new TextEncoder().encode(JSON.stringify(payload)));
  const block = { type:"vault", key: src.key || "vault", title: src.title || "🔒 私人資料" };
  if (src.notes && src.notes.length) block.notes = src.notes;
  return { ...block, v:1, iterations:ITERATIONS, salt:b64(salt), iv:b64(iv), data:b64(new Uint8Array(data)) };
}

async function open(block, pass){
  const key = await deriveKey(pass, unb64(block.salt), block.iterations, "decrypt");
  const plain = await subtle.decrypt({ name:"AES-GCM", iv:unb64(block.iv) }, key, unb64(block.data));
  return JSON.parse(new TextDecoder().decode(plain));
}

function readSource(file){
  const src = JSON.parse(fs.readFileSync(file, "utf8"));
  if (!Array.isArray(src.sections) || !src.sections.length)
    throw new Error(`${file} 沒有 sections`);
  for (const s of src.sections){
    if (!s.title || !Array.isArray(s.items)) throw new Error("每個 section 都要有 title 與 items");
    for (const it of s.items){
      if (it.url && !/^https?:\/\//i.test(it.url)) throw new Error(`網址必須是 http(s)：${it.label || ""}`);
      if (!it.value && !it.url && !it.note) throw new Error(`「${s.title}」有一項沒有 value、url 或 note`);
    }
  }
  return src;
}

/* 要確認沒外洩的字串：每個值、網址與備註的完整內容、值裡面含數字的長字串（訂單編號、密碼）、
   以及密語本身。標籤（「訂單編號」「房間」）不是機密，網站別處本來就會出現，不檢查。 */
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

/* ── --block：只輸出密文（測試用） ── */
if (process.argv[2] === "--block"){
  const src = readSource(process.argv[3]);
  if (!src.passphrase) throw new Error("--block 模式的明文檔必須自己填 passphrase");
  process.stdout.write(JSON.stringify(await seal(src)));
  process.exit(0);
}

/* ── 一般模式：加密並放進網站 ── */
const TRIP = process.argv[2];
if (!TRIP){ console.error("用法：node tools/encrypt-vault.mjs <行程資料夾>"); process.exit(2); }
const SITE = path.join(ROOT, TRIP, "index.html");
const SRC  = path.join(ROOT, TRIP, `${TRIP}-vault.json`);
if (!fs.existsSync(SRC)){ console.error(`找不到明文檔 ${SRC}`); process.exit(2); }

const src = readSource(SRC);
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

const dayId = src.day || "info";
const day = T.days.find(d => d.id === dayId);
if (!day){ console.error(`找不到分頁 ${dayId}`); process.exit(1); }
const key = src.key || "vault";

/* 內容沒變就不重寫：每次加密的 salt 與 iv 都不同，重跑會讓 diff 無故變動 */
const old = T.days.flatMap(d => d.blocks || []).find(b => b.type === "vault" && b.key === key);
let unchanged = false;
if (old && old.iterations === ITERATIONS && old.title === (src.title || "🔒 私人資料")
    && same(old.notes || [], src.notes || [])){
  try { unchanged = same(await open(old, src.passphrase), { sections: src.sections }); } catch {}
}

if (unchanged){
  console.log("私人區塊內容沒有變動，不重寫。");
} else {
  for (const d of T.days) d.blocks = (d.blocks || []).filter(b => !(b.type === "vault" && b.key === key));
  day.blocks.unshift(await seal(src));
  fs.writeFileSync(SITE, html.slice(0, start) + JSON.stringify(T, null, 2) + html.slice(end));
}

/* ── 檢查 ── */
const written = fs.readFileSync(SITE, "utf8");
const back = JSON.parse(findTripData(written).raw)
  .days.flatMap(d => d.blocks || []).find(b => b.type === "vault" && b.key === key);
let ok = true;
try {
  if (!same(await open(back, src.passphrase), { sections: src.sections })){ ok = false; console.error("FAIL 解回來的內容不一致"); }
} catch { ok = false; console.error("FAIL 寫進去的密文解不開"); }

const leaks = secretsOf(src).filter(s => written.includes(s));
if (leaks.length){ ok = false; console.error("FAIL 明文出現在 index.html：" + leaks.map(mask).join("、")); }

const items = src.sections.reduce((n, s) => n + s.items.length, 0);
console.log(`${ok ? "OK" : "FAIL"}  ${src.sections.length} 個區段、${items} 項，放在「${day.nav}」分頁最上面。`);
console.log(`     解回檢查${ok ? "通過" : "失敗"}；明文外洩掃描 ${leaks.length ? "有 " + leaks.length + " 筆" : "乾淨"}（${secretsOf(src).length} 個字串）。`);
if (generated){
  console.log(`\n已產生新的通關密語，並存進 ${path.relative(ROOT, SRC)} 的 passphrase 欄位：`);
  console.log(`  ${src.passphrase}`);
}
process.exit(ok ? 0 : 1);
