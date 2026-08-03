/* ══════════════════════════════════════════════════
   Excel → TRIP_DATA 解析器
   瀏覽器與 Node 共用。輸入 SheetJS 的 workbook 物件，
   回傳 { data, errors, warnings }。
   ══════════════════════════════════════════════════ */

const SHEETS = {
  meta:  "基本設定",
  member:"成員",
  plan:  "行程",
  flight:"航班",
  pack:  "打包清單",
  phrase:"常用片語",
  sos:   "緊急資訊"
};

const TYPE_MAP = {
  "景點":  { type:"stop",    kind:"sight" },
  "餐飲":  { type:"stop",    kind:"meal"  },
  "購物":  { type:"stop",    kind:"shop"  },
  "交通":  { type:"transit" },
  "住宿":  { type:"lodging" },
  "二選一": { type:"options"  }
};
const SUB_TYPES = ["→起終點", "→經過站", "→選項", "→連結"];
const TYPE_ZH = { stop:"景點", transit:"交通", lodging:"住宿",
                  options:"二選一", flight:"航班" };
const zh = t => TYPE_ZH[t] || t;

/* 連結樣式：舊的 plain 一律視為 theme，不認得的也退回 theme */
const LINK_STYLES = ["theme", "map", "naver", "booking"];

/* 交通方式 → Google Maps URLs 的 travelmode 參數 */
export const TRAVEL_MODES = {
  "大眾運輸":"transit", "大眾交通":"transit", "捷運":"transit", "地鐵":"transit",
  "公車":"transit", "電車":"transit", "火車":"transit", "transit":"transit",
  "自駕":"driving", "開車":"driving", "租車":"driving", "計程車":"driving",
  "taxi":"driving", "driving":"driving",
  "步行":"walking", "走路":"walking", "walking":"walking",
  "單車":"bicycling", "腳踏車":"bicycling", "自行車":"bicycling", "bicycling":"bicycling"
};
export const MODE_ZH = { transit:"大眾運輸", driving:"自駕", walking:"步行", bicycling:"單車" };
const normMode = v => TRAVEL_MODES[String(v || "").trim().toLowerCase()]
                   || TRAVEL_MODES[String(v || "").trim()] || null;

/* 停留時間：只吃分鐘數。也容忍「90分」「1.5hr」這類寫法。 */
function toMinutes(v){
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "number") return v < 1 ? Math.round(v * 1440) : Math.round(v);
  const t = String(v).trim();
  let m = /^(\d+(?:\.\d+)?)\s*(?:小時|時|hr|h)$/i.exec(t);
  if (m) return Math.round(Number(m[1]) * 60);
  m = /^(\d+)\s*:\s*(\d{2})$/.exec(t);
  if (m) return Number(m[1]) * 60 + Number(m[2]);
  m = /^(\d+(?:\.\d+)?)/.exec(t);
  return m ? Math.round(Number(m[1])) : null;
}
export const fmtMinutes = n => {
  if (!n) return "";
  if (n < 60) return `${n} 分`;
  const h = Math.floor(n / 60), r = n % 60;
  return r ? `${h} 小時 ${r} 分` : `${h} 小時`;
};
const addMinutes = (hhmm, min) => {
  const m = /^(\d{1,2}):(\d{2})/.exec(hhmm || "");
  if (!m) return "";
  const t = Number(m[1]) * 60 + Number(m[2]) + (min || 0);
  const pad = n => String(n).padStart(2, "0");
  return `${pad(Math.floor(t / 60) % 24)}:${pad(t % 60)}`;
};
const STYLE_ALIAS = { plain:"theme", klook:"booking", kkday:"booking", ticket:"booking" };
const normStyle = v => {
  const t = String(v || "").trim().toLowerCase();
  if (!t) return "theme";
  const a = STYLE_ALIAS[t] || t;
  return LINK_STYLES.includes(a) ? a : "theme";
};

const s = v => (v === null || v === undefined) ? "" : String(v).trim();
const lines = v => s(v) ? s(v).split(/\r?\n/).map(x => x.trim()).filter(Boolean) : [];

