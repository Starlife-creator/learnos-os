// 初始化（必须最后加载）：首次渲染 / SW 注册
// ── 初始化 ──
const initial = (location.hash || '').replace('#', '').split('?')[0];
(async () => {
  await loadLocale(currentLang());
  applyI18n(document);
  const savedTheme = localStorage.getItem('theme');
  applyTheme(THEME_ORDER.includes(savedTheme) ? savedTheme : 'auto');
  switchPage(PAGES.includes(initial) ? initial : 'dashboard');
})();
document.getElementById('searchInput').addEventListener('input', onSearchInput);
loadOcrProbe();
// C7 PWA：注册 Service Worker（仅 http/https，离线缓存静态资源）+ 新版本提示
if ('serviceWorker' in navigator && /^https?:$/.test(location.protocol)) {
  let refreshing = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (refreshing) return;
    refreshing = true;
    location.reload();
  });
  navigator.serviceWorker.register('./sw.js').then((reg) => {
    reg.addEventListener('updatefound', () => {
      const nw = reg.installing;
      if (!nw) return;
      nw.addEventListener('statechange', () => {
        if (nw.state === 'installed' && navigator.serviceWorker.controller) {
          const bar = document.getElementById('updateBar');
          if (bar) bar.classList.add('active');
        }
      });
    });
  }).catch(() => {});
}
function applyUpdate() {
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage('SKIP_WAITING');
  } else {
    location.reload();
  }
}
