// 个人物理学习 OS — 前端逻辑（抽离自 inline，便于静态语法校验）
const API = '';
const X_HEADER = 'X-Requested-With';
const X_VALUE = 'PhysicsStudyOS';

// ── F2 双语：语言加载 / 翻译 / DOM 应用 ──
const LOCALES = ['zh-CN', 'en-US'];
let _dict = {};
let _lang = '';

function currentLang() {
  const saved = localStorage.getItem('lang');
  if (LOCALES.includes(saved)) return saved;
  // 首次访问：按浏览器语言就近选择（zh 前缀 → zh-CN，en 前缀 → en-US）
  const nav = (navigator.language || '').toLowerCase();
  if (nav.startsWith('en')) return 'en-US';
  return 'zh-CN';
}

async function loadLocale(lang) {
  _lang = lang;
  localStorage.setItem('lang', lang);
  document.documentElement.lang = lang === 'en-US' ? 'en' : 'zh-CN';
  try {
    const res = await fetch(`/locale/${lang}.json`, { cache: 'no-cache' });
    _dict = await res.json();
  } catch (e) {
    _dict = {};
  }
}

function t(key, fallback) {
  return _dict[key] || fallback || key;
}

function applyI18n(root) {
  const scope = root || document;
  scope.querySelectorAll('[data-i18n]').forEach(el => {
    const text = t(el.getAttribute('data-i18n'));
    el.textContent = text; // 键值为纯文本（测试守护无 HTML 标签），用 textContent 避免 XSS
  });
  scope.querySelectorAll('[data-i18n-ph]').forEach(el => {
    el.setAttribute('placeholder', t(el.getAttribute('data-i18n-ph')));
  });
  scope.querySelectorAll('[data-i18n-aria]').forEach(el => {
    el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria')));
  });
  const sel = document.getElementById('langSelect');
  if (sel) sel.value = _lang;
}

async function setLang(lang) {
  if (!LOCALES.includes(lang)) return;
  await loadLocale(lang);
  applyI18n(document);
  refreshForLang();
  toast(t('toast.langSwitched'));
}

function refreshForLang() {
  const active = document.querySelector('.page.active');
  if (!active) return;
  const page = active.id;
  if (page === 'page-dashboard') loadDashboard();
  else if (page === 'page-problems') loadProblems(1);
  else if (page === 'page-review') loadReviews();
  else if (page === 'page-oral') { /* 动态对话内容不翻译 */ }
  else if (page === 'page-rag') { loadRagDocs(); loadRagStatus(); }
  else if (page === 'page-exam') loadExamPapers();
  else if (page === 'page-settings') { loadSettings(); loadPrefs(); loadFsrsStatus(); }
}

function uid() {
  return 'req-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}

async function api(path, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  const headers = { 'Content-Type': 'application/json', [X_HEADER]: X_VALUE };
  // 写操作携带幂等键，避免网络重试产生重复数据
  if (method !== 'GET') headers['X-Request-Id'] = uid();
  const body = opts.body ? JSON.stringify(opts.body) : undefined;

  let fetchFailed = false;
  const doFetch = () => fetch(API + path, { method, headers, body }).catch(e => { fetchFailed = true; throw e; });
  try {
    const res = await doFetch();
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || t('msg.requestFail'));
    return data;
  } catch (err) {
    // 仅对网络层失败（非 HTTP 错误）重试一次（GET）
    if (fetchFailed && method === 'GET') {
      fetchFailed = false;
      const res = await doFetch();
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || t('msg.requestFail'));
      return data;
    }
    throw err;
  }
}

function toast(msg, type = 'success') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function masteryBar(level) {
  let html = '<div class="mastery-bar">';
  for (let i = 1; i <= 5; i++) html += `<div class="mastery-dot ${i <= level ? 'filled' : ''}"></div>`;
  return html + '</div>';
}

function masteryTag(level) {
  const cls = level >= 4 ? 'tag-green' : level >= 3 ? 'tag-blue' : level >= 2 ? 'tag-amber' : 'tag-red';
  const labels = {1:'label.rate1',2:'label.rate2',3:'label.rate3',4:'label.rate4',5:'label.rate5'};
  return `<span class="tag ${cls}">${t(labels[level]||'label.unknown')}</span>`;
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function renderMath(el) {
  if (window.renderMathInElement) {
    renderMathInElement(el || document.body, {
      delimiters: [
        {left: '$$', right: '$$', display: true},
        {left: '$', right: '$', display: false},
        {left: '\\[', right: '\\]', display: true},
        {left: '\\(', right: '\\)', display: false},
      ],
      throwOnError: false,
    });
  }
}

// ── 弹窗：焦点陷阱 + Esc/遮罩关闭 ──
let _lastFocus = null;
function openModal(id) {
  const overlay = document.getElementById(id);
  if (!overlay) return;
  _lastFocus = document.activeElement;
  overlay.classList.add('active');
  const focusables = overlay.querySelectorAll('input, textarea, select, button, [tabindex]');
  if (focusables.length) focusables[0].focus();
}
function closeModal(id) {
  const overlay = document.getElementById(id);
  if (!overlay) return;
  overlay.classList.remove('active');
  if (_lastFocus && _lastFocus.focus) _lastFocus.focus();
  // 清理详情弹窗的键盘快捷键监听
  if (id === 'problemModal' && overlay._onKey) {
    document.removeEventListener('keydown', overlay._onKey);
    overlay._onKey = null;
  }
  // 通知等待中的 Promise（如确认弹窗被遮罩/Esc 关闭时 resolve 为取消）
  overlay.dispatchEvent(new Event('close'));
}

// ── B5 标签 ──────────────────────────────────────────────

let currentTags = [];

function renderTags() {
  const wrap = document.getElementById('editTagsWrap');
  wrap.innerHTML = currentTags.map((tag, i) =>
    `<span class="chip${tag.pending ? ' pending' : ''}" title="${tag.pending ? t('tag.aiSuggest') : ''}">${escapeHtml(tag.text)}<span class="chip-x" onclick="removeTag(${i})" aria-label="${t('detail.removeTag')}">&times;</span></span>`
  ).join('');
  const hint = document.getElementById('editTagsHint');
  const pendingCount = currentTags.filter(c => c.pending).length;
  if (pendingCount) hint.textContent = t('tag.hintPending').replace('{n}', pendingCount);
  else if (currentTags.length) hint.textContent = t('tag.hintSaved');
  else hint.textContent = t('tag.hintManual');
}

function removeTag(i) { currentTags.splice(i, 1); renderTags(); }

async function extractTags() {
  const btn = document.getElementById('extractTagsBtn');
  btn.disabled = true;
  try {
    const data = await api('/api/ai/extract-tags', {
      method: 'POST',
      body: {
        title: document.getElementById('editTitle').value,
        content: document.getElementById('editContent').value,
        course: document.getElementById('editCourse').value,
        topic: document.getElementById('editTopic').value,
      },
    });
    currentTags = (data.tags || []).map(t => ({
      text: String(t),
      pending: data.confidence < 0.9 || data.pending === true,
    }));
    renderTags();
    const source = data.source === 'ai' ? 'AI' : t('tag.sourceRule');
    const conf = Math.round((data.confidence || 0) * 100);
    document.getElementById('editTagsHint').textContent =
      t('tag.extractResult').replace('{s}', source).replace('{c}', conf).replace('{d}', data.source !== 'ai' ? t('tag.degraded') : '') + '。';
  } catch(e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

function tagInputKeydown(e) {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const input = document.getElementById('editTagInput');
  const text = input.value.trim();
  if (text) {
    currentTags.push({ text, pending: false });
    input.value = '';
    renderTags();
  }
}


document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.active').forEach(m => closeModal(m.id));
  }
  // 焦点陷阱
  if (e.key === 'Tab') {
    const active = document.querySelector('.modal-overlay.active');
    if (!active) return;
    const f = active.querySelectorAll('input, textarea, select, button, [tabindex]');
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});
document.querySelectorAll('.modal-overlay').forEach(m => {
  m.addEventListener('click', e => { if (e.target === m) closeModal(m.id); });
});

// ── 确认弹窗（替代原生 confirm）──
function confirmDialog(message) {
  return new Promise((resolve) => {
    const modal = document.getElementById('confirmModal');
    document.getElementById('confirmMsg').textContent = message;
    openModal('confirmModal');
    const ok = document.getElementById('confirmOk');
    const cancel = document.getElementById('confirmCancel');
    const cleanup = (result) => {
      ok.removeEventListener('click', onOk);
      cancel.removeEventListener('click', onCancel);
      modal.removeEventListener('close', onClose);
      closeModal('confirmModal');
      resolve(result);
    };
    const onOk = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onClose = () => cleanup(false); // 遮罩/Esc 关闭视为取消
    ok.addEventListener('click', onOk);
    cancel.addEventListener('click', onCancel);
    modal.addEventListener('close', onClose);
  });
}

// ── 导航 + 深链 ──
const PAGES = ['dashboard', 'problems', 'review', 'oral', 'rag', 'exam', 'settings'];
function parseHash() {
  const h = (location.hash || '').replace('#', '');
  const [page, query] = h.split('?');
  return { page, query: new URLSearchParams(query || '') };
}
function getHashParam(key) { return parseHash().query.get(key) || ''; }
function clearPrereqFilter() {
  history.replaceState(null, '', '#problems');
  loadProblems(1);
}
function switchPage(page, {push=true}={}) {
  if (!PAGES.includes(page)) page = 'dashboard';
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === page));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + page).classList.add('active');
  if (push) history.pushState(null, '', '#' + page);
  if (page === 'dashboard') loadDashboard();
  if (page === 'problems') loadProblems(1);
  if (page === 'review') loadReviews();
  if (page === 'settings') { loadSettings(); loadFsrsStatus(); }
  if (page === 'rag') { loadRagDocs(); loadRagSearch(''); }
  if (page === 'exam') loadExam();
}
window.addEventListener('popstate', () => {
  const { page } = parseHash();
  switchPage(PAGES.includes(page) ? page : 'dashboard', {push: false});
  if (page === 'problems') loadProblems(1);
});
document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => switchPage(el.dataset.page));
});
document.addEventListener('keydown', (e) => {
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  const idx = parseInt(e.key, 10);
  if (idx >= 1 && idx <= PAGES.length) {
    // 复习页：数字 1-4 直接评分当前第一张卡片
    if (idx <= 4 && document.getElementById('page-review').classList.contains('active')) {
      const item = document.querySelector('#reviewList .list-item');
      if (item) {
        const btn = item.querySelectorAll('button[onclick*="completeReview"]')[idx - 1];
        if (btn) { btn.click(); return; }
      }
    }
    switchPage(PAGES[idx - 1]);
  }
});

// ── 主题切换（auto / dark / light）──
const THEME_ORDER = ['auto', 'dark', 'light'];
function applyTheme(theme) {
  const root = document.documentElement;
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (theme === 'dark') { root.dataset.theme = 'dark'; if (meta) meta.content = 'dark'; }
  else if (theme === 'light') { root.dataset.theme = 'light'; if (meta) meta.content = 'light'; }
  else { delete root.dataset.theme; if (meta) meta.content = 'light dark'; }
  const btn = document.getElementById('themeToggle');
  if (btn) btn.title = t('theme.' + theme);
}
document.getElementById('themeToggle').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme || 'auto';
  const next = THEME_ORDER[(THEME_ORDER.indexOf(cur) + 1) % THEME_ORDER.length];
  localStorage.setItem('theme', next);
  applyTheme(next);
  toast(t('theme.switched').replace('{t}', t('theme.' + next)));
});

// ── 概览 ──
async function loadDashboard() {
  const el = document.getElementById('topicsList');
  el.innerHTML = '<div class="loading">' + t('msg.loading') + '</div>';
  try {
    const d = await api('/api/dashboard');
    document.getElementById('statTotal').textContent = d.stats.total || 0;
    document.getElementById('statDue').textContent = d.due || 0;
    document.getElementById('statMastered').textContent = d.stats.mastered || 0;
    document.getElementById('statAvg').textContent = (d.stats.avg_mastery || 0).toFixed(1);
    updateDueBadge(d.due || 0);
    maybeNotify(d.due || 0);

    if (d.topics && d.topics.length) {
      el.innerHTML = d.topics.map(t => `
        <div class="flex-between mb-8">
          <span class="text-sm">${escapeHtml(t.topic)}</span>
          <span class="flex gap-8 items-center">
            <span class="tag tag-gray">${t('dash.topicCount').replace('{n}', t.count)}</span>
            ${masteryBar(t.mastery)}
          </span>
        </div>`).join('');
    } else {
      el.innerHTML = '<div class="empty"><p>' + t('msg.noData') + '</p></div>';
    }

    const recentEl = document.getElementById('recentList');
    if (d.recent && d.recent.length) {
      recentEl.innerHTML = d.recent.map(p => `
        <div class="list-item" onclick="viewProblem(${p.id})">
          <div class="list-item-header">
            <span class="list-item-title">${escapeHtml(p.title)}</span>
            ${masteryTag(p.mastery)}
          </div>
          <div class="list-item-meta">${escapeHtml(p.course)} · ${escapeHtml(p.topic)} · ${escapeHtml(p.error_type)}</div>
        </div>`).join('');
    } else {
      recentEl.innerHTML = '<div class="empty"><p>' + t('msg.noRecent') + '</p></div>';
    }
    drawTrend(d);
    drawAnalytics(d);
    drawErrorDist(d.error_distribution || []);
    drawErrorTrend(d.error_trend || []);
    loadProfile(d);

    // 课程级统计
    const courseEl = document.getElementById('courseStats');
    if (d.course_stats && d.course_stats.length) {
      courseEl.innerHTML = d.course_stats.map(c => `
        <div class="flex-between mb-8">
          <span class="text-sm" style="min-width:80px">${escapeHtml(c.course)}</span>
          <span class="flex gap-8 items-center" style="flex:1">
            <span style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden">
              <span style="display:block;height:100%;width:${(c.avg_mastery/5*100).toFixed(0)}%;background:${c.avg_mastery>=4?'var(--success)':c.avg_mastery>=3?'var(--accent)':'var(--warning)'};border-radius:3px"></span>
            </span>
            <span class="tag tag-gray">${t('dash.courseCount').replace('{m}', c.avg_mastery).replace('{n}', c.count).replace('{d}', c.due>0 ? t('dash.courseDue').replace('{n}', c.due) : '')}</span>
          </span>
        </div>`).join('');
    } else {
      courseEl.innerHTML = '<div class="empty"><p>' + t('msg.noCourse') + '</p></div>';
    }

    // 最近复习活动
    const actEl = document.getElementById('recentActivity');
    if (d.recent_activity && d.recent_activity.length) {
      const labels = {1:'label.rev1',2:'label.rev2',3:'label.rev3',4:'label.rev4'};
      actEl.innerHTML = d.recent_activity.map(a => `
        <div class="list-item" onclick="viewProblem(${a.problem_id})" style="padding:8px 12px">
          <span class="text-sm">📝 ${escapeHtml(a.title)}</span>
          <span class="tag ${a.result==='4'?'tag-green':a.result==='3'?'tag-blue':'tag-amber'}">${t('label.rev'+a.result)||'?'}</span>
          <span class="text-muted text-sm" style="float:right">${(a.created_at||'').slice(0,16)}</span>
        </div>`).join('');
    } else { actEl.innerHTML = '<div class="empty"><p>' + t('msg.noActivity') + '</p></div>'; }
  } catch(e) { toast(e.message, 'error'); }
}