/* Excel 可能把日期存成序號或 Date 物件，統一成 YYYY-MM-DD */
function toDate(v){
  if (!v && v !== 0) return "";
  if (v instanceof Date){
    // Excel 的日期沒有時間概念。有些環境讀成 UTC 午夜，有些讀成當地午夜，
    // 直接用 toISOString() 會在 UTC+8 差一天，所以挑落在午夜的那一種解讀。
    const pad = n => String(n).padStart(2, "0");
    const useUTC = v.getUTCHours() === 0 && v.getUTCMinutes() === 0;
    return useUTC
      ? `${v.getUTCFullYear()}-${pad(v.getUTCMonth()+1)}-${pad(v.getUTCDate())}`
      : `${v.getFullYear()}-${pad(v.getMonth()+1)}-${pad(v.getDate())}`;
  }
  if (typeof v === "number"){
    const ms = Math.round((v - 25569) * 86400 * 1000);
    return new Date(ms).toISOString().slice(0, 10);
  }
  const t = s(v).replace(/\//g, "-");
  const m = t.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  return m ? `${m[1]}-${m[2].padStart(2,"0")}-${m[3].padStart(2,"0")}` : t;
}

/* 時間可能是 Excel 的小數，轉成 HH:MM */
function toTime(v){
  if (v === null || v === undefined || v === "") return "";
  if (typeof v === "number" && v < 1){
    const mins = Math.round(v * 24 * 60);
    return `${String(Math.floor(mins/60)).padStart(2,"0")}:${String(mins%60).padStart(2,"0")}`;
  }
  if (v instanceof Date){
    // Excel 的時間存成 1899-12-30 的某個時刻，且為 UTC。
    // 用 getHours() 會加上瀏覽器時區，台北會整整偏移 8 小時。
    const pad = n => String(n).padStart(2, "0");
    return `${pad(v.getUTCHours())}:${pad(v.getUTCMinutes())}`;
  }
  return s(v);
}

function fixUrl(u){
  const t = s(u);
  if (!t) return "";
  return /^https?:\/\//i.test(t) ? t : "https://" + t.replace(/^\/+/, "");
}

/* 找出標題列（含指定關鍵字的那一列） */
function table(grid, keyword){
  if (!grid || !grid.length) return null;
  const hi = grid.findIndex(r => (r || []).some(c => s(c) === keyword));
  if (hi < 0) return null;
  const head = (grid[hi] || []).map(s);
  const rows = grid.slice(hi + 1)
    .filter(r => (r || []).some(c => s(c) !== ""))
    .map(r => {
      const o = {};
      head.forEach((h, i) => { if (h) o[h] = r ? r[i] : null; });
      return o;
    });
  return { head, rows, headerIndex: hi };
}

export function parseGrids(book){
  const grid = n => book.sheets[n] || null;
  const errors = [], warnings = [];
  const err  = (sheet, row, msg) => errors.push({ sheet, row, msg });
  const warn = (sheet, row, msg) => warnings.push({ sheet, row, msg });

  /* 缺少工作表：必填的缺了就無法繼續 */
  let fatal = false;
  for (const [k, name] of Object.entries(SHEETS)){
    if (!book.sheets[name]){
      if (["meta","member","plan","flight"].includes(k)){ err(name, null, "找不到這張工作表"); fatal = true; }
      else warn(name, null, "找不到這張工作表，會略過");
    }
  }
  if (fatal) return { data: null, errors, warnings };

  /* 合併儲存格：記錄下來但繼續解析，好讓其他問題也一次列出 */
  for (const name of Object.values(SHEETS)){
    const mg = (book.merges || {})[name] || [];
    if (mg.length && name !== SHEETS.meta){
      err(name, null, `有 ${mg.length} 處合併儲存格（${mg.slice(0,3).join("、")}），請先取消合併`);
    }
  }

  /* ── 基本設定 ────────────────────────── */
  const metaT = table(grid(SHEETS.meta), "項目");
  if (!metaT) { err(SHEETS.meta, null, "找不到「項目」標題列"); return { data:null, errors, warnings }; }
  const kv = {};
  metaT.rows.forEach(r => { if (s(r["項目"])) kv[s(r["項目"])] = r["填寫內容"]; });

  const need = k => { const v = kv[k]; if (!s(v)) err(SHEETS.meta, null, `「${k}」沒有填`); return v; };
  const rawRate = s(kv["匯率"]);
  let rate = null;
  if (rawRate){
    rate = Number(rawRate.replace(/[^\d.]/g, ""));
    if (!rate || !isFinite(rate)){
      err(SHEETS.meta, null, `「匯率」看不懂（${rawRate}）。只填數字，或整格留空讓系統自動查。`);
      rate = null;
    } else if (!/^\d*\.?\d+$/.test(rawRate)){
      warn(SHEETS.meta, null, `「匯率」含有非數字的字（${rawRate}），已當成 ${rate} 使用。`);
    }
  }

  const meta = {
    tripId:   s(need("行程代號")).toLowerCase().replace(/[^a-z0-9-]/g, "-"),
    title:    s(need("網站標題")),
    subtitle: s(kv["副標題"]),
    theme:    (s(kv["主題"]) || "auto").toLowerCase(),
    startDate: toDate(need("出發日")),
    endDate:   toDate(kv["回程日"]),
    place: {
      name:      s(need("城市（英文）")),
      nameLocal: s(kv["城市（當地語）"]),
      lat:  kv["緯度"] === null || s(kv["緯度"]) === "" ? null : Number(kv["緯度"]),
      lon:  kv["經度"] === null || s(kv["經度"]) === "" ? null : Number(kv["經度"]),
      timezone: s(kv["時區"]) || null
    },
    travelMode: normMode(kv["交通方式"]) || "transit",
    money: { home: (s(kv["本國幣別"]) || "TWD").toUpperCase(),
             local: s(kv["當地幣別"]).toUpperCase(), rate, rateDate: null },
    members: [],
    categories: ["食","衣","住","行","玩"]
  };
  if (!/^\d{4}-\d{2}-\d{2}$/.test(meta.startDate))
    err(SHEETS.meta, null, `「出發日」格式不對（${meta.startDate}），請用 2026-02-25`);

  /* ── 成員 ────────────────────────────── */
  const memT = table(grid(SHEETS.member), "姓名");
  meta.members = memT ? memT.rows.map(r => s(r["姓名"])).filter(Boolean) : [];
  if (meta.members.length < 2) err(SHEETS.member, null, "至少要有 2 位成員");
  if (new Set(meta.members).size !== meta.members.length) err(SHEETS.member, null, "有重複的姓名");

  /* ── 行程 ────────────────────────────── */
  const planT = table(grid(SHEETS.plan), "類型");
  const dayMap = new Map();
  let lastBlock = null, lastDay = null, maxDaySeen = 0;
  let curMode = meta.travelMode;      /* 填一次之後往下沿用，直到再次填寫 */
  /* Excel 的公式通常已經算好時間，但公式可能被刪掉或插入新列時沒帶到，
     所以這裡再算一次當備援。同一天內累加，換天就重設。 */
  let clock = null, clockDay = null;

  /* 每一天的日期一律由出發日推算，使用者不用填，也就不會填錯 */
  const dateOf = n => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(meta.startDate)) return "";
    const t = Date.parse(meta.startDate + "T00:00:00Z") + (n - 1) * 86400000;
    return new Date(t).toISOString().slice(0, 10);
  };

  if (planT) planT.rows.forEach((r, i) => {
    const rowNo = planT.headerIndex + 2 + i;      // Excel 上看到的列號
    const type  = s(r["類型"]);
    if (!type) return;

    const dayNo = s(r["天"]) ? Number(r["天"]) : lastDay;
    if (!dayNo){ err(SHEETS.plan, rowNo, "沒有填「天」，也接不到上一列"); return; }
    const prevDay = lastDay;
    lastDay = dayNo;
    if (s(r["交通方式"])){
      const m = normMode(r["交通方式"]);
      if (!m) err(SHEETS.plan, rowNo,
        `不認得的交通方式「${s(r["交通方式"])}」。可填：大眾運輸、自駕、步行、單車`);
      else curMode = m;
    }

    if (!dayMap.has(dayNo)){
      if (dayNo < maxDaySeen)
        warn(SHEETS.plan, rowNo, `第 ${dayNo} 天出現在第 ${maxDaySeen} 天後面，順序看起來亂了。建議照天數由小到大排。`);
      maxDaySeen = Math.max(maxDaySeen, dayNo);
      dayMap.set(dayNo, { id:`d${dayNo}`, nav:"", date: dateOf(dayNo), blocks:[] });
    } else if (dayNo !== prevDay && dayNo < maxDaySeen){
      warn(SHEETS.plan, rowNo,
        `這一列的「天」是 ${dayNo}，但前一列是第 ${prevDay} 天。已幫你歸到第 ${dayNo} 天並依時間排好，` +
        `不過建議在 Excel 裡也把它移到同一段，之後比較好維護。`);
    }
    const day = dayMap.get(dayNo);
    if (s(r["分頁名稱"])){
      const label = s(r["分頁名稱"]).replace(/^D\s*\d+\s*/i, "");   // 使用者若手打 D1 也吃得下
      if (day.nav && day.nav !== label)
        err(SHEETS.plan, rowNo, `第 ${dayNo} 天有兩個不同的分頁名稱（「${day.nav}」與「${label}」）`);
      day.nav = label;
    }

    const links = [];
    if (s(r["連結網址"]) || s(r["連結文字"])){
      if (!s(r["連結網址"])) warn(SHEETS.plan, rowNo, "有連結文字但沒有網址，會略過");
      else links.push({
        label: s(r["連結文字"]) || "開啟連結",
        url:   fixUrl(r["連結網址"]),
        style: normStyle(s(r["連結樣式"]))
      });
    }

    /* 從屬列 */
    if (SUB_TYPES.includes(type)){
      if (!lastBlock){ err(SHEETS.plan, rowNo, `「${type}」前面沒有可以附屬的區塊`); return; }
      const note = s(r["說明／備註"]);
      if (type === "→起終點" || type === "→經過站"){
        if (lastBlock.type !== "transit"){
          err(SHEETS.plan, rowNo, `「${type}」只能接在「交通」後面，目前接在「${zh(lastBlock.type)}」`); return;
        }
        const st = { name: s(r["標題"]), nameLocal: s(r["當地語"]), active: type === "→起終點" };
        if (note.startsWith("⚠")) st.note = note.replace(/^⚠️?\s*/, "");
        else if (note) st.exit = note;
        lastBlock.steps.push(st);
      } else if (type === "→選項"){
        if (lastBlock.type !== "options"){
          err(SHEETS.plan, rowNo, `「→選項」只能接在「二選一」後面，目前接在「${zh(lastBlock.type)}」`); return;
        }
        lastBlock.choices.push({
          label: String.fromCharCode(65 + lastBlock.choices.length),
          title: s(r["標題"]), titleLocal: s(r["當地語"]),
          note, links
        });
      } else if (type === "→連結"){
        if (!links.length){ err(SHEETS.plan, rowNo, "「→連結」沒有填網址"); return; }
        (lastBlock.links = lastBlock.links || []).push(...links);
      }
      return;
    }

    /* 主要區塊 */
    const def = TYPE_MAP[type];
    if (!def){
      err(SHEETS.plan, rowNo,
        `不認得的類型「${type}」。可用：${Object.keys(TYPE_MAP).join("、")}，或 ${SUB_TYPES.join("、")}`);
      return;
    }
    const bt = def.type;

    const stay = toMinutes(r["停留(分)"] ?? r["停留"] ?? r["停留時間"]);
    let time = toTime(r["時間"]);
    if (clockDay !== dayNo){ clock = null; clockDay = dayNo; }   /* 換天重設 */
    if (time) clock = time;
    else if (clock) time = clock;
    if (clock && stay) clock = addMinutes(clock, stay);
    const notes = lines(r["說明／備註"]);
    let b;
    if (bt === "transit"){
      b = { type:"transit", time, title: s(r["標題"]) || "交通",
            line: notes[0] || "", duration: notes[1] || (stay ? `約 ${fmtMinutes(stay)}` : ""),
            steps: [], links, stay, mode: curMode };
    } else if (bt === "lodging"){
      b = { type:"lodging", time, title: s(r["標題"]) || "住宿", titleLocal: s(r["當地語"]),
            address: s(r["地點"]), notes, links, stay, mode: curMode };
    } else if (bt === "options"){
      b = { type:"options", time, title: s(r["標題"]) || "選一個",
            place: s(r["地點"]), placeLocal: "", choices: [], links, stay, mode: curMode };
    } else {
      if (!s(r["標題"])){ err(SHEETS.plan, rowNo, `「${type}」必須填標題`); return; }
      b = { type:"stop", kind: def.kind, time, title: s(r["標題"]), titleLocal: s(r["當地語"]),
            place: s(r["地點"]), notes, links, stay, mode: curMode };
    }
    day.blocks.push(b);
    lastBlock = b;
  });

  /* ── 航班 ────────────────────────────── */
  const flightT = table(grid(SHEETS.flight), "航空公司");
  if (flightT) flightT.rows.forEach((r, i) => {
    const rowNo = flightT.headerIndex + 2 + i;
    const dayNo = Number(s(r["天"]));
    if (!dayNo){ err(SHEETS.flight, rowNo, "沒有填「天」"); return; }
    if (!dayMap.has(dayNo)){ err(SHEETS.flight, rowNo, `第 ${dayNo} 天在「行程」裡不存在`); return; }
    const b = {
      type:"flight", airline: s(r["航空公司"]), code: s(r["航班編號"]),
      from: s(r["出發地"]), fromTerminal: s(r["出發航廈"]),
      to:   s(r["抵達地"]), toTerminal:   s(r["抵達航廈"]),
      date: "", depart: toTime(r["起飛"]), arrive: toTime(r["抵達"])
    };
    if (!b.airline || !b.code || !b.from || !b.to)
      err(SHEETS.flight, rowNo, "航空公司／航班編號／出發地／抵達地 都必須填");
    const day = dayMap.get(dayNo);
    b.date = day.date;
    b._pos = s(r["位置"]) === "當天最後" ? "last" : "first";
    day.blocks.push(b);
  });

  /* ── 打包清單 → 行前分頁 ─────────────── */
  const packT = table(grid(SHEETS.pack), "清單名稱");
  const prepBlocks = [{ type:"weather", days:7 }];
  if (packT){
    const byList = new Map();
    packT.rows.forEach(r => {
      const ln = s(r["清單名稱"]) || "清單";
      const cat = s(r["分類"]) || "項目";
      const item = s(r["項目"]);
      if (!item) return;
      if (!byList.has(ln)) byList.set(ln, new Map());
      const g = byList.get(ln);
      if (!g.has(cat)) g.set(cat, []);
      g.get(cat).push(item);
    });
    let n = 0;
    for (const [ln, groups] of byList){
      prepBlocks.push({
        type:"checklist", key:`list${n++}`, title: ln,
        groups: [...groups].map(([name, items]) => ({ name, items }))
      });
    }
  }

  /* ── 片語與緊急資訊 ──────────────────── */
  const phT = table(grid(SHEETS.phrase), "當地語");
  const phrases = phT ? phT.rows.map(r => ({ local: s(r["當地語"]), zh: s(r["中文"]) }))
                              .filter(p => p.zh) : [];
  const sosT = table(grid(SHEETS.sos), "名稱");
  const emergency = sosT ? sosT.rows.map(r => ({
      label: s(r["名稱"]), value: s(r["內容"]), tel: s(r["電話"]) === "是"
    })).filter(e => e.label && e.value) : [];   // 只有名稱沒有內容的視為填寫提示

  /* ── 組裝 ────────────────────────────── */
  /* ── 每天內部重新排序 ──────────────────────────
     1. 去程航班永遠最前、回程航班永遠最後
     2. 住宿一律排到當天最下面
     3. 其餘依時間先後，沒填時間的跟著上一個有時間的走
     排序後若順序真的變了，告訴使用者是哪一天、哪一項被搬動 */
  const mins = t => {
    const m = /^(\d{1,2}):(\d{2})/.exec(t || "");
    return m ? Number(m[1]) * 60 + Number(m[2]) : null;
  };
  const label = b => b.type === "flight" ? `航班 ${b.code}`
                   : b.type === "transit" ? `交通「${b.title}」`
                   : `「${b.title || b.type}」`;

  for (const [dayNo, day] of dayMap){
    let carry = -1;
    const keyed = day.blocks.map((b, i) => {
      let tier = 1;
      if (b.type === "flight") tier = b._pos === "last" ? 3 : 0;
      else if (b.type === "lodging") tier = 2;
      const t = mins(b.time);
      if (t !== null) carry = t;
      return { b, i, tier, t: t !== null ? t : carry };
    });
    const sorted = [...keyed].sort((x, y) =>
      x.tier - y.tier || x.t - y.t || x.i - y.i);

    /* 航班是最後才從另一張工作表加進來的，它被移到頭尾不算「使用者排錯」。
       只比較非航班項目的相對順序有沒有變。 */
    const seq = arr => arr.filter(k => k.tier !== 0 && k.tier !== 3).map(k => k.i);
    const before = seq(keyed), after = seq(sorted);
    const moved = after
      .map((v, idx) => ({ v, idx }))
      .filter(({ v, idx }) => v !== before[idx])
      .map(({ v }) => ({ k: keyed.find(k => k.i === v) }));
    if (moved.length){
      const names = moved.slice(0, 3).map(({ k }) => label(k.b)).join("、");
      warn(SHEETS.plan, null,
        `第 ${dayNo} 天（${day.nav || "D" + dayNo}）的順序已自動調整：${names}` +
        (moved.length > 3 ? ` 等 ${moved.length} 項` : "") +
        `。住宿一律排在當天最後，其餘照時間先後。`);
    }
    day.blocks = sorted.map(k => { delete k.b._pos; return k.b; });
  }

  const days = [...dayMap.entries()].sort((a,b) => a[0]-b[0]).map(([n, d]) => ({
    ...d,
    nav: `D${n}${d.nav ? " " + d.nav : ""}`      // 使用者只填後半，D 幾由天數自動帶
  }));
  if (!days.length) err(SHEETS.plan, null, "一列行程都沒有");

  const nums = [...dayMap.keys()].sort((a,b)=>a-b);
  for (let i = 1; i < nums.length; i++)
    if (nums[i] !== nums[i-1] + 1)
      warn(SHEETS.plan, null, `天數不連續：第 ${nums[i-1]} 天之後直接跳到第 ${nums[i]} 天`);
  if (nums.length && nums[0] !== 1)
    warn(SHEETS.plan, null, `行程從第 ${nums[0]} 天開始，不是第 1 天`);

  days.forEach(d => {
    const first = d.blocks.find(b => b.type !== "flight");
    if (first && !first.time && d.blocks.some(b => b.stay))
      warn(SHEETS.plan, null,
        `${d.nav} 沒有填當天第一列的「時間」，後面的停留時間就無法累加。`);
    /* 只有大眾運輸才需要列站點；自駕、步行、單車沒有站可列 */
    d.blocks.filter(b => b.type === "transit" && !b.steps.length && b.mode === "transit")
            .forEach(b => warn(SHEETS.plan, null,
              `${d.nav} 的「${b.title}」是大眾運輸但沒有列出起終點站，`
              + `底下可以加「→起終點」把上下車站寫進去。`));
    d.blocks.filter(b => b.type === "options" && b.choices.length < 2)
            .forEach(() => warn(SHEETS.plan, null, `${d.nav} 有「二選一」但選項少於 2 個`));
  });

  const data = {
    meta,
    days: [
      { id:"d0", nav:"📝 行前", blocks: prepBlocks },
      ...days,
      { id:"wallet", nav:"💰 記帳", blocks:[{ type:"expenses" }] },
      { id:"info",   nav:"🆘 資訊",
        blocks: [phrases.length && { type:"phrases" }, emergency.length && { type:"emergency" }].filter(Boolean) }
    ].filter(d => d.blocks.length),
    phrases, emergency
  };

  return { data: errors.length ? null : data, errors, warnings };
}

