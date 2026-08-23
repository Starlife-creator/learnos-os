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

// ── 帮助系统：搜索 / 目录高亮 / 新手引导 ──
function helpFilter() {
  const q = (document.getElementById('helpSearch').value || '').trim().toLowerCase();
  const items = document.querySelectorAll('.help-item');
  const empty = document.getElementById('helpEmpty');
  let shown = 0;
  items.forEach(el => {
    const text = el.textContent.toLowerCase();
    const hit = !q || text.includes(q);
    el.classList.toggle('hidden', !hit);
    if (hit) shown++;
  });
  if (empty) empty.classList.toggle('hidden', shown > 0);
}

// 目录点击 → 展开对应区块并滚动定位
document.querySelectorAll('#helpToc .help-toc-link').forEach(link => {
  link.addEventListener('click', (e) => {
    e.preventDefault();
    const id = link.dataset.target;
    const sec = document.getElementById(id);
    if (!sec) return;
    sec.open = true;
    sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

// 滚动时高亮当前可见区块
(function setupHelpSpy() {
  const links = Array.from(document.querySelectorAll('#helpToc .help-toc-link'));
  const map = new Map(links.map(l => [l.dataset.target, l]));
  const items = links.map(l => document.getElementById(l.dataset.target)).filter(Boolean);
  if (!('IntersectionObserver' in window) || !items.length) return;
  const obs = new IntersectionObserver((entries) => {
    entries.forEach(en => {
      if (en.isIntersecting) {
        links.forEach(l => l.classList.remove('active'));
        const link = map.get(en.target.id);
        if (link) link.classList.add('active');
      }
    });
  }, { rootMargin: '-80px 0px -60% 0px', threshold: 0 });
  items.forEach(el => obs.observe(el));
})();

// 新手引导：分步高亮（不依赖第三方），状态存 localStorage
function startHelpTour() {
  const steps = [
    { sel: '#help-quick', title: '五步学习循环', body: '这是 LearnOS 的核心闭环：录入 → 提示 → 主动回忆 → 间隔复习 → 口试。建议每天从「概览」的今日行动开始。' },
    { sel: '#help-dashboard', title: '今日行动', body: '「概览」页把到期复习、薄弱概念口试、错题巩固、题库练习汇总到一个队列，按优先级推进。' },
    { sel: '#help-review', title: '诚实评分', body: '复习时按 忘记 / 模糊 / 想起 / 秒答 四档评分，评分越诚实，FSRS 调度越准。' },
    { sel: '#help-oral', title: '口试检验', body: '用 AI 五轮追问检验你是否真正理解，答案按「中国/红涨绿跌」无关的纯知识判定即可。' },
    { sel: '#help-data', title: '数据安全', body: '数据全部存在本机 learnos.db，可随时导出/备份。需要时从「概览」页导出即可。' }
  ];
  let i = 0;
  const overlay = document.createElement('div');
  overlay.className = 'tour-overlay';
  function render() {
    const s = steps[i];
    overlay.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'tour-card';
    card.innerHTML =
      '<div class="tour-step">第 ' + (i + 1) + ' / ' + steps.length + ' 步</div>' +
      '<h4>' + s.title + '</h4>' +
      '<p>' + s.body + '</p>' +
      '<div class="tour-actions">' +
        (i > 0 ? '<button class="btn btn-sm btn-secondary" id="tourPrev">上一步</button>' : '') +
        (i < steps.length - 1
          ? '<button class="btn btn-sm btn-primary" id="tourNext">下一步</button>'
          : '<button class="btn btn-sm btn-primary" id="tourNext">完成</button>') +
        '<button class="btn btn-sm btn-secondary" id="tourSkip">跳过</button>' +
      '</div>';
    overlay.appendChild(card);
    card.querySelector('#tourNext').addEventListener('click', () => {
      i++;
      if (i >= steps.length) return close();
      render();
    });
    const prev = card.querySelector('#tourPrev');
    if (prev) prev.addEventListener('click', () => { i--; render(); });
    card.querySelector('#tourSkip').addEventListener('click', close);
    const target = document.getElementById(s.sel.replace('#', ''));
    if (target) { target.open = true; target.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  }
  function close() {
    overlay.remove();
    try { localStorage.setItem('learnos_help_tour_done', '1'); } catch (e) {}
  }
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  document.body.appendChild(overlay);
  render();
}

// 首次进入帮助页时自动引导一次
(function autoTour() {
  try {
    if (localStorage.getItem('learnos_help_tour_done')) return;
    const help = document.getElementById('page-help');
    if (help && help.classList.contains('active')) startHelpTour();
  } catch (e) {}
})();