// ── C6 复习提醒：角标 + 浏览器通知 ──
function updateDueBadge(n) {
  const b = document.getElementById('dueBadge');
  if (!b) return;
  b.textContent = n > 99 ? '99+' : n;
  b.classList.toggle('hidden', !(n > 0));
}

function notificationsEnabled() { return localStorage.getItem('notifyEnabled') === '1'; }

function maybeNotify(due) {
  if (!due || !notificationsEnabled() || !('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  const now = Date.now();
  const last = parseInt(localStorage.getItem('notifyLastAt') || '0', 10);
  if (now - last < 10 * 60 * 1000) return;  // 10 分钟节流
  try {
    new Notification(t('notify.title'), { body: t('notify.dueBody').replace('{n}', due), tag: 'due-review' });
    localStorage.setItem('notifyLastAt', String(now));
  } catch(e) { /* 通知失败不影响 */ }
}

async function requestNotifyPermission() {
  if (!('Notification' in window)) { toast(t('notify.unsupported'), 'error'); return; }
  if (Notification.permission === 'granted') { toast(t('notify.granted')); return; }
  const res = await Notification.requestPermission();
  toast(res === 'granted' ? t('notify.on') : t('notify.off'), res === 'granted' ? 'success' : 'error');
}

// C6 轮询：每 5 分钟轻量刷新待复习角标 + 通知
setInterval(async () => {
  try {
    const d = await api('/api/dashboard');
    updateDueBadge(d.due || 0);
    maybeNotify(d.due || 0);
    const { page } = parseHash();
    if (page === 'dashboard' && !document.hidden) loadDashboard();
  } catch(e) { /* 静默失败 */ }
}, 5 * 60 * 1000);

function drawTrend(data) {
  const svg = document.getElementById('trendSvg');
  const hint = document.getElementById('trendHint');
  const log = (data && data.points) || [];
  const summary = (data && data.summary) || {};
  if (!log.length) { svg.innerHTML = ''; hint.textContent = t('trend.empty'); return; }
  const W = 300, H = 120, pad = 10;
  const max = 5, min = 0;
  const n = log.length;
  const x = i => pad + (W - 2 * pad) * (n === 1 ? 0.5 : i / (n - 1));
  const y = v => H - pad - (H - 2 * pad) * ((v - min) / (max - min));
  const pts = log.map((d, i) => `${x(i).toFixed(1)},${y(d.avg_mastery).toFixed(1)}`).join(' ');
  const last = log[log.length - 1];
  svg.innerHTML = `
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="var(--border)" stroke-width="1"/>
    <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2"/>
    <circle cx="${x(n - 1).toFixed(1)}" cy="${y(last.avg_mastery).toFixed(1)}" r="3" fill="var(--accent)"/>
  `;
  const acc = summary.week_accuracy ? t('trend.weekAcc').replace('{p}', summary.week_accuracy) : '';
  hint.textContent = t('trend.summary').replace('{n}', n).replace('{acc}', acc).replace('{m}', last.avg_mastery);
}

// ── D4 复习分析（压力图 + 卡组健康度）──
function drawAnalytics(data) {
  const chart = document.getElementById('dueChart');
  const hint = document.getElementById('dueChartHint');
  if (!chart) return;
  const series = (data && data.due_7d) || [];
  if (!series.length) { chart.innerHTML = ''; hint.textContent = '—'; return; }
  const max = Math.max(1, ...series.map(s => s.due));
  const todayLabel = new Date().toISOString().slice(5, 10);
  chart.innerHTML = series.map(s => {
    const h = Math.max(3, Math.round(s.due / max * 82));
    const isToday = s.date.slice(5) === todayLabel;
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px">
      <span class="text-sm" style="font-size:11px">${s.due || ''}</span>
      <div style="width:100%;max-width:34px;height:${h}px;background:${isToday ? 'var(--warning)' : 'var(--accent)'};border-radius:3px;opacity:${s.due ? 1 : 0.25}"></div>
      <span class="text-sm" style="font-size:10px;color:var(--text-2)">${isToday ? t('label.today') : s.date.slice(5)}</span>
    </div>`;
  }).join('');
  const total7 = series.reduce((n, s) => n + s.due, 0);
  hint.textContent = total7 ? t('dash.due7').replace('{n}', total7) : t('dash.due7None');
  const dh = document.getElementById('deckHealth');
  const h = (data && data.deck_health) || {};
  if (dh) dh.innerHTML = h.total ?
    t('dash.deckHealth').replace('{a}', h.newborn).replace('{b}', h.learning).replace('{c}', h.mature).replace('{t}', h.total).replace('{r}', h.avg_repetition).replace('{m}', h.avg_mastery) :
    t('msg.noData');
  drawPressure((data && data.pressure) || {});
  drawForgetPredict((data && data.forget_predict) || {});
  drawTodayTasks((data && data.tasks) || []);
  drawStubborn((data && data.stubborn) || []);
  drawGamification((data && data.gamification) || {});
  drawTelemetry((data && data.telemetry) || {});
  drawWeekly((data && data.weekly) || {});
  if (data && data.forgetting !== undefined) drawForgetCurve(data.forgetting);
}

// ── D4 遗忘曲线（SVG 纯手绘，零依赖）──
function drawForgetCurve(f) {
  const svg = document.getElementById('forgetSvg');
  const stats = document.getElementById('forgetStats');
  if (!svg || !f) return;
  const W = 340, H = 150, padL = 34, padB = 22, padT = 10, padR = 10;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const NS = 'http://www.w3.org/2000/svg';
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const mk = (tag, attrs) => { const el = document.createElementNS(NS, tag); for (const k in attrs) el.setAttribute(k, attrs[k]); return el; };
  const xOf = t => padL + (t / 30) * plotW;
  const yOf = r => padT + (1 - r) * plotH;
  // 网格 + 轴
  for (const g of [0.5, 0.7, 0.9]) {
    const l = mk('line', { x1: padL, y1: yOf(g), x2: W - padR, y2: yOf(g), stroke: '#e5eaf2', 'stroke-width': 1 });
    svg.appendChild(l);
  }
  svg.appendChild(mk('line', { x1: padL, y1: padT, x2: padL, y2: H - padB, stroke: '#c3cddb', 'stroke-width': 1.2 }));
  svg.appendChild(mk('line', { x1: padL, y1: H - padB, x2: W - padR, y2: H - padB, stroke: '#c3cddb', 'stroke-width': 1.2 }));
  // 预测曲线（平均稳定度）
  if (f.curve && f.curve.length > 1) {
    const d = f.curve.map((p, i) => `${i ? 'L' : 'M'}${xOf(p.t).toFixed(1)},${yOf(p.r).toFixed(1)}`).join(' ');
    const path = mk('path', { d, fill: 'none', stroke: '#3b82f6', 'stroke-width': 2 });
    svg.appendChild(path);
  }
  // 实测桶点（蓝点）
  (f.buckets || []).forEach((b, i) => {
    if (!b.count) return;
    const t = [1.5, 5.5, 11, 22, 45, 80][i] || 20;
    const c = mk('circle', { cx: xOf(t), cy: yOf(b.avg_r), r: 4, fill: '#22c55e', stroke: '#fff', 'stroke-width': 1.5 });
    svg.appendChild(c);
  });
  // 目标保持率虚线
  const target = (parseFloat(localStorage.getItem('fsrsRetention') || '0.9') || 0.9);
  const tl = mk('line', { x1: padL, y1: yOf(target), x2: W - padR, y2: yOf(target), stroke: '#f59e0b', 'stroke-width': 1, 'stroke-dasharray': '4 3' });
  svg.appendChild(tl);
  // 坐标点标
  for (const t of [0, 10, 20, 30]) {
    const lbl = mk('text', { x: xOf(t), y: H - 6, 'text-anchor': 'middle', 'font-size': '9px', fill: '#94a3b8' }, `${t}d`);
    svg.appendChild(lbl);
  }
  if (stats) {
    const total = f.buckets.reduce((a, b) => a + b.count, 0);
    stats.innerHTML = total
      ? t('fsrs.stats').replace('{n}', total).replace('{s}', f.avg_stability || 0) + '<span class="text-muted">' + t('fsrs.legend') + '</span>'
      : '<span class="text-muted">' + t('msg.noFsrs') + '</span>';
  }
}

// ── D6 游戏化 ──
function drawGamification(g) {
  const el = document.getElementById('gameCard');
  if (!el) return;
  if (!g || g.total_reviews === undefined) { el.innerHTML = '<div class="text-muted text-sm">' + t('msg.noData') + '</div>'; return; }
  const unlocked = (g.badges || []).filter(b => b.unlocked);
  el.innerHTML = `<div class="flex-between mb-8">
    <div><span class="text-sm">${t('stat.xpTotal')}</span><div class="text-xl">${g.total_xp}</div></div>
    <div><span class="text-sm">${t('stat.xpToday')}</span><div class="text-xl">${g.today_xp}</div></div>
    <div><span class="text-sm">${t('stat.streak')}</span><div class="text-xl">🔥 ${g.streak}</div></div>
    <div><span class="text-sm">${t('stat.totalRev')}</span><div class="text-xl">${g.total_reviews}</div></div>
  </div>
  <div class="flex wrap gap-8">${(g.badges || []).map(b =>
    `<span class="tag ${b.unlocked ? 'tag-green' : 'tag-gray'}" title="${escapeHtml(b.label)}">${b.unlocked ? '🏅' : '🔒'} ${escapeHtml(b.id.replace('_',' '))}</span>`
  ).join('') || '<span class="text-muted text-sm">' + t('stat.badgeFirst') + '</span>'}</div>`;
}

// ── C6 AI 遥测 ──
function drawTelemetry(t) {
  const el = document.getElementById('telemetryCard');
  if (!el) return;
  if (!t || t.calls === undefined) { el.innerHTML = '<div class="text-muted text-sm">' + t('msg.noData') + '</div>'; return; }
  const rate = t.fail_rate > 0.3 ? 'tag-red' : t.fail_rate > 0.1 ? 'tag-warn' : 'tag-green';
  el.innerHTML = `<div class="flex-between mb-8">
    <span class="text-sm">${t('stat.calls7')}</span><b>${t.calls}</b>
    <span class="text-sm">${t('stat.failRate')}</span><span class="tag ${rate}">${(t.fail_rate * 100).toFixed(0)}%</span>
    <span class="text-sm">${t('stat.avgLatency')}</span><b>${t.avg_latency_ms}ms</b>
    <span class="text-sm">${t('stat.tokens')}</span><b>${t.tokens}</b>
  </div>
  ${t.slow_routes && t.slow_routes.length ? `<p class="hint-text">${t('stat.slowRoutes')}：${t.slow_routes.map(escapeHtml).join('、')}</p>` : ''}`;
}

// ── D5 周报 ──
function drawWeekly(w) {
  const el = document.getElementById('weeklyCard');
  if (!el) return;
  if (!w || w.week_start === undefined) { el.innerHTML = '<div class="text-muted text-sm">' + t('msg.noData') + '</div>'; return; }
  const delta = (w.review_delta || 0);
  const deltaStr = delta > 0 ? `+${delta}` : String(delta);
  el.innerHTML = `<div class="flex-between mb-8">
    <span class="text-sm">${t('stat.weekRange').replace('{s}', escapeHtml(w.week_start))}</span>
  </div>
  <div class="flex-between mb-8">
    <span class="text-sm">${t('stat.newProblem')}</span><b>${w.new_problems}（${t('stat.lastWeek').replace('{n}', w.prev_problems)}）</b>
    <span class="text-sm">${t('stat.reviewCount')}</span><b>${w.week_reviews}（${deltaStr}）</b>
    <span class="text-sm">${t('stat.goodRate')}</span><b>${(w.good_rate * 100).toFixed(0)}%</b>
  </div>
  <p class="hint-text">${t('report.tip').replace('{t}', t(w.tip_key || 'report.tipWeekNone'))}</p>`;
}

// ── P0 顽固错题 ──
function drawStubborn(list) {
  const el = document.getElementById('stubbornList');
  if (!el) return;
  if (!list || !list.length) { el.innerHTML = t('msg.noStubborn'); return; }
  el.innerHTML = list.map(p => {
    const rate = p.total_reviews ? Math.round(p.miss_count / p.total_reviews * 100) : 0;
    return `<div class="flex-between mb-8">
      <a href="#" onclick="event.preventDefault();viewProblem(${p.id});return false;">${escapeHtml(p.title)}</a>
      <span class="text-muted">${t('forgetting.missed').replace('{n}', p.miss_count).replace('{p}', rate).replace('{m}', p.mastery)}</span>
    </div>`;
  }).join('') + '<p class="hint-text mt-8">' + t('stubborn.hint') + '</p>';
}

// ── P0 复习压力指数（PI）──
function drawPressure(p) {
  const el = document.getElementById('pressureCard');
  if (!el) return;
  const color = p.level === t('pressure.high') ? 'var(--danger,#ef4444)' : p.level === t('pressure.mid') ? 'var(--warning)' : 'var(--success)';
  el.innerHTML = p.total == null ? '—' :
    `<div class="flex-between"><span class="text-sm">${t('pressure.score').replace('{s}', p.score)} <b style="color:${color}">${p.level}</b></span>
     <span class="text-sm text-muted">${t('pressure.detail').replace('{o}', p.overdue).replace('{t}', p.today).replace('{tm}', p.tomorrow).replace('{e}', p.est_minutes)}</span></div>
     <div class="error-bar-track mt-8"><div class="error-bar-fill" style="width:${Math.min(100, p.score)}%;background:${color}"></div></div>
     <p class="hint-text mt-8">${p.overdue > 0 ? t('pressure.overdue').replace('{n}', p.overdue) : t('pressure.noOverdue')}</p>`;
}

// ── P0 遗忘预测（FSRS R 值）──
function drawForgetPredict(f) {
  const el = document.getElementById('forgetCard');
  if (!el) return;
  if (!f.count) { el.innerHTML = t('msg.noRisk'); return; }
  const pct = (r) => (r * 100).toFixed(0) + '%';
  el.innerHTML =
    `<div class="text-sm">${t('forget.overview').replace('{n}', f.count).replace('{r}', pct(f.avg_r)).replace('{h}', f.high_risk).replace('{m}', f.medium_risk)}</div>
     ${f.top && f.top.length ? `<div class="mt-8 text-sm">${t('forget.top')}：${f.top.map(t => `<a href="#" onclick="event.preventDefault();viewProblem(${t.problem_id});return false;">${escapeHtml(t.title)}（R=${pct(t.r)}）</a>`).join('、')}</div>` : ''}
     <p class="hint-text mt-8">${t('forget.rehint')}</p>`;
}

// ── P0 今日任务清单 ──
function drawTodayTasks(tasks) {
  const el = document.getElementById('taskCard');
  if (!el) return;
  if (!tasks || !tasks.length) { el.innerHTML = t('msg.noTask'); return; }
  const icons = { review: '📚', error_focus: '🎯', exam: '🏃', done: '✅' };
  el.innerHTML = tasks.map(t =>
    `<div class="flex-between mb-8"><span class="text-sm">${icons[t.kind] || ''} ${escapeHtml(t.label)}</span>
     ${t.kind === 'review' && t.count ? `<a class="btn btn-secondary btn-sm" href="#review">${t('task.goReviewBtn')}</a>` : ''}</div>`
  ).join('');
}

// ── C6 错因分布（横向条形）──
function drawErrorDist(list) {
  const el = document.getElementById('errorDist');
  if (!el) return;
  if (!list || !list.length) { el.innerHTML = '<div class="empty"><p>' + t('msg.noData') + '</p></div>'; return; }
  const total = list.reduce((n, e) => n + e.count, 0);
  const colors = {1:'#ef4444',2:'#f97316',3:'#f59e0b',4:'#3b82f6',5:'#22c55e'};
  el.innerHTML = list.map(e => {
    const pct = Math.round(e.count / total * 100);
    return `<div class="error-bar-row">
      <span class="text-sm" style="min-width:88px">${escapeHtml(e.label)}</span>
      <span class="error-bar-track"><span class="error-bar-fill" style="width:${pct}%;background:${colors[Math.round(e.avg_mastery)] || '#94a3b8'}"></span></span>
      <span class="text-sm text-muted" style="min-width:52px;text-align:right">${t('errDist.item').replace('{n}', e.count).replace('{p}', pct)}</span>
    </div>`;
  }).join('');
  el.insertAdjacentHTML('beforeend', '<p class="hint-text mt-8">' + t('errDist.hint') + '</p>');
}

// ── 错题（真分页 + 搜索 + 排序）──
let problemPage = 1;
let problemPages = 1;
let _searchTimer = null;

function onSearchInput() {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => loadProblems(1), 250);
}

async function loadProblems(page = 1) {
  problemPage = page;
  const listEl = document.getElementById('problemsList');
  listEl.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  const q = document.getElementById('searchInput').value.trim();
  const sort = document.getElementById('sortSelect').value;
  const params = new URLSearchParams({ page, limit: 20, q, sort });
  const prereqId = getHashParam('prereq');
  if (prereqId) params.set('prereq', prereqId);
  const filterHint = document.getElementById('prereqFilterHint');
  if (filterHint) filterHint.style.display = prereqId ? 'flex' : 'none';
  try {
    const data = await api(`/api/problems?${params.toString()}`);
    problemPages = data.pages || 1;
    const items = data.items || data;
    if (!items.length) {
      listEl.innerHTML = '<div class="empty"><p>' + t('detail.emptyList') + '</p></div>';
    } else {
      listEl.innerHTML = items.map(p => `
        <div class="list-item" style="display:flex;gap:10px;align-items:flex-start">
          <input type="checkbox" style="margin-top:3px;accent-color:var(--accent)" onclick="event.stopPropagation();toggleBatch(${p.id},this.checked)" aria-label="${t('detail.pickAria')}">
          <div style="flex:1" onclick="viewProblem(${p.id})">
            <div class="list-item-header">
              <span class="list-item-title">${p.starred ? '⭐ ' : ''}${escapeHtml(p.title)}${miniTrendDots(p.recent_results)}</span>
              ${masteryTag(p.mastery)}
          </div>
          <div class="list-item-meta">${escapeHtml(p.course)} · ${escapeHtml(p.topic)} · ${escapeHtml(p.error_type)} · ${escapeHtml(p.created_at)}</div>
          </div>
        </div>`).join('');
    }
    renderPager();
  } catch(e) { toast(e.message, 'error'); }
}

function renderPager() {
  const pager = document.getElementById('pager');
  if (problemPages <= 1) { pager.innerHTML = ''; return; }
  pager.innerHTML = `
    <button class="btn btn-secondary btn-sm" ${problemPage <= 1 ? 'disabled' : ''} onclick="loadProblems(${problemPage - 1})">${t('pager.prev')}</button>
    <span class="text-sm text-muted">${problemPage} / ${problemPages}</span>
    <button class="btn btn-secondary btn-sm" ${problemPage >= problemPages ? 'disabled' : ''} onclick="loadProblems(${problemPage + 1})">${t('pager.next')}</button>
  `;
}

async function viewProblem(id) {
  try {
    const p = await api(`/api/problems/${id}`);
    document.getElementById('modalTitle').textContent = (p.starred ? '⭐ ' : '') + p.title;
    let html = `
      <div class="flex gap-8 mb-8">
        <span class="tag tag-blue">${escapeHtml(p.course || t('detail.noCourse'))}</span>
        <span class="tag tag-gray">${escapeHtml(p.topic || t('detail.noTopic'))}</span>
        ${masteryTag(p.mastery)}
      </div>`;
    if (Array.isArray(p.tags) && p.tags.length) {
      html += `<div class="flex gap-8 mb-8" style="flex-wrap:wrap">${p.tags.map(t =>
        `<span class="chip${p.tags_status === 'suggested' ? ' pending' : ''}">${escapeHtml(String(t))}</span>`).join('')}</div>`;
    }
    // A2 先修告警：绑定概念的先修掌握度低时提示
    if (Array.isArray(p.prereq_warnings) && p.prereq_warnings.length) {
      html += `<div style="border:1px solid var(--warning);background:var(--warning-light,rgba(240,180,60,.12));border-radius:8px;padding:10px 12px;margin-bottom:12px">
        <div style="font-size:13px;font-weight:600;color:var(--warning);margin-bottom:4px">${t('detail.prereqWarn')}</div>
        ${p.prereq_warnings.map(w =>
          `<span class="tag tag-warn" style="cursor:pointer;margin:2px" title="${t('detail.prereqTitle')}" onclick="openPrereqMode(${w.concept_id})">${escapeHtml(w.name)} ${w.mastery}%</span>`).join('')}
        <div class="text-sm text-muted" style="margin-top:4px">${t('detail.prereqAdvice')}</div>
      </div>`;
    }
    html += `<div class="card" style="border-color:var(--border);margin-bottom:12px">
      <div class="card-title">${t('detail.content')}</div>
      <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(p.content)}</p>
      ${(p.media_list || []).map(m => `<img class="photo-full" src="/${escapeHtml(m)}" alt="${t('common.photoAlt')}" onclick="window.open('/${escapeHtml(m)}','_blank')">`).join('')}
    </div>`;
    if (p.my_attempt) {
      html += `<div class="card" style="border-color:var(--border);margin-bottom:12px">
        <div class="card-title">${t('detail.myAttempt')}</div>
        <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(p.my_attempt)}</p>
      </div>`;
    }
    html += `<div class="card-title mt-16">${t('detail.hintsTitle')}</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},1)" id="hint1btn">${t('detail.hint1')}</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},2)" id="hint2btn">${t('detail.hint2')}</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},3)" id="hint3btn">${t('detail.hint3')}</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},4)" id="hint4btn">${t('detail.hint4')}</button>
      </div>
      <div id="hintsArea"></div>`;
    if (p.hints && p.hints.length) {
      p.hints.forEach(h => {
        html += `<div class="hint-card"><h4>${t('detail.hintLevel').replace('{l}', h.level)}</h4><p>${escapeHtml(h.content)}</p></div>`;
      });
    }
    html += `<div class="card-title mt-16">${t('detail.methodsTitle')}</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="addMethod(${id})">${t('detail.addMethod')}</button>
      </div>
      <div id="methodsArea">${renderMethods(p.methods || [], id)}</div>
      <div class="card-title mt-16">${t('detail.variantsTitle')}</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="generateVariants(${id})" id="genVariantsBtn">${t('detail.genVariants')}</button>
        <button class="btn btn-primary btn-sm hidden" onclick="saveVariants(${id})" id="saveVariantsBtn">${t('detail.saveVariants')}</button>
      </div>
      <div id="variantsArea"></div>
      <div id="savedVariants"></div>
      <div class="card-title mt-16">${t('detail.feynmanTitle')}</div>
      <p class="text-sm text-muted" style="margin-bottom:8px">${t('detail.feynmanDesc')}</p>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="startFeynman(${id})">${t('detail.startFeynman')}</button>
      </div>
      <div id="feynmanReview"></div>`;
    if (p.feynman_self_review) renderFeynmanReview(p.feynman_self_review);
    renderSavedVariants(p.variants);
    html += `<div class="flex gap-12 mt-16">
      <button class="btn btn-secondary btn-sm" onclick="editProblem(${id})">${t('detail.edit')}</button>
      <button class="btn btn-secondary btn-sm" onclick="toggleStar(${id})">${p.starred ? t('detail.starred') : t('detail.star')}</button>
      <button class="btn btn-danger btn-sm" onclick="deleteProblem(${id})">${t('prob.delete')}</button>
    </div>
    <div id="problemHistory" class="mt-16"></div>
    <div id="relatedProblems" class="mt-16"></div>
    <p class="text-sm text-muted mt-12" style="opacity:0.6">${t('detail.shortcut')}</p>`;
    document.getElementById('modalBody').innerHTML = html;
    renderMath(document.getElementById('modalBody'));
    openModal('problemModal');
    // 异步加载历史 + 关联题目
    loadHistory(id);
    loadRelated(id);
    // 详情弹窗内键盘快捷键
    const modal = document.getElementById('problemModal');
    const onKey = (e) => {
      if (!modal.classList.contains('active')) return;
      const tag = (document.activeElement && document.activeElement.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === '1') getHint(id, 1);
      else if (e.key === '2') getHint(id, 2);
      else if (e.key === '3') getHint(id, 3);
      else if (e.key === '4') getHint(id, 4);
      else if (e.key === 's') toggleStar(id);
      else if (e.key === 'e') editProblem(id);
      else if (e.key === 'd') deleteProblem(id);
    };
    modal._onKey = onKey;
    document.addEventListener('keydown', onKey);
  } catch(e) { toast(e.message, 'error'); }
}

async function getHint(id, level) {
  const btn = document.getElementById(`hint${level}btn`);
  btn.disabled = true; btn.textContent = t('msg.loading');
  const area = document.getElementById('hintsArea');
  const levelName = t('hint.levelName').replace('{n}', level);
  const diagnoseHtml = (on) => on ? '<p class="hint-text" style="color:var(--warning)">' + t('hint.diagnose') + '</p>' : '';
  const card = document.createElement('div');
  card.className = 'hint-card';
  card.innerHTML = `<h4>${levelName} <span class="tag tag-green">AI</span> <span class="text-muted text-sm">${t('hint.streaming')}</span></h4><p id="hintStreamText"></p>`;
  area.appendChild(card);
  const streamText = card.querySelector('#hintStreamText');
  // C7 SSE 重连：单次流读取，断流抛错由外层重试
  const streamOnce = async () => {
    const r = await fetch(`/api/problems/${id}/hint`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'PhysicsStudyOS', 'Accept': 'text/event-stream' },
      body: JSON.stringify({ level, lang: currentLang() }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || t('msg.requestFail') + ` (${r.status})`);
    }
    const ctype = r.headers.get('Content-Type') || '';
    if (!ctype.includes('text/event-stream')) {
      const data = await r.json();
      const srcTag = data.source === 'ai' ? '<span class="tag tag-green">AI</span>' :
                     data.source === 'fallback' ? '<span class="tag tag-amber">' + t('hint.fallbackTag') + '</span>' : '<span class="tag tag-gray">' + t('hint.cacheTag') + '</span>';
      card.querySelector('.tag-green').textContent = srcTag.replace(/<[^>]+>/g, '').trim();
      streamText.textContent = data.content || streamText.textContent;
      if (data.diagnose) card.insertAdjacentHTML('afterbegin', diagnoseHtml(true));
      if (data.sources) card.insertAdjacentHTML('afterbegin', ragSourcesHtml(data.sources));
      renderMath(card);
      return true;
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let done = false;
    while (true) {
      let chunk;
      try {
        chunk = await reader.read();
      } catch (e) {
        throw new Error('stream');
      }
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop();
      for (const evt of events) {
        const evtMatch = evt.match(/^event: (.+)$/m);
        const dataMatch = evt.match(/^data: (.+)$/m);
        if (!dataMatch) continue;
        const event = evtMatch ? evtMatch[1] : 'message';
        const payload = JSON.parse(dataMatch[1]);
        if (event === 'delta') {
          streamText.textContent += payload.delta || '';
          renderMath(card);
        } else if (event === 'sources') {
          if (payload.sources && payload.sources.length) {
            card.insertAdjacentHTML('afterbegin', ragSourcesHtml(payload.sources));
          }
        } else if (event === 'done') {
          streamText.textContent = payload.content || streamText.textContent;
          renderMath(card);
          if (payload.diagnose) card.insertAdjacentHTML('afterbegin', diagnoseHtml(true));
          done = true;
        } else if (event === 'error') {
          if (payload.partial) streamText.textContent = payload.partial;
          if (payload.fallback) {
            streamText.textContent = payload.fallback;
            card.querySelector('.tag-green').textContent = t('hint.fallbackTag');
            card.querySelector('.tag-green').className = 'tag tag-amber';
            toast(t('toast.aiFormat'), 'warn');
          }
          done = true;
        }
      }
    }
    return done;
  };
  try {
    let ok = false;
    for (let attempt = 1; attempt <= 3 && !ok; attempt++) {
      try {
        ok = await streamOnce();
      } catch (e) {
        if (attempt < 3 && streamText.textContent.length) {
          toast(t('toast.reconnect').replace('{n}', attempt), 'warn');
          await new Promise(res => setTimeout(res, 800));
        } else if (attempt >= 3) {
          toast(t('toast.reconnectFail'), 'error');
        } else {
          throw e;
        }
      }
    }
    if (!ok) toast(t('toast.streamLost'), 'error');
    finishHintBtn(btn, levelName);
  } catch(e) {
    toast(e.message, 'error'); btn.disabled = false; btn.textContent = levelName;
  }
}

function finishHintBtn(btn, levelName) {
  btn.textContent = t('hint.viewed'); btn.style.opacity = '0.5';
}

function openProblemModal() { editProblem(null); }

async function editProblem(id) {
  const modal = document.getElementById('editModal');
  const titleEl = document.getElementById('editModalTitle');
  if (id) {
    titleEl.textContent = t('edit.title');
    try {
      const p = await api(`/api/problems/${id}`);
      document.getElementById('editId').value = p.id;
      document.getElementById('editTitle').value = p.title || '';
      document.getElementById('editCourse').value = p.course || '';
      document.getElementById('editTopic').value = p.topic || '';
      document.getElementById('editContent').value = p.content || '';
      document.getElementById('editAttempt').value = p.my_attempt || '';
      document.getElementById('editErrorType').value = p.error_type || t('common.pendingDiag');
      document.getElementById('editMastery').value = p.mastery || 1;
      document.getElementById('editStarred').checked = p.starred === 1;
      currentTags = Array.isArray(p.tags) ? p.tags.map(t => ({ text: String(t), pending: p.tags_status === 'suggested' })) : [];
      renderTags();
      document.getElementById('editTagInput').value = '';
      renderEditPhotos(Array.isArray(p.media_list) ? p.media_list : []);
    } catch(e) { toast(e.message, 'error'); return; }
  } else {
    titleEl.textContent = t('edit.newTitle');
    document.getElementById('editId').value = '';
    ['editTitle','editCourse','editTopic','editContent','editAttempt'].forEach(i => document.getElementById(i).value = '');
    document.getElementById('editErrorType').value = t('common.pendingDiag');
    document.getElementById('editMastery').value = 1;
    currentTags = [];
    renderTags();
    document.getElementById('editTagInput').value = '';
    renderEditPhotos([]);
  }
  document.getElementById('dupHint').textContent = '';
  openModal('editModal');
}

// ── C7 相似题查重（编辑弹窗输入时防抖）──
let _dupTimer = null;
function checkDuplicates() {
  clearTimeout(_dupTimer);
  const el = document.getElementById('dupHint');
  if (!el) return;
  el.textContent = '';
  _dupTimer = setTimeout(async () => {
    const content = document.getElementById('editContent').value.trim();
    if (content.length < 20) return;
    try {
      const topic = document.getElementById('editTopic').value.trim();
      const exclude = document.getElementById('editId').value;
      const q = new URLSearchParams({ content, topic, exclude });
      const r = await api('/api/problems/duplicates?' + q.toString());
      if (!r.duplicates || !r.duplicates.length) return;
      const links = r.duplicates.map(d =>
        `<a href="#" onclick="event.preventDefault();viewProblem(${d.id});return false;">#${d.id}（${(d.similarity*100).toFixed(0)}%）</a>`).join('、');
      el.innerHTML = t('dup.found').replace('{n}', r.duplicates.length).replace('{l}', links);
    } catch(e) { /* 静默 */ }
  }, 800);
}