/* ISO 3166 國碼 → ISO 4217 幣別。查不到就留空並提醒使用者自己填。 */
export const COUNTRY_CURRENCY = {
  JP:"JPY", KR:"KRW", TW:"TWD", CN:"CNY", HK:"HKD", MO:"MOP", SG:"SGD", MY:"MYR",
  TH:"THB", VN:"VND", PH:"PHP", ID:"IDR", IN:"INR", LK:"LKR", NP:"NPR", BD:"BDT",
  PK:"PKR", KH:"KHR", LA:"LAK", MM:"MMK", BN:"BND", MN:"MNT", MV:"MVR", BT:"BTN",
  KZ:"KZT", UZ:"UZS", KG:"KGS", GE:"GEL", AM:"AMD", AZ:"AZN",
  AE:"AED", SA:"SAR", QA:"QAR", KW:"KWD", BH:"BHD", OM:"OMR", IL:"ILS", JO:"JOD",
  LB:"LBP", TR:"TRY", IQ:"IQD",
  AD:"EUR", AT:"EUR", BE:"EUR", CY:"EUR", DE:"EUR", EE:"EUR", ES:"EUR", FI:"EUR",
  FR:"EUR", GR:"EUR", HR:"EUR", IE:"EUR", IT:"EUR", LT:"EUR", LU:"EUR", LV:"EUR",
  MC:"EUR", ME:"EUR", MT:"EUR", NL:"EUR", PT:"EUR", SI:"EUR", SK:"EUR", SM:"EUR", VA:"EUR",
  GB:"GBP", CH:"CHF", LI:"CHF", NO:"NOK", SE:"SEK", DK:"DKK", IS:"ISK", PL:"PLN",
  CZ:"CZK", HU:"HUF", RO:"RON", BG:"BGN", RS:"RSD", BA:"BAM", MK:"MKD", AL:"ALL",
  UA:"UAH", MD:"MDL",
  US:"USD", CA:"CAD", MX:"MXN", BR:"BRL", AR:"ARS", CL:"CLP", CO:"COP", PE:"PEN",
  UY:"UYU", PY:"PYG", BO:"BOB", EC:"USD", CR:"CRC", PA:"PAB", GT:"GTQ", HN:"HNL",
  NI:"NIO", SV:"USD", BZ:"BZD", DO:"DOP", JM:"JMD", TT:"TTD", BS:"BSD", BB:"BBD", PR:"USD",
  AU:"AUD", NZ:"NZD", FJ:"FJD", PG:"PGK", NC:"XPF", PF:"XPF", VU:"VUV", WS:"WST", TO:"TOP", GU:"USD",
  ZA:"ZAR", EG:"EGP", MA:"MAD", TN:"TND", DZ:"DZD", KE:"KES", TZ:"TZS", UG:"UGX",
  ET:"ETB", NG:"NGN", GH:"GHS", SN:"XOF", CI:"XOF", CM:"XAF", RW:"RWF", NA:"NAD",
  BW:"BWP", MU:"MUR", SC:"SCR", MG:"MGA", ZM:"ZMW", MW:"MWK", MZ:"MZN"
};

