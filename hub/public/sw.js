/* Service Worker for 电脑遥控中心 PWA
策略: 仅缓存静态壳资源(HTML/manifest/图标/SW), 实时 API 请求(/api/*)始终走网络,
保证中转/代理响应始终是最新的。*/
const CACHE = "rc-shell-v1";
const SHELL = [
  "./",
  "./manifest.webmanifest",
  "./icons/apple-touch-icon.png",
  "./icons/icon-192.png",
  "./icons/icon-512.png"
];

self.addEventListener("install", e => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // API 中转请求: 永远走网络(实时性 + 局域网易断)
  if (url.pathname.startsWith("/api/")) return;
  // 静态壳: cache-first, 失败回退网络
  e.respondWith(
    caches.match(req).then(cached => {
      if (cached) return cached;
      return fetch(req).then(resp => {
        if (resp && resp.status === 200 && url.origin === location.origin) {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
        }
        return resp;
      }).catch(() => cached);
    })
  );
});