// ── C7 语音输入（webkitSpeechRecognition，Chrome/Edge）──
function startVoiceInput(targetId, btnId = 'voiceBtn') {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = document.getElementById(btnId);
  const ta = document.getElementById(targetId);
  if (!SR) { toast(t('toast.noVoice'), 'warn'); return; }
  if (btn.dataset.rec === '1') {
    btn.dataset.rec = '0';
    btn.textContent = t('voice.start');
    if (_rec) { _rec.stop(); _rec = null; }
    return;
  }
  const rec = new SR();
  _rec = rec;
  rec.lang = 'zh-CN';
  rec.interimResults = true;
  btn.dataset.rec = '1';
  btn.textContent = t('voice.recording');
  rec.onresult = (ev) => {
    let text = '';
    for (let i = 0; i < ev.results.length; i++) text += ev.results[i][0].transcript;
    ta.value = ta.value.replace(/\s*$/, '') + (ta.value.trim() ? '\n' : '') + text;
  };
  rec.onend = () => {
    _rec = null;
    btn.dataset.rec = '0';
    btn.textContent = t('voice.start');
  };
  rec.onerror = (e) => {
    if (e.error !== 'aborted') toast(t('voice.fail') + ': ' + e.error, 'error');
    btn.dataset.rec = '0';
    btn.textContent = t('voice.start');
  };
  rec.start();
}
let _rec = null;

