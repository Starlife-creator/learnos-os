// 策略：全站网络优先（本地应用网络必然可达），缓存仅作离线兜底。
// 新版发布后无需硬刷新，也无需再递增 CACHE_NAME（网络优先下新资源自然生效）。
// v3：API 直连 + 无内联脚本（配合 CSP 收紧），清旧缓存
const CACHE_NAME = 'learnos-os-v3';

self.addEventListener('install', () => {
  self.skipWaiting();
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
  if (event.request.method !== 'GET') return;
  // API 一律直连：数据实时性优先，且 SW 对 no-store 响应的 cache.put 会引发流互锁
  if (url.pathname.startsWith('/api/')) return;
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
});