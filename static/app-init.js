// 初始化（必须最后加载）：首次渲染 / SW 注册
// ── 初始化 ──
const initial = (location.hash || '').replace('#', '').split('?')[0];
(async () => {
  await loadLocale(currentLang());
  applyI18n(document);
  const savedTheme = localStorage.getItem('theme');
  applyTheme(THEME_ORDER.includes(savedTheme) ? savedTheme : 'auto');
  const sel = document.getElementById('subjectSelect');
  if (sel) {
    const resolved = await loadSubjectOptions(sel, currentSubject());
    if (resolved !== currentSubject()) {
      localStorage.setItem('subject', resolved);
      const url = new URL(location.href);
      url.searchParams.set('subject', resolved);
      history.replaceState(null, '', url.toString());
    }
  }
  // 兜底：设置页默认学科下拉提前填充（即使 loadSettings 出错也保证可选）
  loadSubjectOptions(document.getElementById('setDefaultSubject'));
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
          if (bar) { bar.classList.add('active'); bar.classList.remove('hidden'); }
        }
      });
    });
  }).catch(() => {});
}
function applyUpdate() {
  trackEvent('update.click'); // P2-5
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage('SKIP_WAITING');
  } else {
    location.reload();
  }
}

// 浏览器通知（最小可用）：页面隐藏时若有到期复习，且用户已授权，则提醒一次。
// 失败静默——通知只是可选项，不影响任何主流程。
(async () => {
  try {
    if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
      const d = await fetch('/api/dashboard').then(r => r.json()).catch(() => null);
      if (d && (d.due || 0) > 0) {
        new Notification('LearnOS', { body: t('notify.due').replace('{n}', d.due) });
      }
    }
  } catch (e) { /* 静默 */ }
})();