async function saveProblem() {
  const id = document.getElementById('editId').value;
  const body = {
    title: document.getElementById('editTitle').value,
    course: document.getElementById('editCourse').value,
    topic: document.getElementById('editTopic').value,
    content: document.getElementById('editContent').value,
    my_attempt: document.getElementById('editAttempt').value,
    error_type: document.getElementById('editErrorType').value,
    mastery: parseInt(document.getElementById('editMastery').value, 10),
    starred: document.getElementById('editStarred').checked ? 1 : 0,
    tags: currentTags.map(t => t.text).filter(Boolean),
    media_path: document.getElementById('editMediaPath').value,
  };
  if (!body.title.trim() || !body.content.trim()) { toast(t('toast.titleRequired'), 'error'); return; }
  try {
    if (id) {
      await api(`/api/problems/${id}`, { method: 'PUT', body });
    } else {
      await api('/api/problems', { method: 'POST', body });
    }
    toast(id ? t('msg.updated') : t('msg.created'));
    closeModal('editModal');
    loadProblems(problemPage);
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteProblem(id) {
  const ok = await confirmDialog(t('confirm.deleteProblem'));
  if (!ok) return;
  let cancelled = false;
  const toastEl = document.createElement('div');
  toastEl.className = 'toast error';
  toastEl.setAttribute('role', 'status');
  toastEl.setAttribute('aria-live', 'polite');
  const undoLink = document.createElement('a');
  undoLink.href = '#';
  undoLink.style.cssText = 'color:#fff;text-decoration:underline;cursor:pointer';
  undoLink.textContent = t('undo');
  undoLink.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    cancelled = true;
    toastEl.remove();
    toast(t('msg.deleteCancelled'), 'success');
  });
  toastEl.appendChild(document.createTextNode(t('msg.deleted') + ' · '));
  toastEl.appendChild(undoLink);
  document.body.appendChild(toastEl);
  // 10 秒倒计时后真正删除
  await new Promise(r => setTimeout(r, 10000));
  if (cancelled) { toastEl.remove(); return; }
  try {
    await api(`/api/problems/${id}`, { method: 'DELETE' });
    toastEl.remove();
    toast(t('msg.deleted'));
    closeModal('problemModal');
    loadProblems(problemPage);
  } catch(e) { toastEl.remove(); toast(e.message, 'error'); }
}

async function toggleStar(id) {
  try {
    await api('/api/problems/batch', { method: 'POST', body: { ids: [id], action: 'star' } });
    toast(t('msg.starToggled'));
    closeModal('problemModal'); viewProblem(id);
  } catch(e) { toast(e.message, 'error'); }
}

async function loadHistory(id) {
  try {
    const history = await api(`/api/problems/${id}/history`);
    const el = document.getElementById('problemHistory');
    if (!history.length) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="card-title">${t('history.title')}</div>` +
      history.map(h => {
        const labels = {1:'label.flash1',2:'label.flash2',3:'label.flash3',4:'label.flash4'};
        const cls = h.result === '4' ? 'tag-green' : h.result === '3' ? 'tag-blue' : h.result === '2' ? 'tag-amber' : 'tag-red';
        return `<span class="tag ${cls}" style="margin:1px 4px" title="${t('history.intervalTitle').replace('{d}', h.due_date).replace('{i}', h.interval_days)}">${t(labels[h.result]||'')||h.result}</span>`;
      }).join(' ');
  } catch(e) {}
}

async function loadRelated(id) {
  try {
    const related = await api(`/api/problems/${id}/related`);
    const el = document.getElementById('relatedProblems');
    if (!related.length) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="card-title">${t('related.title')}</div>` +
      related.map(r => `<span class="tag tag-gray" style="cursor:pointer;margin:1px 4px" onclick="closeModal('problemModal');viewProblem(${r.id})">${escapeHtml(r.title)}</span>`).join('');
  } catch(e) {}
}

function miniTrendDots(results) {
  if (!results || !results.length) return '';
  const colors = {1:'var(--danger)',2:'var(--warning)',3:'var(--accent)',4:'var(--success)'};
  return '<span style="display:inline-flex;gap:2px;vertical-align:middle;margin-left:6px">' +
    results.map(r => `<span style="width:6px;height:6px;border-radius:50%;background:${colors[r]||'var(--border)'}"></span>`).join('') +
    '</span>';
}

let _batchSelected = new Set();
function toggleBatch(pid, checked) {
  if (checked) _batchSelected.add(pid); else _batchSelected.delete(pid);
  document.getElementById('batchBar').classList.toggle('hidden', _batchSelected.size === 0);
  document.getElementById('batchCount').textContent = _batchSelected.size;
}
async function batchAction(action) {
  const ids = Array.from(_batchSelected);
  if (!ids.length) return;
  if (action === 'delete') {
    const ok = await confirmDialog(t('confirm.batchDelete').replace('{n}', ids.length));
    if (!ok) return;
  }
  try {
    await api('/api/problems/batch', { method: 'POST', body: { ids, action } });
    toast(t('msg.processedN').replace('{n}', ids.length));
    _batchSelected.clear();
    document.getElementById('batchBar').classList.add('hidden');
    loadProblems(problemPage);
  } catch(e) { toast(e.message, 'error'); }
}

// ── 复习 ──
async function loadTodaySummary() {
  try {
    const s = await api('/api/reviews/summary/today');
    const el = document.getElementById('todaySummaryText');
    if (!el) return;
    const errParts = Object.entries(s.error_counts || {})
      .map(([k, v]) => `${k}:${v}`).join('、');
    let tip = t('today.tipNone');
    if (s.done > 0) {
      tip = t('today.tipDone').replace('{n}', s.done).replace('{a}', s.accuracy);
      if (s.hard > 0) tip += t('today.tipHard').replace('{n}', s.hard);
      if (s.top_error) tip += t('today.tipTop').replace('{s}', escapeHtml(s.top_error));
      tip += s.done >= 3 && s.accuracy >= 80 ? t('today.tipGood') : t('today.tipRetry');
    }
    el.innerHTML = `${tip}` +
      (s.due_tomorrow > 0 ? `<br><span class="text-muted">${t('today.dueTomorrow').replace('{n}', s.due_tomorrow)}</span>` : '') +
      (errParts ? `<br><span class="text-muted">${t('today.errDist').replace('{s}', escapeHtml(errParts))}</span>` : '');
  } catch(e) { /* 复盘卡片非关键路径，失败静默 */ }
}

function toggleInterleave(checked) {
  localStorage.setItem('interleave', checked ? '1' : '0');
  loadReviews();
}

async function loadReviews() {
  const el = document.getElementById('reviewList');
  el.innerHTML = '<div class="loading">' + t('msg.loading') + '</div>';
  loadTodaySummary();
  try {
    const interleave = localStorage.getItem('interleave') !== '0';
    const mode = interleave ? '' : '?mode=plain';
    document.getElementById('interleaveToggle').checked = interleave;
    const list = await api('/api/reviews' + mode);
    if (!list.length) {
      el.innerHTML = '<div class="empty"><p>' + t('review.noneToday') + '</p></div>';
      return;
    }
    const today = new Date().toISOString().slice(0, 10);
    const dueCount = list.filter(r => r.due_date <= today).length;
    document.getElementById('reviewProgress').innerHTML = `
      <div style="height:8px;background:var(--border);border-radius:4px;overflow:hidden;margin-bottom:10px">
        <div style="height:100%;width:0%;background:var(--accent);border-radius:4px;transition:width .3s" id="reviewProgressBar"></div>
      </div>
      <span class="text-sm text-muted" id="reviewProgressText">${t('review.dueProgress').replace('{n}', dueCount).replace('{m}', 0)}</span>
    `;
    let completed = 0;
    const updateProgress = () => {
      completed++;
      const bar = document.getElementById('reviewProgressBar');
      if (bar) bar.style.width = (completed / (dueCount || 1) * 100).toFixed(0) + '%';
      const text = document.getElementById('reviewProgressText');
      if (text) text.textContent = t('review.dueProgress').replace('{n}', dueCount).replace('{m}', completed);
    };
    window._reviewUpdateProgress = updateProgress;
    el.innerHTML = list.map(r => `
      <div class="list-item">
        <div class="list-item-header">
          <span class="list-item-title">${escapeHtml(r.title)}${r.variant_id ? ` <span class="tag tag-blue">${t('review.variantTag')}</span>` : ''}${r.feynman_gaps ? ` <span class="tag tag-warn" title="${t('review.feynmanGapTitle')}">${t('review.feynmanGapTag').replace('{n}', r.feynman_gaps)}</span>` : ''}</span>
          <span class="tag ${r.due_date <= today ? 'tag-red' : 'tag-gray'}">
            ${r.due_date <= today ? t('review.dueToday') : t('review.dueSoon')}
          </span>
        </div>
        <div class="list-item-meta">${escapeHtml(r.course)} · ${escapeHtml(r.topic)} · ${t('review.dueLabel')}: ${r.due_date} · ${t('review.intervalLabel').replace('{n}', r.interval_days)}</div>
        <div class="flex gap-8 mt-12 flex-wrap">
          <button class="btn btn-danger btn-sm" onclick="completeReview(${r.id},1)">${t('rating.btn1')}</button>
          <button class="btn btn-secondary btn-sm" onclick="completeReview(${r.id},2)">${t('rating.btn2')}</button>
          <button class="btn btn-secondary btn-sm" onclick="completeReview(${r.id},3)">${t('rating.btn3')}</button>
          <button class="btn btn-primary btn-sm" onclick="completeReview(${r.id},4)">${t('rating.btn4')}</button>
          <button class="btn btn-secondary btn-sm" onclick="rescheduleReview(${r.id})">${t('review.again')}</button>
          <button class="btn btn-secondary btn-sm" onclick="viewProblem(${r.problem_id})">${t('review.viewProblem')}</button>
        </div>
      </div>`).join('');
  } catch(e) { toast(e.message, 'error'); }
}

async function completeReview(id, rating) {
  try {
    const r = await api(`/api/reviews/${id}/complete`, { method: 'POST', body: { rating } });
    const labels = {1:'label.mark1',2:'label.mark2',3:'label.mark3',4:'label.mark4'};
    toast(t('review.nextDue').replace('{r}', t(labels[rating])).replace('{d}', r.next_due).replace('{i}', r.interval_days));
    if (window._reviewUpdateProgress) window._reviewUpdateProgress();
    loadReviews();
    if (document.getElementById('page-dashboard').classList.contains('active')) loadDashboard();
  } catch(e) { toast(e.message, 'error'); }
}

