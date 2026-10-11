/* ══════════════════════════════════════════════════
   Service Worker —— 離線快取
   ──────────────────────────────────────────────────
   行程內容（index.html）採「網路優先」，所以改完行程
   重新部署，使用者一有網路就會拿到新版，不用改這裡。

   manifest.json 與 icons/ 走「快取優先」，改動它們時必須把
   下面的 VERSION 加 1，否則已經開過網站的人會一直拿到舊版，
   重新部署也救不回來。（sw.js 本身不在 PRECACHE 裡，瀏覽器
   會自己偵測並安裝新版，不需要為了它升版。）
   ══════════════════════════════════════════════════ */
const VERSION  = "v3";
const CACHE    = `trip-${VERSION}`;
const TIMEOUT  = 3000;   // 網路等待上限（毫秒），超過就用快取

const PRECACHE = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-512.png",
  "./icons/apple-touch-icon.png",
  "./icons/favicon-32.png"
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;

  // 跨網域一律放行，不攔截也不快取：
  //   Open-Meteo  → 天氣要即時，且 app 自己有存 localStorage
  //   Firebase    → 同步邏輯自行處理離線
  if (new URL(req.url).origin !== self.location.origin) return;

  const isDoc = req.mode === "navigate" || req.destination === "document";
  e.respondWith(isDoc ? networkFirst(req) : cacheFirst(req));
});

/* 網頁本體：網路優先，逾時或離線就用快取 */
async function networkFirst(req){
  const cache = await caches.open(CACHE);
  try{
    const net = await Promise.race([
      /* cache:"no-cache" 會強制向伺服器驗證。少了它，這個 fetch 會先撞上
         瀏覽器自己的 HTTP 快取——GitHub Pages 對 HTML 回 max-age=600，
         等於「網路優先」在部署後 10 分鐘內拿到的還是舊檔。
         用 no-cache 而非 no-store：帶 ETag 發條件式請求，沒變就回 304，
         既保證最新又不會每次都重傳整頁。 */
      fetch(req, { cache: "no-cache" }),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), TIMEOUT))
    ]);
    if (net && net.ok) cache.put(req, net.clone());
    return net;
  }catch{
    return (await cache.match(req))
        || (await cache.match("./index.html"))
        || new Response("目前離線，且沒有可用的快取版本。", {
             status: 503,
             headers: { "Content-Type": "text/plain;charset=utf-8" }
           });
  }
}

/* 圖示等靜態檔：快取優先 */
async function cacheFirst(req){
  const hit = await caches.match(req);
  if (hit) return hit;
  try{
    const res = await fetch(req);
    if (res.ok) (await caches.open(CACHE)).put(req, res.clone());
    return res;
  }catch{
    return new Response("", { status: 504 });
  }
}
