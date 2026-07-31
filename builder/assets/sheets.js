/* ══════════════════════════════════════════════════
   Google 試算表 → 純二維陣列
   輸出格式與 parse.js 的 toBook() 相同：{ sheets, merges }，
   所以解析邏輯完全共用，不必為試算表另寫一套。
   ══════════════════════════════════════════════════ */

const API = "https://sheets.googleapis.com/v4/spreadsheets";

/* 從各種形式的網址取出試算表 ID */
export function sheetId(input){
  const t = String(input || "").trim();
  if (!t) return null;
  const m = t.match(/\/spreadsheets\/d\/([A-Za-z0-9_-]{20,})/);
  if (m) return m[1];
  /* 只貼 ID 也接受 */
  if (/^[A-Za-z0-9_-]{20,}$/.test(t)) return t;
  return null;
}

function explain(status, body){
  const msg = (body && body.error && body.error.message) || "";
  const reason = (body && body.error && body.error.status) || "";
  if (status === 400){
    /* 最常見的狀況：Drive 裡的檔案還是 .xlsx，只是用試算表介面開著。
       Sheets API 讀不了 Office 檔，必須先轉檔。 */
    if (/must not be an Office file|not supported for this document/i.test(msg))
      return "這份檔案還是 Excel（.xlsx）格式，不是 Google 試算表——Sheets API 讀不了。" +
             "請在試算表裡點 檔案 → 另存為 Google 試算表，" +
             "然後把「新產生的那份」重新設成公開連結，並改用它的網址（網址會變）。";
    if (/Unable to parse range/i.test(msg))
      return `工作表名稱有問題，可能含有特殊字元。${msg}`;
    return `Google 不接受這個請求。${msg}`;
  }
  if (status === 403){
    if (/API has not been used|is disabled/i.test(msg))
      return "這個 Google Cloud 專案還沒啟用 Sheets API。到 Google Cloud Console → API 和服務 → 啟用「Google Sheets API」。";
    if (/API key not valid/i.test(msg))
      return "API 金鑰無效。確認有複製完整，而且金鑰的 API 限制有包含 Google Sheets API。";
    return "沒有權限讀取。請把試算表設成「知道連結的任何人」→「檢視者」。";
  }
  if (status === 404)
    return "找不到這份試算表。確認網址正確，而且它是 Google 試算表（不是還沒轉檔的 .xlsx）。";
  if (status === 429)
    return "呼叫太頻繁被暫時擋住，等一分鐘再試。";
  return `Google 回傳 ${status}${msg ? "：" + msg : ""}`;
}

async function get(url){
  const r = await fetch(url);
  let body = null;
  try { body = await r.json(); } catch {}
  if (!r.ok){
    const e = new Error(explain(r.status, body));
    e.status = r.status;
    throw e;
  }
  return body;
}

const col = n => {
  let s = "";
  n = Number(n) + 1;
  while (n > 0){ const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = Math.floor((n - 1) / 26); }
  return s;
};

/**
 * 回傳 { sheets:{ 工作表名: 二維陣列 }, merges:{ 工作表名: ["A5:A8", …] }, title }
 */
export async function fetchSheets(idOrUrl, apiKey, onStep){
  const id = sheetId(idOrUrl);
  if (!id) throw new Error("看不出試算表網址。請從瀏覽器位址欄整段複製，格式像 https://docs.google.com/spreadsheets/d/xxxx/edit");
  if (!apiKey) throw new Error("還沒填 Google API 金鑰。");

  /* 1. 工作表清單與合併儲存格 */
  onStep && onStep("讀取工作表清單…");
  const meta = await get(
    `${API}/${id}?fields=properties(title),sheets(properties(title,sheetId),merges)&key=${encodeURIComponent(apiKey)}`);

  const names = (meta.sheets || []).map(s => s.properties.title);
  if (!names.length) throw new Error("這份試算表裡沒有任何工作表。");

  const merges = {};
  for (const s of meta.sheets || []){
    merges[s.properties.title] = (s.merges || []).map(m =>
      `${col(m.startColumnIndex)}${m.startRowIndex + 1}:${col(m.endColumnIndex - 1)}${m.endRowIndex}`);
  }

  /* 2. 一次抓完所有工作表的值。
        FORMATTED_VALUE 回傳「畫面上看到的字串」，日期會是 2026/2/25、
        時間會是 19:55，剛好避開 Excel 序號與時區的所有陷阱。 */
  onStep && onStep(`讀取 ${names.length} 個工作表的內容…`);
  const ranges = names.map(n => `ranges=${encodeURIComponent("'" + n.replace(/'/g, "''") + "'")}`).join("&");
  const data = await get(
    `${API}/${id}/values:batchGet?${ranges}` +
    `&valueRenderOption=FORMATTED_VALUE&majorDimension=ROWS&key=${encodeURIComponent(apiKey)}`);

  const sheets = {};
  (data.valueRanges || []).forEach((vr, i) => {
    sheets[names[i]] = (vr.values || []).map(row => row.map(v => (v === "" ? null : v)));
  });

  return { sheets, merges, title: meta.properties && meta.properties.title, id };
}