async function rescheduleReview(id) {
  try {
    await api(`/api/reviews/${id}/reschedule`, { method: 'PUT' });
    toast(t('msg.rescheduledToday'));
    loadReviews();
  } catch(e) { toast(e.message, 'error'); }
}

function openPrereqMode(conceptId) {
  closeModal('problemModal');
  switchPage('problems');
  window.location.hash = `#page-problems?prereq=${conceptId}`;
  loadProblems();
}

// ── A5 Feynman 口述反转 ──
let _feynmanMode = false;

function renderFeynmanReview(sr) {
  const el = document.getElementById('feynmanReview');
  if (!el || !sr) return;
  el.innerHTML = `<div style="border:1px solid var(--border);border-radius:8px;padding:10px;background:var(--surface)">
    <div style="font-size:13px;font-weight:600;margin-bottom:6px">${t('feyn.savedTitle')}</div>
    ${sr.gaps && sr.gaps.length ? `<div class="text-sm" style="margin-bottom:4px"><b style="color:var(--warning)">${t('feyn.gaps')}</b>${sr.gaps.map(g => `<span class="tag tag-warn" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
    ${sr.wrong && sr.wrong.length ? `<div class="text-sm" style="margin-bottom:4px"><b style="color:var(--danger,#ef4444)">${t('feyn.wrong')}</b>${sr.wrong.map(g => `<span class="tag tag-red" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
    ${sr.clear && sr.clear.length ? `<div class="text-sm"><b style="color:var(--success,#22c55e)">${t('feyn.clear')}</b>${sr.clear.map(g => `<span class="tag tag-green" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
  </div>`;
}

async function startFeynman(problemId) {
  try {
    const r = await api('/api/feynman/start', { method: 'POST', body: { problem_id: problemId } });
    _feynmanMode = true;
    closeModal('problemModal');
    switchPage('oral');
    oralSessionId = r.session_id;
    oralTurn = 1;
    document.getElementById('oralStartCard').classList.add('hidden');
    document.getElementById('oralChatCard').classList.remove('hidden');
    document.getElementById('oralTopicDisplay').textContent = t('detail.feynmanTitle');
    document.getElementById('oralTurn').textContent = t('feyn.stepOf').replace('{n}', 1).replace('{m}', 3);
    document.getElementById('oralChat').innerHTML = `<div class="chat-msg assistant"><div class="bubble">${escapeHtml(r.reply)}</div></div>`;
    renderMath(document.getElementById('oralChat'));
    document.getElementById('oralAnswer').value = '';
    document.getElementById('oralAnswer').focus();
  } catch(e) { toast(e.message, 'error'); }
}

async function showFeynmanSelfReview() {
  if (!oralSessionId) return;
  try {
    const r = await api(`/api/feynman/${oralSessionId}/self-review`);
    const sr = r.saved || r.draft;
    if (!sr) { toast(t('feyn.selfEmpty'), 'error'); return; }
    const chatEl = document.getElementById('oralChat');
    chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
      <div class="card-title" style="font-size:14px">${t('feyn.reviewTitle')}</div>
      ${sr.gaps && sr.gaps.length ? `<div class="text-sm"><b>${t('feyn.gaps')}</b>${sr.gaps.map(g => `<span class="tag tag-warn" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
      ${sr.wrong && sr.wrong.length ? `<div class="text-sm"><b>${t('feyn.wrong')}</b>${sr.wrong.map(g => `<span class="tag tag-red" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
      ${sr.clear && sr.clear.length ? `<div class="text-sm"><b>${t('feyn.clear')}</b>${sr.clear.map(g => `<span class="tag tag-green" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
      <div class="flex gap-8 mt-8">
        <button class="btn btn-primary btn-sm" onclick="confirmFeynmanSelfReview()">${t('common.confirmSave')}</button>
        <button class="btn btn-secondary btn-sm" onclick="this.closest('.bubble').remove()">${t('common.close')}</button>
      </div>
    </div></div>`;
    chatEl.scrollTop = chatEl.scrollHeight;
    window._feynmanSr = sr;
  } catch(e) { toast(e.message, 'error'); }
}

async function confirmFeynmanSelfReview() {
  const sr = window._feynmanSr;
  if (!sr || !oralSessionId) return;
  try {
    await api(`/api/feynman/${oralSessionId}/self-review`, { method: 'POST', body: {
      gaps: sr.gaps || [], wrong: sr.wrong || [], clear: sr.clear || [],
    }});
    window._feynmanSr = null;
    toast(t('feyn.savedToast'));
    _feynmanMode = false;
  } catch(e) { toast(e.message, 'error'); }
}

// ── A4 举一反三 ──
let draftVariants = [];

function renderSavedVariants(variants) {
  const el = document.getElementById('savedVariants');
  if (!el) return;
  if (!variants || !variants.length) { el.innerHTML = ''; return; }
  el.innerHTML = variants.map((v, i) => {
    const q = v.correct !== undefined && v.total !== undefined ?
      ` <span class="tag ${v.correct / v.total >= 0.8 ? 'tag-green' : 'tag-amber'}">${t('variant.rate').replace('{c}', v.correct).replace('{t}', v.total)}</span>` : '';
    return `<div class="hint-card"><h4>${t('variant.item').replace('{i}', i + 1).replace('{m}', escapeHtml(v.mode || t('variant.uncat')))}${q}</h4>
      <p>${escapeHtml(v.title)}</p>
      <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(v.content)}</p>
      <p class="text-sm text-muted">${t('variant.answer').replace('{a}', escapeHtml(v.answer || '—'))}</p></div>`;
  }).join('');
}

async function generateVariants(id) {
  const btn = document.getElementById('genVariantsBtn');
  btn.disabled = true;
  try {
    const r = await api(`/api/problems/${id}/variants/generate`, { method: 'POST', body: {} });
    draftVariants = r.variants || [];
    const area = document.getElementById('variantsArea');
    area.innerHTML = draftVariants.map((v, i) => `
      <div class="hint-card">
        <h4>${t('variant.draftItem').replace('{m}', escapeHtml(v.mode || t('review.variantTag'))).replace('{i}', i + 1)}</h4>
        <p>${escapeHtml(v.title)}</p>
        <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(v.content)}</p>
        <p class="text-sm text-muted">${t('variant.ref').replace('{a}', escapeHtml(v.answer || '—'))}</p>
      </div>`).join('') || '<p class="text-sm text-muted">' + t('variant.none') + '</p>';
    document.getElementById('saveVariantsBtn').classList.remove('hidden');
    toast(r.source === 'local' ? t('variant.genLocal') : t('variant.genDraft'));
  } catch(e) { toast(e.message, 'error'); } finally { btn.disabled = false; }
}

async function saveVariants(id) {
  if (!draftVariants.length) return;
  try {
    const r = await api(`/api/problems/${id}/variants`, { method: 'POST', body: { variants: draftVariants } });
    draftVariants = [];
    document.getElementById('variantsArea').innerHTML = '';
    document.getElementById('saveVariantsBtn').classList.add('hidden');
    toast(t('variant.saved').replace('{n}', r.count).replace('{t}', r.total));
    const p = await api(`/api/problems/${id}`);
    renderSavedVariants(p.variants);
  } catch(e) { toast(e.message, 'error'); }
}

// ── F1 口试 → 复习卡 ──
let _oralDraft = null;

async function draftOralCard() {
  if (!oralSessionId) return;
  try {
    const r = await api(`/api/oral/${oralSessionId}/draft-card`, { method: 'POST', body: {} });
    _oralDraft = r.draft || {};
    const d = _oralDraft;
    const chatEl = document.getElementById('oralChat');
    chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
      <div class="card-title" style="font-size:14px">${t('draft.cardTitle')}</div>
      <div class="text-sm"><b>${t('draft.title')}</b>${escapeHtml(d.title || '')}</div>
      <div class="text-sm" style="white-space:pre-wrap"><b>${t('draft.content')}</b>${escapeHtml(d.content || '')}</div>
      <div class="text-sm">${t('draft.topicErr').replace('{t}', escapeHtml(d.topic || '')).replace('{e}', escapeHtml(d.error_type || ''))}</div>
      <div class="flex gap-8 mt-8">
        <button class="btn btn-primary btn-sm" onclick="saveOralCard()">${t('draft.saveAs')}</button>
        <button class="btn btn-secondary btn-sm" onclick="this.closest('.bubble').remove()">${t('common.discard')}</button>
      </div>
    </div></div>`;
    chatEl.scrollTop = chatEl.scrollHeight;
  } catch(e) { toast(e.message, 'error'); }
}

async function saveOralCard() {
  if (!_oralDraft) return;
  try {
    const d = _oralDraft;
    await api('/api/problems', { method: 'POST', body: {
      title: d.title || t('oral.cardTitle'), content: d.content || '', topic: d.topic || '',
      error_type: d.error_type || t('oral.defaultErr'), my_attempt: d.my_attempt || '',
      tags: d.tags || [],
    }});
    _oralDraft = null;
    toast(t('oral.savedToast'));
  } catch(e) { toast(e.message, 'error'); }
}

// ── 口试 ──
let oralSessionId = null;
let oralTurn = 0;

async function startOral() {
  _feynmanMode = false;
  const topic = document.getElementById('oralTopic').value.trim();
  if (!topic) { toast(t('oral.needTopic'), 'error'); return; }
  try {
    const r = await api('/api/oral/start', { method: 'POST', body: { topic } });
    oralSessionId = r.session_id;
    oralTurn = 1;
    document.getElementById('oralStartCard').classList.add('hidden');
    document.getElementById('oralChatCard').classList.remove('hidden');
    document.getElementById('oralTopicDisplay').textContent = t('oral.topicDisplay').replace('{t}', topic);
    document.getElementById('oralTurn').textContent = t('oral.roundOf').replace('{n}', 1).replace('{m}', 5);
    document.getElementById('oralChat').innerHTML = `<div class="chat-msg assistant"><div class="bubble">${escapeHtml(r.reply)}</div></div>`;
    renderMath(document.getElementById('oralChat'));
    document.getElementById('oralAnswer').value = '';
    document.getElementById('oralAnswer').focus();
  } catch(e) { toast(e.message, 'error'); }
}

function showThinking() {
  const chatEl = document.getElementById('oralChat');
  const div = document.createElement('div');
  div.className = 'chat-msg assistant thinking';
  div.id = 'oralThinking';
  div.innerHTML = '<div class="bubble">' + t('oral.thinking') + '</div>';
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
}

async function respondOral() {
  const answer = document.getElementById('oralAnswer').value.trim();
  if (!answer) return;
  const chatEl = document.getElementById('oralChat');
  chatEl.innerHTML += `<div class="chat-msg user"><div class="bubble">${escapeHtml(answer)}</div></div>`;
  chatEl.scrollTop = chatEl.scrollHeight;
  document.getElementById('oralAnswer').value = '';
  document.getElementById('oralAnswer').disabled = true;
  showThinking();
  try {
    const r = await api('/api/oral/respond', { method: 'POST', body: { session_id: oralSessionId, answer } });
    const t = document.getElementById('oralThinking');
    if (t) t.remove();
    oralTurn++;
    chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">${escapeHtml(r.reply)}</div></div>`;
    renderMath(chatEl);
    chatEl.scrollTop = chatEl.scrollHeight;
    if (r.finished) {
      document.getElementById('oralTurn').textContent = _feynmanMode ? t('oral.feynDone') : t('oral.ended');
      document.getElementById('oralAnswer').placeholder = t('oral.restartHint');
      if (_feynmanMode) {
        chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
          <button class="btn btn-primary btn-sm" onclick="showFeynmanSelfReview()">${t('oral.selfReviewBtn')}</button>
          <span class="text-sm text-muted">${t('oral.gapHint')}</span>
        </div></div>`;
      } else {
        chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
          <button class="btn btn-primary btn-sm" onclick="draftOralCard()">${t('oral.draftBtn')}</button>
          <span class="text-sm text-muted">${t('oral.draftHint')}</span>
        </div></div>`;
      }
      chatEl.scrollTop = chatEl.scrollHeight;
    } else {
      document.getElementById('oralTurn').textContent = _feynmanMode ? t('feyn.stepOf').replace('{n}', oralTurn + 1).replace('{m}', 3) : t('oral.roundOf').replace('{n}', oralTurn).replace('{m}', 5);
      document.getElementById('oralAnswer').disabled = false;
      document.getElementById('oralAnswer').focus();
    }
  } catch(e) {
    const t = document.getElementById('oralThinking');
    if (t) t.remove();
    toast(e.message, 'error');
    document.getElementById('oralAnswer').disabled = false;
  }
}

async function resetOral() {
  if (oralSessionId) {
    try { await api(`/api/oral/${oralSessionId}/end`, { method: 'POST', body: {} }); } catch(e) {}
  }
  oralSessionId = null; oralTurn = 0;
  document.getElementById('oralStartCard').classList.remove('hidden');
  document.getElementById('oralChatCard').classList.add('hidden');
  document.getElementById('oralTopic').value = '';
}

// ── C5 学习者档案 ──
// ── C7 错因趋势（近 30 天 vs 历史）──
function drawErrorTrend(list) {
  const el = document.getElementById('errorTrend');
  if (!el) return;
  if (!list || !list.length) { el.innerHTML = '<div class="empty"><p>' + t('msg.noData') + '</p></div>'; return; }
  el.innerHTML = list.map(t => {
    const up = t.delta > 0, down = t.delta < 0;
    const arrow = up ? '↗' : down ? '↘' : '→';
    const cls = up ? 'tag-red' : down ? 'tag-green' : 'tag-gray';
    return `<div class="flex-between mb-8">
      <span class="text-sm" style="min-width:88px">${escapeHtml(t.label)}</span>
      <span class="text-sm text-muted" style="flex:1">${t('errTrend.range').replace('{n}', t.recent_count).replace('{p}', t.recent_pct).replace('{h}', t.total_pct)}</span>
      <span class="tag ${cls}">${arrow} ${up ? '+' : ''}${t.delta}%</span>
    </div>`;
  }).join('');
}

// ── C7 考试冲刺卡（倒计时 + 每日计划）──
function renderSprint(goal, stats) {
  const el = document.getElementById('sprintCard');
  if (!el) return;
  if (!goal || !goal.exam_date) { el.innerHTML = t('sprint.noGoal'); return; }
  const days = Math.ceil((new Date(goal.exam_date) - new Date()) / 86400000);
  const target = goal.exam_target_score ? t('sprint.target').replace('{s}', goal.exam_target_score) : '';
  const total = stats ? (stats.total || 0) : 0;
  const mastered = stats ? (stats.mastered || 0) : 0;
  const remaining = Math.max(0, total - mastered);
  let plan = '';
  if (days > 0 && remaining > 0) {
    const perDay = Math.ceil(remaining / days);
    plan = t('sprint.plan').replace('{d}', days).replace('{r}', remaining).replace('{p}', perDay);
  } else if (days <= 0) {
    plan = `<span class="tag tag-red">${t('sprint.passed')}</span>`;
  } else {
    plan = t('sprint.done');
  }
  el.innerHTML = `<div class="text-sm">${plan}</div>
    <div class="text-sm text-muted mt-8">${escapeHtml(goal.exam_date || '')} ${escapeHtml(target)}</div>`;
}

async function loadProfile(dash) {
  try {
    const p = await api('/api/profile');
    const el = document.getElementById('profileBox');
    if (!el) return;
    const topicLine = (p.topics || []).slice(0, 3).map(t =>
      `${escapeHtml(t.topic)} ${t.avg_mastery}/5`).join('、');
    const errLine = (p.errors || []).slice(0, 4).map(e =>
      `${escapeHtml(e.error_type)}×${e.count}`).join('、');
    const pace = p.pace || {};
    const goal = p.goal || {};
    let goalText = t('profile.noGoal');
    if (goal.exam_date) {
      const days = Math.ceil((new Date(goal.exam_date) - new Date()) / 86400000);
      goalText = t('profile.goalText').replace('{d}', goal.exam_date).replace('{n}', days) + (goal.exam_target_score ? '，' + t('sprint.target').replace('{s}', goal.exam_target_score) : '');
    }
    el.innerHTML =
      t('profile.topicLine').replace('{v}', topicLine || t('profile.noneTopics')) + '<br>' +
      t('profile.errLine').replace('{v}', errLine || t('profile.noneErrors')) + '<br>' +
      t('profile.paceLine').replace('{r}', pace.week_reviews).replace('{n}', pace.week_new_problems).replace('{h}', pace.peak_hour) + '<br>' +
      t('profile.goalLine').replace('{v}', escapeHtml(goalText));
    document.getElementById('profExamDate').value = goal.exam_date || '';
    document.getElementById('profExamScore').value = goal.exam_target_score || '';
    renderSprint(goal, dash ? dash.stats : null);
  } catch(e) { /* 可选 */ }
}

async function saveProfile() {
  try {
    await api('/api/profile', {
      method: 'PUT',
      body: {
        exam_date: document.getElementById('profExamDate').value,
        exam_target_score: document.getElementById('profExamScore').value,
      },
    });
    toast(t('profile.saved'));
    loadProfile();
  } catch(e) { toast(e.message, 'error'); }
}

// ── C7 打印错题集（尊重当前搜索/排序，全量拉取）──
async function printProblems() {
  const q = document.getElementById('searchInput').value.trim();
  const sort = document.getElementById('sortSelect').value;
  const params = new URLSearchParams({ page: 1, limit: 10000, q, sort });
  try {
    const data = await api('/api/problems?' + params.toString());
    const items = data.items || data;
    if (!items.length) { toast(t('print.noItems'), 'warn'); return; }
    const area = document.getElementById('printArea');
    const sorted = [...items].sort((a, b) => (b.mastery || 0) - (a.mastery || 0));
    area.innerHTML = `<h2>${t('print.bookTitle').replace('{n}', items.length).replace('{d}', new Date().toLocaleDateString())}</h2>` +
      sorted.map(p => `<div class="print-item">
        <div class="print-title">${t('print.pTitle').replace('{t}', escapeHtml(p.title || t('print.unnamed'))).replace('{m}', p.mastery)}</div>
        <div class="print-meta">${t('print.meta').replace('{c}', escapeHtml(p.course || '')).replace('{t}', escapeHtml(p.topic || '')).replace('{e}', escapeHtml(p.error_type || t('common.pendingDiag')))}</div>
        <pre>${escapeHtml(p.content || '')}</pre>
        ${p.my_attempt ? `<div class="print-hdr">${t('print.myAttempt')}</div><pre>${escapeHtml(p.my_attempt)}</pre>` : ''}
        ${p.fix_action ? `<div class="print-hdr">${t('print.fixAction')}</div><pre>${escapeHtml(p.fix_action)}</pre>` : ''}
      </div>`).join('');
    window.print();
  } catch(e) { toast(e.message, 'error'); }
}

// ── P0 打印增强：考前自测卷（隐藏答案，薄弱优先 + 同知识点隔开）──
async function printQuizSheet() {
  const q = document.getElementById('searchInput').value.trim();
  const params = new URLSearchParams({ page: 1, limit: 10000, q });
  try {
    const data = await api('/api/problems?' + params.toString());
    const items = data.items || data;
    if (items.length < 3) { toast(t('print.tooFew'), 'warn'); return; }
    const quiz = [...items]
      .sort((a, b) => (a.mastery || 0) - (b.mastery || 0))
      .slice(0, 30);
    const buckets = {};
    for (const p of quiz) {
      const k = p.topic || t('variant.uncat');
      (buckets[k] = buckets[k] || []).push(p);
    }
    const ordered = [];
    let last = null;
    while (ordered.length < quiz.length) {
      const pool = Object.keys(buckets).filter(k => k !== last && buckets[k].length);
      const k = pool.length ? pool.reduce((a, b) => buckets[a].length >= buckets[b].length ? a : b)
                            : Object.keys(buckets).find(k => buckets[k].length);
      ordered.push(buckets[k].shift());
      last = k;
      if (buckets[k].length === 0) delete buckets[k];
    }
    const minutes = Math.max(5, Math.round(quiz.length * 1.5));
    document.getElementById('printArea').innerHTML =
      `<h2>${t('print.quizTitle').replace('{n}', quiz.length).replace('{m}', minutes).replace('{d}', new Date().toLocaleDateString())}</h2>
      <p class="hint-text">${t('print.quizHint')}</p>
      ${ordered.map((p, i) => `<div class="print-item">
        <div class="print-title">${t('print.quizItem').replace('{i}', i + 1).replace('{c}', escapeHtml(p.course || '')).replace('{t}', escapeHtml(p.topic || ''))}</div>
        <pre>${escapeHtml(p.content || '')}</pre>
        <div class="print-answer-line">${t('print.answerLine')}</div>
      </div>`).join('')}`;
    window.print();
  } catch(e) { toast(e.message, 'error'); }
}

// ── P0 闪电复习（快速翻卡，忘了=1 / 记得=4）──
let _flashQueue = [];
let _flashIdx = 0;
let _flashDone = 0;

async function startFlashReview() {
  const list = await api('/api/reviews');
  if (!list.length) { toast(t('flash.noToday'), 'warn'); return; }
  _flashQueue = list;
  _flashIdx = 0;
  _flashDone = 0;
  document.getElementById('flashBody').innerHTML = `
    <div class="flash-count text-sm text-muted mb-8" id="flashCount"></div>
    <div class="flash-front">
      <div class="text-muted text-sm mb-8" id="flashMeta"></div>
      <pre class="flash-content" id="flashContent">${t('msg.loading')}</pre>
    </div>
    <div class="flash-back hidden" id="flashBack">
      <div class="print-hdr">${t('print.myAttempt')}</div>
      <pre class="flash-content" id="flashAttempt"></pre>
      <div class="print-hdr">${t('print.fixAction')}</div>
      <pre class="flash-content" id="flashFix"></pre>
    </div>
    <p class="hint-text text-center">${t('flash.hint')}</p>`;
  document.querySelector('#flashModal .modal-footer').classList.remove('hidden');
  openModal('flashModal');
  _flashRender();
}

function _flashRender() {
  const r = _flashQueue[_flashIdx];
  if (!r) { _flashFinish(); return; }
  document.getElementById('flashCount').textContent =
    t('flash.count').replace('{n}', _flashIdx + 1).replace('{m}', _flashQueue.length).replace('{d}', _flashDone);
  document.getElementById('flashMeta').textContent =
    t('flash.meta').replace('{c}', escapeHtml(r.course || '')).replace('{t}', escapeHtml(r.topic || '')).replace('{e}', escapeHtml(r.error_type || t('common.pendingDiag')));
  document.getElementById('flashContent').textContent = r.content || t('flash.noContent');
  document.getElementById('flashAttempt').textContent = r.my_attempt || t('flash.noAttempt');
  document.getElementById('flashFix').textContent = r.fix_action || t('flash.noFix');
  flashFlip(true);
}

function flashFlip(forceBack) {
  const back = document.getElementById('flashBack');
  if (forceBack === true || !back.classList.contains('hidden')) {
    back.classList.add('hidden');
    return;
  }
  back.classList.remove('hidden');
}

async function flashRate(rating) {
  const r = _flashQueue[_flashIdx];
  _flashDone++;
  try { await api(`/api/reviews/${r.id}/complete`, { method: 'POST', body: { rating } }); }
  catch(e) { toast(e.message, 'error'); }
  _flashIdx++;
  _flashRender();
}

function _flashFinish() {
  const el = document.getElementById('flashBody');
  el.innerHTML = `<div class="text-center" style="padding:30px 0">
    <h3 style="margin-bottom:12px">${t('flash.doneTitle')}</h3>
    <p class="text-muted">${t('flash.doneDesc').replace('{n}', _flashQueue.length)}</p>
  </div>`;
  document.querySelector('#flashModal .modal-footer').classList.add('hidden');
  loadReviews();
}

document.addEventListener('keydown', e => {
  if (!document.getElementById('flashModal').classList.contains('active')) return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); flashRate(1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); flashRate(4); }
});

// ── A8 一题多解 ──
function renderMethods(methods, id) {
  if (!Array.isArray(methods) || !methods.length) {
    return '<p class="text-sm text-muted">' + t('method.none') + '</p>';
  }
  return '<div class="flex column gap-8">' + methods.map((m, i) =>
    `<div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px">
      <div class="flex-between mb-4">
        <b class="text-sm">${t('method.title').replace('{i}', i + 1)}</b>
        <button class="btn btn-secondary btn-sm" onclick="removeMethod(${id},${i})">${t('method.delete')}</button>
      </div>
      <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(m)}</p>
    </div>`).join('') + '</div>';
}

async function addMethod(id) {
  const text = window.prompt(t('method.prompt'), '');
  if (text === null) return;
  const p = await api(`/api/problems/${id}`);
  const methods = [...(p.methods || []), text.trim()].filter(Boolean);
  try {
    await api(`/api/problems/${id}`, { method: 'PUT', body: { methods } });
    document.getElementById('methodsArea').innerHTML = renderMethods(methods, id);
    toast(t('method.saved'));
    renderMath(document.getElementById('methodsArea'));
  } catch(e) { toast(e.message, 'error'); }
}

async function removeMethod(id, idx) {
  const p = await api(`/api/problems/${id}`);
  const methods = (p.methods || []).filter((_, i) => i !== idx);
  await api(`/api/problems/${id}`, { method: 'PUT', body: { methods } });
  document.getElementById('methodsArea').innerHTML = renderMethods(methods, id);
  toast(t('method.deleted'));
}

// ── 设置 ──
async function probeLocalModels() {
  const el = document.getElementById('ollamaStatus');
  if (!el) return;
  try {
    const r = await api('/api/models/probe');
    if (r.ollama && r.ollama.available) {
      const list = (r.ollama.models || []).slice(0, 5).join(', ');
      el.innerHTML = t('ollama.available').replace('{l}', escapeHtml(list));
      el.style.color = 'var(--success)';
    } else {
      el.textContent = t('ollama.noLocal');
      el.style.color = 'var(--text-2)';
    }
  } catch(e) {
    el.textContent = t('ollama.probeFail');
  }
}

// ── B1 拍照/截图录题 ──
let _editPhotos = []; // 当前表单已上传图片相对路径

function renderEditPhotos(paths) {
  _editPhotos = (paths || []).filter(Boolean);
  document.getElementById('editMediaPath').value = _editPhotos.join(',');
  const wrap = document.getElementById('photoPreviewWrap');
  const btn = document.getElementById('extractPhotoBtn');
  const delBtn = document.getElementById('clearPhotoBtn');
  if (_editPhotos.length) {
    wrap.classList.remove('hidden');
    wrap.innerHTML = _editPhotos.map(p =>
      `<span class="photo-preview"><img src="/${escapeHtml(p)}" alt="${t('common.photoAlt')}"></span>`).join('');
    btn.classList.remove('hidden');
    delBtn.classList.remove('hidden');
  } else {
    wrap.classList.add('hidden');
    wrap.innerHTML = '';
    btn.classList.add('hidden');
    delBtn.classList.add('hidden');
  }
}

async function uploadPhotoBlob(blob) {
  const b64 = await new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(',')[1] || '');
    fr.onerror = reject;
    fr.readAsDataURL(blob);
  });
  const r = await api('/api/upload/photo', { method: 'POST', body: { data: b64, name: blob.name || 'paste.png' } });
  return r.path;
}

async function attachPhoto(blob) {
  try {
    const path = await uploadPhotoBlob(blob);
    renderEditPhotos([..._editPhotos, path]);
    toast(t('photo.uploaded'), 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function extractPhoto() {
  const path = _editPhotos[_editPhotos.length - 1];
  if (!path) return;
  const btn = document.getElementById('extractPhotoBtn');
  btn.disabled = true;
  btn.textContent = t('ocr.recognizing');
  try {
    const r = await api('/api/ai/extract-photo', { method: 'POST', body: { media_path: path } });
    if (!r.draft) {
      toast(r.error || t('ocr.noVision'), 'info');
      return;
    }
    const d = r.draft;
    if (d.title) document.getElementById('editTitle').value = d.title;
    if (d.topic) document.getElementById('editTopic').value = d.topic;
    if (d.content) document.getElementById('editContent').value = d.content;
    if (d.answer) {
      const hint = document.getElementById('editContent').value;
      document.getElementById('editContent').value = hint + (hint ? '\n\n' : '') +
        t('ocr.answerBlock').replace('{a}', d.answer) + (d.analysis ? t('ocr.analysisBlock').replace('{a}', d.analysis) : '');
    }
    toast(t('ocr.filled'), 'success');
  } catch(e) { toast(e.message, 'error'); }
  finally {
    btn.disabled = false;
    btn.textContent = t('ocr.btnTitle');
  }
}

function clearPhoto() { renderEditPhotos([]); }

// 全局粘贴：编辑弹窗打开时，剪贴板图片 → 上传
document.addEventListener('paste', (e) => {
  if (!document.getElementById('editModal').classList.contains('active')) return;
  const items = (e.clipboardData && e.clipboardData.items) || [];
  for (const it of items) {
    if (it.type && it.type.startsWith('image/')) {
      e.preventDefault();
      attachPhoto(it.getAsFile());
      return;
    }
  }
});
document.getElementById('editPhotoFile').addEventListener('change', (e) => {
  const f = e.target.files && e.target.files[0];
  if (f) attachPhoto(f);
  e.target.value = '';
});

// ── B3 教材库 RAG ──
function ragSourcesHtml(sources) {
  if (!sources || !sources.length) return '';
  return `<div class="rag-sources"><span class="text-sm text-muted">${t('rag.srcLabel')}</span>` +
    sources.map(s =>
      `<button class="btn btn-link btn-sm" onclick="openRagSource('${encodeURIComponent(s.path)}')">` +
      `${escapeHtml(s.name)}${s.page ? t('rag.pageSuffix').replace('{p}', s.page) : ''}</button>`).join('') + `</div>`;
}

async function openRagSource(encPath) {
  try {
    await api(`/api/rag/open?path=${encPath}`);
  } catch(e) { toast(e.message, 'error'); }
}

async function ingestRag() {
  const path = document.getElementById('ragPath').value.trim();
  if (!path) { toast(t('rag.needPath'), 'error'); return; }
  const status = document.getElementById('ragStatus');
  status.textContent = t('rag.ingesting');
  try {
    const r = await api('/api/rag/ingest', { method: 'POST', body: { path } });
    status.textContent = t('rag.ingested').replace('{d}', r.docs || 1).replace('{c}', r.chunks) +
      (r.errors && r.errors.length ? t('rag.skipped').replace('{s}', r.errors.join('；')) : '');
    toast(t('rag.ingestDone'));
    loadRagDocs();
  } catch(e) {
    status.textContent = '';
    toast(e.message, 'error');
  }
}

async function loadRagDocs() {
  const el = document.getElementById('ragDocList');
  if (!el) return;
  try {
    const r = await api('/api/rag/docs');
    if (!r.items.length) { el.innerHTML = '<p class="text-sm text-muted">' + t('rag.noDocs') + '</p>'; return; }
    el.innerHTML = r.items.map(d => `
      <div class="list-item" style="padding:8px 0">
        <div class="list-item-header">
          <span class="list-item-title">${escapeHtml(d.source_path)}</span>
          <span class="tag tag-gray">${t('rag.chunkCount').replace('{n}', d.chunk_count)}</span>
        </div>
        <div class="flex gap-8 mt-8">
          <button class="btn btn-secondary btn-sm" onclick="openRagSource('${encodeURIComponent(d.source_path)}')">${t('rag.open')}</button>
          <button class="btn btn-danger btn-sm" onclick="deleteRagDoc(${d.id})">${t('rag.remove')}</button>
        </div>
      </div>`).join('');
  } catch(e) { el.innerHTML = `<p class="text-sm text-muted">${escapeHtml(e.message)}</p>`; }
}

async function deleteRagDoc(id) {
  const ok = await confirmDialog(t('rag.removeConfirm'));
  if (!ok) return;
  try {
    await api(`/api/rag/doc/${id}`, { method: 'DELETE' });
    toast(t('rag.removed'));
    loadRagDocs();
  } catch(e) { toast(e.message, 'error'); }
}

// ── 试卷 OCR（B2）──
let _ocrResultText = '';
async function loadOcrProbe() {
  const el = document.getElementById('ocrProbe');
  if (!el) return;
  try {
    const r = await api('/api/ocr/probe');
    const parts = [
      r.paddleocr ? 'paddleocr ✓' : 'paddleocr ✗',
      r.pdfminer ? 'pdfminer ✓' : 'pdfminer ✗',
      r.renderer ? 'pypdfium2 ✓' : 'pypdfium2 ✗',
    ];
    el.textContent = t('ocr.capability').replace('{s}', parts.join(' · ')) +
      (r.paddleocr ? t('ocr.scanYes') : t('ocr.scanNo'));
  } catch(e) { el.textContent = t('ocr.probeFail').replace('{m}', e.message); }
}

function collectOcrTexts() {
  return Array.from(document.querySelectorAll('.ocr-text'))
    .map((t, i) => t('ocr.pageLabel').replace('{n}', i + 1) + '\n' + t.value).join('\n\n');
}

async function runOcr() {
  const path = document.getElementById('ocrPath').value.trim();
  if (!path) { toast(t('rag.needPath'), 'error'); return; }
  const el = document.getElementById('ocrResultList');
  el.innerHTML = '<p class="text-sm text-muted">' + t('ocr.extracting') + '</p>';
  _ocrResultText = '';
  try {
    const r = await api('/api/ocr/extract', { method: 'POST', body: { path } });
    const pages = r.pages || [];
    _ocrResultText = collectOcrTexts();
    el.innerHTML = `
      <p class="text-sm text-muted mb-8">${t('ocr.engineInfo').replace('{e}', escapeHtml(r.engine)).replace('{n}', pages.length).replace('{c}', pages[0] ? pages[0].confidence : '-')}</p>
      ${pages.map(p => `
        <div class="ocr-page mb-8">
          <div class="text-sm" style="font-weight:600;margin-bottom:4px">${t('ocr.itemTitle').replace('{n}', p.page)} <span class="text-muted">${t('ocr.confidence').replace('{c}', p.confidence)}</span></div>
          <textarea class="form-input ocr-text" style="width:100%;min-height:120px;font-family:monospace" oninput="_ocrResultText = collectOcrTexts()">${escapeHtml(p.text)}</textarea>
        </div>`).join('')}`;
    _ocrResultText = collectOcrTexts();
    toast(t('ocr.done'));
  } catch(e) {
    el.innerHTML = `<p class="text-sm tag tag-red" style="white-space:pre-line">${escapeHtml(e.message)}</p>`;
  }
}

async function copyOcrResult() {
  if (!_ocrResultText) { toast(t('ocr.needRun'), 'error'); return; }
  try {
    await navigator.clipboard.writeText(_ocrResultText);
    toast(t('ocr.copied'));
  } catch(e) {
    const ta = document.createElement('textarea');
    ta.value = _ocrResultText; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
    toast(t('ocr.copied'));
  }
}

async function searchRag() {
  const q = document.getElementById('ragQuery').value.trim();
  loadRagSearch(q);
}

async function loadRagSearch(q) {
  const el = document.getElementById('ragResultList');
  if (!el) return;
  if (!q) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="loading">' + t('rag.searching') + '</div>';
  try {
    const r = await api(`/api/rag/search?q=${encodeURIComponent(q)}&k=5`);
    if (!r.items.length) { el.innerHTML = '<p class="text-sm text-muted">' + t('rag.noMatch') + '</p>'; return; }
    el.innerHTML = r.items.map(hit => `
      <div class="hint-card" style="margin-bottom:8px">
        <div class="flex-between">
          <span class="text-sm" style="font-weight:600">${escapeHtml(hit.name)}${hit.page ? t('rag.pageSuffix').replace('{p}', hit.page) : ''}</span>
          <span class="tag tag-gray">${hit.score}</span>
        </div>
        <p class="text-sm" style="margin:6px 0">${escapeHtml(hit.content)}</p>
        <button class="btn btn-link btn-sm" onclick="openRagSource('${encodeURIComponent(hit.source_path)}')">${t('rag.openSrc')}</button>
      </div>`).join('');
  } catch(e) { el.innerHTML = `<p class="text-sm text-muted">${escapeHtml(e.message)}</p>`; }
}

function examBar(pct, width) {
  const w = width || '100%';
  const color = pct >= 80 ? '#22c55e' : pct >= 60 ? '#f59e0b' : '#ef4444';
  return `<div style="height:8px;background:var(--border);border-radius:4px;overflow:hidden;width:${w}">
    <div style="height:100%;width:${Math.min(100, Math.max(0, pct))}%;background:${color};border-radius:4px"></div></div>`;
}

// ── B4 考试就绪度 ──
async function createExamPaper() {
  const name = document.getElementById('examName').value.trim();
  if (!name) { toast(t('exam.needName'), 'error'); return; }
  try {
    const r = await api('/api/exam/papers', { method: 'POST', body: {
      name,
      exam_date: document.getElementById('examDate').value.trim(),
      target: parseInt(document.getElementById('examTarget').value, 10) || 80,
    }});
    toast(t('exam.created'));
    document.getElementById('examName').value = '';
    loadExam();
  } catch(e) { toast(e.message, 'error'); }
}

async function loadExam() {
  const el = document.getElementById('examPaperList');
  if (!el) return;
  try {
    const r = await api('/api/exam/papers');
    const ov = document.getElementById('examOverview');
    if (r.overall === null) {
      ov.innerHTML = '<p class="text-sm text-muted mt-8">' + t('exam.noneYet') + '</p>';
      el.innerHTML = '';
      return;
    }
    ov.innerHTML = `<div class="flex-between mb-8">
        <span class="text-sm">${t('exam.globalReady')}</span>
        <span class="flex gap-8 items-center">
          <span class="tag ${r.overall >= 80 ? 'tag-green' : r.overall >= 60 ? 'tag-amber' : 'tag-red'}">${r.overall}%</span>
          ${examBar(r.overall, 160)}
        </span>
      </div>`;
    el.innerHTML = r.papers.map(p => {
      const ready = p.readiness;
      return `<div class="card" style="margin-bottom:12px">
        <div class="flex-between">
          <span class="list-item-title">${escapeHtml(p.paper.name)}</span>
          <span class="flex gap-8 items-center">
            <span class="text-sm text-muted">${t('exam.targetLabel').replace('{n}', p.paper.target)}</span>
            <span class="tag ${ready >= p.paper.target ? 'tag-green' : ready >= p.paper.target * 0.75 ? 'tag-amber' : 'tag-red'}">${t('exam.readyLabel').replace('{n}', ready)}</span>
          </span>
        </div>
        <div class="list-item-meta">${t('exam.meta').replace('{d}', escapeHtml(p.paper.exam_date || t('exam.dateNone'))).replace('{n}', p.question_count).replace('{h}', p.hit_rate)}${p.gap_to_target > 0 ? t('exam.gap').replace('{g}', p.gap_to_target) : ''}</div>
        ${examBar(ready)}
        ${p.gaps.length ? `<p class="text-sm mt-8"><b style="color:var(--warning)">${t('exam.weakTopics')}</b>${p.gaps.map(g => `<span class="tag tag-warn" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</p>` : '<p class="text-sm text-muted mt-8">' + t('exam.allGood') + '</p>'}
        <div class="flex gap-8 mt-12">
          <button class="btn btn-secondary btn-sm" onclick="loadExamDetail(${p.paper.id})">${t('exam.viewAdd')}</button>
          <button class="btn btn-danger btn-sm" onclick="deleteExamPaper(${p.paper.id})">${t('exam.delete')}</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<p class="text-sm text-muted">${escapeHtml(e.message)}</p>`; }
}

async function loadExamDetail(id) {
  try {
    const p = await api(`/api/exam/papers/${id}`);
    let rowsHtml = (p.questions || []).map((q, i) => `
      <tr>
        <td>${escapeHtml(q.qno || i + 1)}</td>
        <td>${escapeHtml(q.topic)}</td>
        <td>${q.weight}</td>
        <td><span class="tag ${(p.readiness >= p.target || true) ? 'tag-gray' : ''}">—</span></td>
      </tr>`).join('');
    const html = `<div class="modal" role="dialog" style="max-width:640px">
      <div class="modal-header">
        <h3>${t('exam.detailTitle').replace('{n}', escapeHtml(p.paper.name)).replace('{r}', p.readiness).replace('{t}', p.paper.target)}</h3>
        <button class="modal-close" onclick="this.closest('.modal-overlay').classList.remove('active')">&times;</button>
      </div>
      <p class="text-sm text-muted">${t('exam.inputHint')}</p>
      <textarea id="examQInput" rows="6" class="form-input" data-i18n-ph="exam.inputPh"></textarea>
      <div class="flex gap-12 mt-12">
        <button class="btn btn-primary btn-sm" onclick="saveExamQuestions(${id})">${t('exam.saveQ')}</button>
        <button class="btn btn-secondary btn-sm" onclick="this.closest('.modal-overlay').classList.remove('active')">${t('common.close')}</button>
      </div>
      ${p.questions && p.questions.length ? `<table class="table" style="margin-top:12px"><thead><tr><th>${t('exam.colNo')}</th><th>${t('exam.colTopic')}</th><th>${t('exam.colWeight')}</th></tr></thead><tbody>${rowsHtml}</tbody></table>` : ''}
    </div>`;
    const ov = document.createElement('div');
    ov.className = 'modal-overlay active';
    ov.innerHTML = `<div style="position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;padding:16px">${html}</div>`;
    ov.addEventListener('click', e => { if (e.target === ov.firstElementChild) ov.remove(); });
    document.body.appendChild(ov);
  } catch(e) { toast(e.message, 'error'); }
}

async function saveExamQuestions(paperId) {
  const text = document.getElementById('examQInput').value.trim();
  if (!text) { toast(t('exam.needText'), 'error'); return; }
  const questions = [];
  for (const line of text.split('\n')) {
    const parts = line.split('|').map(s => s.trim());
    if (!parts[1]) continue;
    questions.push({ qno: parts[0], topic: parts[1], weight: parseFloat(parts[2]) || 1 });
  }
  if (!questions.length) { toast(t('exam.invalidLine'), 'error'); return; }
  try {
    await api(`/api/exam/papers/${paperId}/questions`, { method: 'POST', body: { questions } });
    toast(t('exam.added').replace('{n}', questions.length));
    loadExam();
    const ov = document.querySelector('.modal-overlay');
    if (ov) ov.remove();
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteExamPaper(id) {
  const ok = await confirmDialog(t('exam.deleteConfirm'));
  if (!ok) return;
  try {
    await api(`/api/exam/papers/${id}`, { method: 'DELETE' });
    toast(t('msg.deleted'));
    loadExam();
  } catch(e) { toast(e.message, 'error'); }
}

// ── 设置页 ──
async function loadSettings() {
  probeLocalModels();
  try {
    const s = await api('/api/settings');
    document.getElementById('setApiBase').value = s.api_base || '';
    document.getElementById('setApiKey').value = '';
    document.getElementById('setApiKey').placeholder = s.has_api_key ? t('set.keyPh') : 'sk-...';
    document.getElementById('setModel').value = s.model || '';
    document.getElementById('setFastModel').value = s.fast_model || '';
    document.getElementById('setHeavyModel').value = s.heavy_model || '';
    document.getElementById('setVisionModel').value = s.vision_model || '';
    document.getElementById('setMasterPassword').value = '';
    document.getElementById('setMasterPassword').placeholder = s.key_source === 'keyfile' ? t('set.masterPhKeyfile') : t('set.masterPh');
    document.getElementById('setTemp').value = s.temperature || '0.3';
    const srcLabel = {
      environment: t('set.srcEnv'),
      keyfile: t('set.srcKeyfile'),
      runtime: t('set.srcRuntime'),
      none: t('set.srcNone'),
    };
    const ksEl = document.getElementById('keyStatus');
    ksEl.textContent = s.key_file_locked
      ? t('set.keyFileLocked') + '（' + (srcLabel[s.key_source] || srcLabel.none) + '）'
      : (srcLabel[s.key_source] || srcLabel.none);
    loadPrefs();
  } catch(e) { toast(e.message, 'error'); }
}

async function unlockKeystore() {
  const pwd = document.getElementById('setMasterPassword').value;
  if (!pwd) { toast(t('set.unlockFail'), 'error'); return; }
  try {
    const r = await api('/api/keystore/unlock', { method: 'POST', body: { master_password: pwd } });
    if (!r.ok) { toast(t('set.unlockFail'), 'error'); return; }
    toast(t('set.unlockOk'), 'success');
    document.getElementById('setMasterPassword').value = '';
    loadSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function clearKeystore() {
  const ok = await confirmDialog(t('set.clearConfirm'));
  if (!ok) return;
  try {
    await api('/api/keystore/clear', { method: 'POST', body: {} });
    toast(t('set.clearOk'), 'success');
    loadSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function showPeriodicReport() {
  try {
    const [w, m] = await Promise.all([
      api('/api/report/weekly'),
      api('/api/report/monthly'),
    ]);
    const html = `
      <div class="flex gap-8 mb-8">
        <button class="btn btn-primary btn-sm" onclick="periodicTab('week')">${t('report.weekTab')}</button>
        <button class="btn btn-secondary btn-sm" onclick="periodicTab('month')">${t('report.monthTab')}</button>
      </div>
      <div id="periodicWeek">${periodicWeekHtml(w)}</div>
      <div id="periodicMonth" class="hidden">${periodicMonthHtml(m)}</div>`;
    const mb = document.getElementById('modalBody');
    mb.innerHTML = html;
    document.getElementById('modalTitle').textContent = t('card.weeklyMore');
    renderMath(mb);
    openModal('problemModal');
  } catch(e) { toast(e.message || t('report.loadFail'), 'error'); }
}

function periodicTab(tab) {
  const w = document.getElementById('periodicWeek');
  const m = document.getElementById('periodicMonth');
  if (!w || !m) return;
  w.classList.toggle('hidden', tab !== 'week');
  m.classList.toggle('hidden', tab !== 'month');
}

function periodicBarHtml(rows, max) {
  const m = max || Math.max(1, ...rows.map(r => r.count));
  return rows.map(r => `
    <div class="error-bar-row">
      <span class="text-sm" style="min-width:130px">${escapeHtml(r.label || r.date)}</span>
      <div class="error-bar-track"><div class="error-bar-fill" style="width:${Math.round(r.count / m * 100)}%;background:var(--accent)"></div></div>
      <b class="text-sm" style="min-width:32px">${r.count}</b>
    </div>`).join('') || '<p class="text-sm text-muted">' + t('msg.noData') + '</p>';
}

function periodicWeekHtml(w) {
  if (!w || w.week_start === undefined) return '<p class="text-sm text-muted">' + t('msg.noData') + '</p>';
  const delta = w.review_delta || 0;
  const deltaStr = delta > 0 ? '+' + delta : String(delta);
  return `
    <p class="text-sm text-muted mb-8">${t('report.weekRange').replace('{s}', escapeHtml(w.week_start))}</p>
    <div class="error-bar-row"><span class="text-sm">${t('report.newProblems')}</span><b>${w.new_problems}</b><span class="text-sm text-muted">${t('report.vsLastWeek').replace('{n}', w.prev_problems)}</span></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.reviews')}</span><b>${w.week_reviews}</b><span class="text-sm text-muted">${t('report.delta').replace('{d}', deltaStr)}</span></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.goodRate')}</span><b>${(w.good_rate * 100).toFixed(0)}%</b></div>
    <p class="hint-text mt-12">${t('report.tip').replace('{t}', t(w.tip_key || 'report.tipWeekNone'))}</p>`;
}

function periodicMonthHtml(m) {
  if (!m || m.start === undefined) return '<p class="text-sm text-muted">' + t('msg.noData') + '</p>';
  const daily = (m.daily || []).map(d => ({ label: d.date, count: d.count }));
  const errs = (m.top_errors || []).map(e => ({ label: e.label, count: e.count }));
  return `
    <p class="text-sm text-muted mb-8">${t('report.monthRange').replace('{s}', escapeHtml(m.start)).replace('{e}', escapeHtml(m.end))}</p>
    <div class="error-bar-row"><span class="text-sm">${t('report.newProblems')}</span><b>${m.month_new}</b><span class="text-sm text-muted">${t('report.vsLastMonth').replace('{n}', m.prev_new)}</span></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.reviews')}</span><b>${m.month_revs}</b><span class="text-sm text-muted">${t('report.vsLastMonth').replace('{n}', m.prev_revs)}</span></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.goodRate')}</span><b>${(m.good_rate * 100).toFixed(0)}%</b></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.activeDays')}</span><b>${m.active_days}</b></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.mastered')}</span><b>${m.mastered}</b><span class="text-sm text-muted">/ ${m.total_problems}</span></div>
    <div class="text-sm text-muted mt-12 mb-8">${t('report.dailyTitle')}</div>
    ${periodicBarHtml(daily.slice(-14))}
    <div class="text-sm text-muted mt-12 mb-8">${t('report.topErrors')}</div>
    ${periodicBarHtml(errs)}
    <p class="hint-text mt-12">${t('report.tip').replace('{t}', t(m.tip_key || 'report.tipMonthNone'))}</p>`;
}

async function loadPrefs() {
  const notifyEl = document.getElementById('prefNotify');
  if (notifyEl) notifyEl.checked = notificationsEnabled();
  try {
    const p = await api('/api/profile');
    const prefs = p.preferences || {};
    const goal = p.goal || {};
    const el = (id, v) => { const e = document.getElementById(id); if (e) e.value = v || (id === 'prefDailyTarget' ? '0' : ''); };
    el('prefDepth', prefs.explain_depth || '2');
    el('prefExamples', prefs.example_count || '1');
    el('prefDailyTarget', goal.daily_review_target === undefined ? '0' : goal.daily_review_target);
  } catch(e) { /* 偏好可选 */ }
}

async function savePrefs() {
  const notifyEl = document.getElementById('prefNotify');
  if (notifyEl) localStorage.setItem('notifyEnabled', notifyEl.checked ? '1' : '0');
  try {
    await api('/api/profile', {
      method: 'PUT',
      body: {
        explain_depth: document.getElementById('prefDepth').value,
        example_count: document.getElementById('prefExamples').value,
        daily_review_target: document.getElementById('prefDailyTarget').value,
      },
    });
    toast(t('msg.prefsSaved'));
    loadPrefs();
  } catch(e) { toast(e.message, 'error'); }
}

// ── P0 FSRS 参数个性化 ──
async function loadFsrsStatus() {
  const el = document.getElementById('fsrsStatus');
  if (!el) return;
  try {
    const s = await api('/api/fsrs/status');
    if (!s.available) {
      el.textContent = t('fsrs.disabled');
      el.style.color = 'var(--warning)';
      return;
    }
    const src = s.params_source === 'trained'
      ? t('fsrs.trained').replace('{d}', escapeHtml(s.trained_at)).replace('{n}', s.sample_count)
      : t('fsrs.default');
    let extra = '';
    if (s.training) extra = ' <span class="tag tag-amber">' + t('fsrs.training') + '</span>';
    else if (s.last_train) extra = ` <span class="tag tag-green">${t('fsrs.trainOk')}</span>`;
    else if (s.train_error) extra = ` <span class="tag tag-red">${t('fsrs.lastFail')}</span>`;
    el.innerHTML = t('fsrs.enabled').replace('{s}', src).replace('{r}', s.desired_retention) + extra;
    const ret = document.getElementById('fsrsRetention');
    if (ret) { ret.value = s.desired_retention; document.getElementById('fsrsRetentionVal').textContent = s.desired_retention; }
    if (s.training) { setTimeout(loadFsrsStatus, 3000); }
  } catch(e) { el.textContent = t('fsrs.loadFail'); }
}

async function saveFsrsRetention() {
  try {
    const r = await api('/api/fsrs/retention', {
      method: 'POST',
      body: { value: parseFloat(document.getElementById('fsrsRetention').value) },
    });
    toast(r.ok ? t('fsrs.retentionSaved') : t('fsrs.retentionRange'), r.ok ? '' : 'error');
    if (r.ok) loadFsrsStatus();
  } catch(e) { toast(e.message, 'error'); }
}

async function trainFsrs() {
  const btn = document.getElementById('trainFsrsBtn');
  btn.disabled = true;
  try {
    const r = await api('/api/fsrs/train', { method: 'POST', body: {} });
    if (r.started) {
      toast(t('fsrs.trainingStart').replace('{n}', r.sample_count));
      setTimeout(loadFsrsStatus, 2000);
    } else {
      toast(t('fsrs.trainFail').replace('{m}', r.error || t('fsrs.unknown')), 'error');
    }
  } catch(e) { toast(e.message, 'error'); }
  btn.disabled = false;
}

async function resetFsrs() {
  try {
    const r = await api('/api/fsrs/reset', { method: 'POST', body: {} });
    toast(r.ok ? t('fsrs.resetOk') : t('fsrs.resetFail'), r.ok ? '' : 'error');
    if (r.ok) loadFsrsStatus();
  } catch(e) { toast(e.message, 'error'); }
}

async function saveSettings() {
  const body = {
    api_base: document.getElementById('setApiBase').value,
    model: document.getElementById('setModel').value,
    temperature: document.getElementById('setTemp').value,
    fast_model: document.getElementById('setFastModel').value,
    heavy_model: document.getElementById('setHeavyModel').value,
    vision_model: document.getElementById('setVisionModel').value,
  };
  const key = document.getElementById('setApiKey').value;
  if (key) body.api_key = key;
  const master = document.getElementById('setMasterPassword').value;
  if (master) body.master_password = master;
  try {
    await api('/api/settings', { method: 'PUT', body });
    toast(t('set.saved'));
    loadSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function testSettings() {
  try {
    const r = await api('/api/settings/test', { method: 'POST', body: {} });
    if (r.ok) toast(t('set.connOk').replace('{r}', r.reply));
  } catch(e) { toast(t('set.connFail').replace('{m}', e.message), 'error'); }
}

// ── 数据导入 / 导出 ──
function _downloadFromApi(path, filename) {
  return fetch(path, { headers: { 'X-Requested-With': 'PhysicsStudyOS' } })
    .then(r => { if (!r.ok) throw new Error(t('export.fail').replace('{s}', r.status)); return r.blob(); })
    .then(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = filename; a.click();
      URL.revokeObjectURL(url);
    });
}

async function exportAnki() {
  try {
    await _downloadFromApi('/api/export?format=anki-csv', `physics_study_anki_${new Date().toISOString().slice(0, 10)}.csv`);
    toast(t('export.anki'));
  } catch(e) { toast(e.message, 'error'); }
}

async function exportIcs() {
  try {
    await _downloadFromApi('/api/export?format=ics', `physics_study_review_${new Date().toISOString().slice(0, 10)}.ics`);
    toast(t('export.ics'));
  } catch(e) { toast(e.message, 'error'); }
}

async function exportData() {
  try {
    const data = await api('/api/export');
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `physics_study_${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast(t('export.data'));
  } catch(e) { toast(e.message, 'error'); }
}

// ── 一键备份 / 还原（全库 JSON）──
async function exportBackup() {
  try {
    await _downloadFromApi('/api/export/backup', `physics-study-backup-${new Date().toISOString().slice(0, 10)}.json`);
    toast(t('export.backup'));
  } catch(e) { toast(e.message, 'error'); }
}

async function importBackup(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const ok = await confirmDialog(t('restore.confirm'));
    if (!ok) { input.value = ''; return; }
    const r = await api('/api/import/restore', { method: 'POST', body: { backup: text } });
    const n = Object.values(r.restored || {}).reduce((s, x) => s + x, 0);
    toast(t('restore.done').replace('{n}', n));
    loadDashboard();
  } catch(e) { toast(t('restore.fail').replace('{m}', e.message), 'error'); }
  input.value = '';
}

async function importData(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    const ok = await confirmDialog(t('import.confirm'));
    if (!ok) { input.value = ''; return; }
    const r = await api('/api/import', { method: 'POST', body: data });
    toast(t('import.done').replace('{n}', r.imported).replace('{b}', r.backup.split(/[\\/]/).pop()));
    loadDashboard();
  } catch(e) { toast(t('import.fail').replace('{m}', e.message), 'error'); }
  input.value = '';
}

// ── 公式速查 ──
const _FORMULAS = [
  {key:'formula.kinematics', eqs:['v = v₀ + at','s = v₀t + ½at²','v² − v₀² = 2as','ω = dθ/dt']},
  {key:'formula.dynamics', eqs:['F = ma','F_f ≤ μN','F = −kx (Hooke\x27s law)','p = mv']},
  {key:'formula.workEnergy', eqs:['W = F·s·cosθ','K = ½mv²','W = ΔK','U_g = mgh','U_e = ½kx²']},
  {key:'formula.momentum', eqs:['p_i = p_f','J = Δp = FΔt','perfectly elastic: v₁'+"'"+' = (m₁−m₂)/(m₁+m₂)·v₁']},
  {key:'formula.circular', eqs:['a_c = v²/r = ω²r','F_c = mv²/r','v = ωr','T = 2π/ω']},
  {key:'formula.electro', eqs:['F = kQq/r²','E = F/q','E = kQ/r²','U = Ed (uniform field)']},
  {key:'formula.circuit', eqs:['V = IR','P = IV = I²R','R_s = R₁+R₂+...','1/R_p = 1/R₁+1/R₂+...']},
  {key:'formula.magnet', eqs:['F = qvB·sinθ','F = ILB·sinθ','Φ = BA·cosθ','ε = −dΦ/dt']},
  {key:'formula.thermo', eqs:['PV = nRT','ΔU = Q − W','η = 1 − T_c/T_h','ΔS = Q_rev/T']},
  {key:'formula.waves', eqs:['v = fλ','n = c/v','n₁sinθ₁ = n₂sinθ₂','dsinθ = mλ (double-slit)']},
  {key:'formula.si', eqs:['n 10⁻⁹','μ 10⁻⁶','m 10⁻³','c 10⁻²','k 10³','M 10⁶','G 10⁹']},
];
function toggleFormulaPanel() {
  const p = document.getElementById('formulaPanel');
  const content = document.getElementById('formulaContent');
  if (p.classList.contains('hidden')) {
    content.innerHTML = _FORMULAS.map(c =>
      `<div style="margin-bottom:8px"><strong>${t('formula.' + c.key)}</strong>: ${c.eqs.map(e=>escapeHtml(e)).join(' &nbsp;| ')}</div>`
    ).join('');
    p.classList.remove('hidden');
    renderMath(content);
  } else {
    p.classList.add('hidden');
  }
}

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
