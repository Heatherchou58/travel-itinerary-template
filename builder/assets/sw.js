/* ══════════════════════════════════════════════════
   Service Worker —— 離線快取
   ──────────────────────────────────────────────────
   行程內容（index.html）採「網路優先」，所以改完行程
   重新部署，使用者一有網路就會拿到新版，不用改這裡。

   只有在「換圖示」或「改 sw.js 本身」時，才需要把下面
   的 VERSION 加 1，強制清掉舊快取。
   ══════════════════════════════════════════════════ */
const VERSION  = "v1";
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
      fetch(req),
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