/* 依出發月份與半球自動選主題（南半球季節對調） */
export function autoTheme(startDate, lat){
  const m = Number(String(startDate).slice(5, 7)) || 1;
  const shift = (lat != null && lat < 0) ? ((m + 5) % 12) + 1 : m;
  if (shift === 12 || shift <= 2) return "snow";
  if (shift <= 5)  return "sakura";
  if (shift <= 8)  return "ocean";
  return "maple";
}


/* ══════════════════════════════════════════════════
   ExcelJS → 純二維陣列
   ExcelJS 的儲存格可能是字串、數字、Date、超連結物件或
   富文字物件，這裡一律攤平成單純的值。
   ══════════════════════════════════════════════════ */
function cellValue(v){
  if (v === null || v === undefined) return null;
  if (v instanceof Date) return v;
  if (typeof v === "object"){
    if (Array.isArray(v.richText)) return v.richText.map(t => t.text).join("");
    if (v.text !== undefined)      return v.text;          // 超連結
    if (v.result !== undefined)    return v.result;        // 公式
    if (v.hyperlink !== undefined) return v.hyperlink;
    return String(v);
  }
  return v;
}

export function toBook(workbook){
  const sheets = {}, merges = {};
  workbook.eachSheet(ws => {
    const grid = [];
    ws.eachRow({ includeEmpty: true }, row => {
      const vals = row.values || [];
      grid.push(vals.slice(1).map(cellValue));   // ExcelJS 的索引從 1 開始
    });
    sheets[ws.name] = grid;
    merges[ws.name] = (ws.model && ws.model.merges) ? ws.model.merges.slice() : [];
  });
  return { sheets, merges };
}

export function parseWorkbook(workbook){
  return parseGrids(toBook(workbook));
}
