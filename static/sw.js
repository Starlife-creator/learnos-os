// 注意：每次发布（index.html/app.js/locale 变更）必须递增 CACHE_NAME，
// 否则 cache-first 策略会让浏览器继续用旧缓存，更新提示也不会出现。
const CACHE_NAME = 'physics-os-v3';
const STATIC_ASSETS = [
  './',
  './index.html',
  './app.js',
  './concept_map.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// 页面确认后跳过等待，立即应用新版本
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  // API 请求：网络优先，失败时尽力回退缓存（离线时提示）
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          if (resp.ok && event.request.method === 'GET') {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return resp;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }
  // 语言词条：网络优先（词条会随版本更新），失败回退缓存
  if (url.pathname.startsWith('/locale/')) {
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return resp;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }
  // 静态资源：缓存优先，网络后备（离线可用）
  event.respondWith(
    caches.match(event.request).then(
      (cached) => cached || fetch(event.request).then((resp) => {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return resp;
      })
    )
  );
});
