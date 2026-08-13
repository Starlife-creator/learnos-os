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
  toast(_lang === 'en-US' ? 'Language switched to English' : '语言已切换');
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
    if (!res.ok) throw new Error(data.error || '请求失败');
    return data;
  } catch (err) {
    // 仅对网络层失败（非 HTTP 错误）重试一次（GET）
    if (fetchFailed && method === 'GET') {
      fetchFailed = false;
      const res = await doFetch();
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || '请求失败');
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
  const labels = {1:'完全不会',2:'有思路',3:'基本会做',4:'熟练',5:'精通'};
  return `<span class="tag ${cls}">${labels[level]||'未知'}</span>`;
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
  wrap.innerHTML = currentTags.map((t, i) =>
    `<span class="chip${t.pending ? ' pending' : ''}" title="${t.pending ? 'AI 建议（置信度不足 0.9），保存确认后生效' : ''}">${escapeHtml(t.text)}<span class="chip-x" onclick="removeTag(${i})" aria-label="移除标签">&times;</span></span>`
  ).join('');
  const hint = document.getElementById('editTagsHint');
  const pendingCount = currentTags.filter(t => t.pending).length;
  if (pendingCount) hint.textContent = `AI 建议已加入（${pendingCount} 项置信度低于 0.9），保存即确认采纳；不想要可直接删除。`;
  else if (currentTags.length) hint.textContent = '保存后标签生效。';
  else hint.textContent = '可手动输入，或用 AI 从题目自动提取（无 AI 配置时自动使用关键词规则）。';
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
    const source = data.source === 'ai' ? 'AI' : '关键词规则';
    const conf = Math.round((data.confidence || 0) * 100);
    document.getElementById('editTagsHint').textContent =
      `${source} 提取，置信度 ${conf}%${data.source !== 'ai' ? '（未配置 AI，已自动降级）' : ''}。保存即确认采纳。`;
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
  if (idx >= 1 && idx <= PAGES.length) switchPage(PAGES[idx - 1]);
});

// ── 主题切换 ──
function applyTheme(theme) {
  const root = document.documentElement;
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (theme === 'dark') { root.style.colorScheme = 'dark'; if (meta) meta.content = 'dark'; }
  else if (theme === 'light') { root.style.colorScheme = 'light'; if (meta) meta.content = 'light'; }
  else { root.style.colorScheme = ''; if (meta) meta.content = 'light dark'; }
}
document.getElementById('themeToggle').addEventListener('click', () => {
  const cur = document.documentElement.style.colorScheme;
  const next = cur === 'dark' ? 'light' : 'dark';
  localStorage.setItem('theme', next);
  applyTheme(next);
});
(function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved) applyTheme(saved);
})();

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
            <span class="tag tag-gray">${t.count}题</span>
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
      recentEl.innerHTML = '<div class="empty"><p>还没有记录，去"错题"页添加第一题吧</p></div>';
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
            <span class="tag tag-gray">${c.avg_mastery} (${c.count}题${c.due>0?',待复习'+c.due:''})</span>
          </span>
        </div>`).join('');
    } else {
      courseEl.innerHTML = '<div class="empty"><p>暂无课程分类</p></div>';
    }

    // 最近复习活动
    const actEl = document.getElementById('recentActivity');
    if (d.recent_activity && d.recent_activity.length) {
      const labels = {1:'忘记',2:'模糊',3:'正确',4:'掌握'};
      actEl.innerHTML = d.recent_activity.map(a => `
        <div class="list-item" onclick="viewProblem(${a.problem_id})" style="padding:8px 12px">
          <span class="text-sm">📝 ${escapeHtml(a.title)}</span>
          <span class="tag ${a.result==='4'?'tag-green':a.result==='3'?'tag-blue':'tag-amber'}">${labels[a.result]||'?'}</span>
          <span class="text-muted text-sm" style="float:right">${(a.created_at||'').slice(0,16)}</span>
        </div>`).join('');
    } else { actEl.innerHTML = '<div class="empty"><p>暂无复习活动</p></div>'; }
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
    new Notification('物理学习 OS', { body: `现在有 ${due} 道题目待复习`, tag: 'due-review' });
    localStorage.setItem('notifyLastAt', String(now));
  } catch(e) { /* 通知失败不影响 */ }
}

async function requestNotifyPermission() {
  if (!('Notification' in window)) { toast('当前浏览器不支持通知', 'error'); return; }
  if (Notification.permission === 'granted') { toast('通知已授权'); return; }
  const res = await Notification.requestPermission();
  toast(res === 'granted' ? '已开启复习提醒' : '未授权（可在浏览器设置中开启）', res === 'granted' ? 'success' : 'error');
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
  if (!log.length) { svg.innerHTML = ''; hint.textContent = '完成复习后会记录掌握度变化'; return; }
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
  const acc = summary.week_accuracy ? ` · 近7天正确率 ${summary.week_accuracy}%` : '';
  hint.textContent = `最近 ${n} 次${acc} · 当前均值 ${last.avg_mastery}`;
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
      <span class="text-sm" style="font-size:10px;color:var(--text-2)">${isToday ? '今天' : s.date.slice(5)}</span>
    </div>`;
  }).join('');
  const total7 = series.reduce((n, s) => n + s.due, 0);
  hint.textContent = total7 ? `未来 7 天共 ${total7} 道复习任务` : '未来 7 天没有复习任务';
  const dh = document.getElementById('deckHealth');
  const h = (data && data.deck_health) || {};
  if (dh) dh.innerHTML = h.total ?
    `新生 ${h.newborn} · 学习中 ${h.learning} · 成长中 ${h.mature}（共 ${h.total} 题，平均复习 ${h.avg_repetition} 次，平均掌握度 ${h.avg_mastery}）` :
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
      ? `已统计 ${total} 张 FSRS 卡 · 平均稳定度 ${f.avg_stability || 0} 天 · <span class="text-muted">绿点=实测遗忘率（越低越牢），蓝线=预测曲线，黄虚线=目标保持率</span>`
      : '<span class="text-muted">暂无 FSRS 卡数据（完成几次复习后自动出现）</span>';
  }
}

// ── D6 游戏化 ──
function drawGamification(g) {
  const el = document.getElementById('gameCard');
  if (!el) return;
  if (!g || g.total_reviews === undefined) { el.innerHTML = '<div class="text-muted text-sm">' + t('msg.noData') + '</div>'; return; }
  const unlocked = (g.badges || []).filter(b => b.unlocked);
  el.innerHTML = `<div class="flex-between mb-8">
    <div><span class="text-sm">累计 XP</span><div class="text-xl">${g.total_xp}</div></div>
    <div><span class="text-sm">今日 XP</span><div class="text-xl">${g.today_xp}</div></div>
    <div><span class="text-sm">连续天数</span><div class="text-xl">🔥 ${g.streak}</div></div>
    <div><span class="text-sm">累计复习</span><div class="text-xl">${g.total_reviews}</div></div>
  </div>
  <div class="flex wrap gap-8">${(g.badges || []).map(b =>
    `<span class="tag ${b.unlocked ? 'tag-green' : 'tag-gray'}" title="${escapeHtml(b.label)}">${b.unlocked ? '🏅' : '🔒'} ${escapeHtml(b.id.replace('_',' '))}</span>`
  ).join('') || '<span class="text-muted text-sm">完成复习解锁第一个徽章</span>'}</div>`;
}

// ── C6 AI 遥测 ──
function drawTelemetry(t) {
  const el = document.getElementById('telemetryCard');
  if (!el) return;
  if (!t || t.calls === undefined) { el.innerHTML = '<div class="text-muted text-sm">' + t('msg.noData') + '</div>'; return; }
  const rate = t.fail_rate > 0.3 ? 'tag-red' : t.fail_rate > 0.1 ? 'tag-warn' : 'tag-green';
  el.innerHTML = `<div class="flex-between mb-8">
    <span class="text-sm">近 7 天调用</span><b>${t.calls}</b>
    <span class="text-sm">失败率</span><span class="tag ${rate}">${(t.fail_rate * 100).toFixed(0)}%</span>
    <span class="text-sm">平均延迟</span><b>${t.avg_latency_ms}ms</b>
    <span class="text-sm">估算 Token</span><b>${t.tokens}</b>
  </div>
  ${t.slow_routes && t.slow_routes.length ? `<p class="hint-text">最慢路由：${t.slow_routes.map(escapeHtml).join('、')}</p>` : ''}`;
}

// ── D5 周报 ──
function drawWeekly(w) {
  const el = document.getElementById('weeklyCard');
  if (!el) return;
  if (!w || w.week_start === undefined) { el.innerHTML = '<div class="text-muted text-sm">' + t('msg.noData') + '</div>'; return; }
  const delta = (w.review_delta || 0);
  const deltaStr = delta > 0 ? `+${delta}` : String(delta);
  el.innerHTML = `<div class="flex-between mb-8">
    <span class="text-sm">本周（${escapeHtml(w.week_start)} 起）</span>
  </div>
  <div class="flex-between mb-8">
    <span class="text-sm">新增错题</span><b>${w.new_problems}（上周 ${w.prev_problems}）</b>
    <span class="text-sm">复习次数</span><b>${w.week_reviews}（${deltaStr}）</b>
    <span class="text-sm">保持率</span><b>${(w.good_rate * 100).toFixed(0)}%</b>
  </div>
  <p class="hint-text">💡 ${escapeHtml(w.tip || '')}</p>`;
}

// ── P0 顽固错题 ──
function drawStubborn(list) {
  const el = document.getElementById('stubbornList');
  if (!el) return;
  if (!list || !list.length) { el.innerHTML = '暂无反复出错的题目，保持势头 🎉'; return; }
  el.innerHTML = list.map(p => {
    const rate = p.total_reviews ? Math.round(p.miss_count / p.total_reviews * 100) : 0;
    return `<div class="flex-between mb-8">
      <a href="#" onclick="event.preventDefault();viewProblem(${p.id});return false;">${escapeHtml(p.title)}</a>
      <span class="text-muted">错 ${p.miss_count} 次 · 再错率 ${rate}% · 掌握度 ${p.mastery}/5</span>
    </div>`;
  }).join('') + '<p class="hint-text mt-8">再错率 = 评分 ≤2 的复习占比；建议对这些题做「错因专项」+ 变式练习。</p>';
}

// ── P0 复习压力指数（PI）──
function drawPressure(p) {
  const el = document.getElementById('pressureCard');
  if (!el) return;
  const color = p.level === '高' ? 'var(--danger,#ef4444)' : p.level === '中' ? 'var(--warning)' : 'var(--success)';
  el.innerHTML = p.total == null ? '—' :
    `<div class="flex-between"><span class="text-sm">压力分 <b style="color:${color}">${p.score}</b>（${p.level}）</span>
     <span class="text-sm text-muted">逾期 ${p.overdue} · 今日 ${p.today} · 明日 ${p.tomorrow} · 合计约 ${p.est_minutes} 分钟</span></div>
     <div class="error-bar-track mt-8"><div class="error-bar-fill" style="width:${Math.min(100, p.score)}%;background:${color}"></div></div>
     <p class="hint-text mt-8">${p.overdue > 0 ? `⚠ 有 ${p.overdue} 题逾期：先清逾期（系统已自动缩短逾期间隔），再按优先级复习。` : '当前无逾期，保持节奏即可。'}</p>`;
}

// ── P0 遗忘预测（FSRS R 值）──
function drawForgetPredict(f) {
  const el = document.getElementById('forgetCard');
  if (!el) return;
  if (!f.count) { el.innerHTML = '近期待复习的题目不多，暂无遗忘风险'; return; }
  const pct = (r) => (r * 100).toFixed(0) + '%';
  el.innerHTML =
    `<div class="text-sm">近期 ${f.count} 题待复习 · 平均检索概率 ${pct(f.avg_r)} · 高危(R&lt;50%) ${f.high_risk} · 中危(50-70%) ${f.medium_risk}</div>
     ${f.top && f.top.length ? `<div class="mt-8 text-sm">最易遗忘：${f.top.map(t => `<a href="#" onclick="event.preventDefault();viewProblem(${t.problem_id});return false;">${escapeHtml(t.title)}（R=${pct(t.r)}）</a>`).join('、')}</div>` : ''}
     <p class="hint-text mt-8">R = 按 FSRS 预测的「明天还记得」概率，R 越低越该先复习。数据随 FSRS 参数个性化而更准。</p>`;
}

// ── P0 今日任务清单 ──
function drawTodayTasks(tasks) {
  const el = document.getElementById('taskCard');
  if (!el) return;
  if (!tasks || !tasks.length) { el.innerHTML = '暂无任务'; return; }
  const icons = { review: '📚', error_focus: '🎯', exam: '🏃', done: '✅' };
  el.innerHTML = tasks.map(t =>
    `<div class="flex-between mb-8"><span class="text-sm">${icons[t.kind] || ''} ${escapeHtml(t.label)}</span>
     ${t.kind === 'review' && t.count ? `<a class="btn btn-secondary btn-sm" href="#review">去复习</a>` : ''}</div>`
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
      <span class="text-sm text-muted" style="min-width:52px;text-align:right">${e.count} 题 · ${pct}%</span>
    </div>`;
  }).join('');
  el.insertAdjacentHTML('beforeend', '<p class="hint-text mt-8">条形颜色 = 该错因的平均掌握度（红低绿高）</p>');
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
      listEl.innerHTML = '<div class="empty"><p>暂无题目，点击"新增题目"开始</p></div>';
    } else {
      listEl.innerHTML = items.map(p => `
        <div class="list-item" style="display:flex;gap:10px;align-items:flex-start">
          <input type="checkbox" style="margin-top:3px;accent-color:var(--accent)" onclick="event.stopPropagation();toggleBatch(${p.id},this.checked)" aria-label="选择题目">
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
    <button class="btn btn-secondary btn-sm" ${problemPage <= 1 ? 'disabled' : ''} onclick="loadProblems(${problemPage - 1})">上一页</button>
    <span class="text-sm text-muted">${problemPage} / ${problemPages}</span>
    <button class="btn btn-secondary btn-sm" ${problemPage >= problemPages ? 'disabled' : ''} onclick="loadProblems(${problemPage + 1})">下一页</button>
  `;
}

async function viewProblem(id) {
  try {
    const p = await api(`/api/problems/${id}`);
    document.getElementById('modalTitle').textContent = (p.starred ? '⭐ ' : '') + p.title;
    let html = `
      <div class="flex gap-8 mb-8">
        <span class="tag tag-blue">${escapeHtml(p.course || '未分类')}</span>
        <span class="tag tag-gray">${escapeHtml(p.topic || '无知识点')}</span>
        ${masteryTag(p.mastery)}
      </div>`;
    if (Array.isArray(p.tags) && p.tags.length) {
      html += `<div class="flex gap-8 mb-8" style="flex-wrap:wrap">${p.tags.map(t =>
        `<span class="chip${p.tags_status === 'suggested' ? ' pending' : ''}">${escapeHtml(String(t))}</span>`).join('')}</div>`;
    }
    // A2 先修告警：绑定概念的先修掌握度低时提示
    if (Array.isArray(p.prereq_warnings) && p.prereq_warnings.length) {
      html += `<div style="border:1px solid var(--warning);background:var(--warning-light,rgba(240,180,60,.12));border-radius:8px;padding:10px 12px;margin-bottom:12px">
        <div style="font-size:13px;font-weight:600;color:var(--warning);margin-bottom:4px">⚠ 先修概念掌握度偏低</div>
        ${p.prereq_warnings.map(w =>
          `<span class="tag tag-warn" style="cursor:pointer;margin:2px" title="点击查看先修相关错题" onclick="openPrereqMode(${w.concept_id})">${escapeHtml(w.name)} ${w.mastery}%</span>`).join('')}
        <div class="text-sm text-muted" style="margin-top:4px">建议先巩固先修概念再做本题（一键「先修模式」可过滤相关历史错题）</div>
      </div>`;
    }
    html += `<div class="card" style="border-color:var(--border);margin-bottom:12px">
      <div class="card-title">题目内容</div>
      <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(p.content)}</p>
      ${(p.media_list || []).map(m => `<img class="photo-full" src="/${escapeHtml(m)}" alt="题目图片" onclick="window.open('/${escapeHtml(m)}','_blank')">`).join('')}
    </div>`;
    if (p.my_attempt) {
      html += `<div class="card" style="border-color:var(--border);margin-bottom:12px">
        <div class="card-title">我的尝试</div>
        <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(p.my_attempt)}</p>
      </div>`;
    }
    html += `<div class="card-title mt-16">分级提示</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},1)" id="hint1btn">① 关键词</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},2)" id="hint2btn">② 方向/公式</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},3)" id="hint3btn">③ 解题框架</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},4)" id="hint4btn">④ 全解析</button>
      </div>
      <div id="hintsArea"></div>`;
    if (p.hints && p.hints.length) {
      p.hints.forEach(h => {
        html += `<div class="hint-card"><h4>第 ${h.level} 级提示</h4><p>${escapeHtml(h.content)}</p></div>`;
      });
    }
    html += `<div class="card-title mt-16">一题多解</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="addMethod(${id})">+ 添加解法</button>
      </div>
      <div id="methodsArea">${renderMethods(p.methods || [], id)}</div>
      <div class="card-title mt-16">举一反三（变式题）</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="generateVariants(${id})" id="genVariantsBtn">生成 3 道变式</button>
        <button class="btn btn-primary btn-sm hidden" onclick="saveVariants(${id})" id="saveVariantsBtn">确认保存变式</button>
      </div>
      <div id="variantsArea"></div>
      <div id="savedVariants"></div>
      <div class="card-title mt-16">Feynman 口述反转</div>
      <p class="text-sm text-muted" style="margin-bottom:8px">向新手讲解本题概念 → 对照解析找漏点 → 生成自评表，漏点进入复习队列优先重考</p>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="startFeynman(${id})">开始口述讲解</button>
      </div>
      <div id="feynmanReview"></div>`;
    if (p.feynman_self_review) renderFeynmanReview(p.feynman_self_review);
    renderSavedVariants(p.variants);
    html += `<div class="flex gap-12 mt-16">
      <button class="btn btn-secondary btn-sm" onclick="editProblem(${id})">编辑</button>
      <button class="btn btn-secondary btn-sm" onclick="toggleStar(${id})">${p.starred ? '★ 已收藏' : '☆ 收藏'}</button>
      <button class="btn btn-danger btn-sm" onclick="deleteProblem(${id})">删除</button>
    </div>
    <div id="problemHistory" class="mt-16"></div>
    <div id="relatedProblems" class="mt-16"></div>
    <p class="text-sm text-muted mt-12" style="opacity:0.6">快捷键：1/2/3/4=提示  s=收藏  e=编辑  d=删除</p>`;
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
  const levelName = '第' + '一二三四'[level - 1] + '级提示';
  const diagnoseHtml = (on) => on ? '<p class="hint-text" style="color:var(--warning)">⚠ 上次复习未通过：若还是卡住，建议先看「薄弱知识点」页重练概念，再回本题（诊断门）。</p>' : '';
  const card = document.createElement('div');
  card.className = 'hint-card';
  card.innerHTML = `<h4>${levelName} <span class="tag tag-green">AI</span> <span class="text-muted text-sm">（流式）</span></h4><p id="hintStreamText"></p>`;
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
      throw new Error(err.error || `请求失败 (${r.status})`);
    }
    const ctype = r.headers.get('Content-Type') || '';
    if (!ctype.includes('text/event-stream')) {
      const data = await r.json();
      const srcTag = data.source === 'ai' ? '<span class="tag tag-green">AI</span>' :
                     data.source === 'fallback' ? '<span class="tag tag-amber">降级</span>' : '<span class="tag tag-gray">缓存</span>';
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
            card.querySelector('.tag-green').textContent = '降级';
            card.querySelector('.tag-green').className = 'tag tag-amber';
            toast('AI 流式输出中断，已显示离线提示', 'warn');
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
          toast(`流式连接中断，正在重连（${attempt}/2）...`, 'warn');
          await new Promise(res => setTimeout(res, 800));
        } else if (attempt >= 3) {
          toast('流式连接中断，重连失败', 'error');
        } else {
          throw e;
        }
      }
    }
    if (!ok) toast('流式连接中断', 'error');
    finishHintBtn(btn, levelName);
  } catch(e) {
    toast(e.message, 'error'); btn.disabled = false; btn.textContent = levelName;
  }
}

function finishHintBtn(btn, levelName) {
  btn.textContent = '已查看'; btn.style.opacity = '0.5';
}

function openProblemModal() { editProblem(null); }

async function editProblem(id) {
  const modal = document.getElementById('editModal');
  const titleEl = document.getElementById('editModalTitle');
  if (id) {
    titleEl.textContent = '编辑题目';
    try {
      const p = await api(`/api/problems/${id}`);
      document.getElementById('editId').value = p.id;
      document.getElementById('editTitle').value = p.title || '';
      document.getElementById('editCourse').value = p.course || '';
      document.getElementById('editTopic').value = p.topic || '';
      document.getElementById('editContent').value = p.content || '';
      document.getElementById('editAttempt').value = p.my_attempt || '';
      document.getElementById('editErrorType').value = p.error_type || '待诊断';
      document.getElementById('editMastery').value = p.mastery || 1;
      document.getElementById('editStarred').checked = p.starred === 1;
      currentTags = Array.isArray(p.tags) ? p.tags.map(t => ({ text: String(t), pending: p.tags_status === 'suggested' })) : [];
      renderTags();
      document.getElementById('editTagInput').value = '';
      renderEditPhotos(Array.isArray(p.media_list) ? p.media_list : []);
    } catch(e) { toast(e.message, 'error'); return; }
  } else {
    titleEl.textContent = '新增题目';
    document.getElementById('editId').value = '';
    ['editTitle','editCourse','editTopic','editContent','editAttempt'].forEach(i => document.getElementById(i).value = '');
    document.getElementById('editErrorType').value = '待诊断';
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
      el.innerHTML = `⚠ 发现 ${r.duplicates.length} 道相似题：${links}`;
    } catch(e) { /* 静默 */ }
  }, 800);
}

// ── C7 语音输入（webkitSpeechRecognition，Chrome/Edge）──
function startVoiceInput(targetId) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = document.getElementById('voiceBtn');
  const ta = document.getElementById(targetId);
  if (!SR) { toast('当前浏览器不支持语音输入（需 Chrome/Edge）', 'warn'); return; }
  if (btn.dataset.rec === '1') {
    btn.dataset.rec = '0';
    btn.textContent = '🎤 语音输入';
    if (_rec) { _rec.stop(); _rec = null; }
    return;
  }
  const rec = new SR();
  _rec = rec;
  rec.lang = 'zh-CN';
  rec.interimResults = true;
  btn.dataset.rec = '1';
  btn.textContent = '⏺ 录音中…（点此停止）';
  rec.onresult = (ev) => {
    let text = '';
    for (let i = 0; i < ev.results.length; i++) text += ev.results[i][0].transcript;
    ta.value = ta.value.replace(/\s*$/, '') + (ta.value.trim() ? '\n' : '') + text;
  };
  rec.onend = () => {
    _rec = null;
    btn.dataset.rec = '0';
    btn.textContent = '🎤 语音输入';
  };
  rec.onerror = (e) => {
    if (e.error !== 'aborted') toast('语音识别失败：' + e.error, 'error');
    btn.dataset.rec = '0';
    btn.textContent = '🎤 语音输入';
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
  if (!body.title.trim() || !body.content.trim()) { toast('标题和题目内容不能为空', 'error'); return; }
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
  const ok = await confirmDialog('确定删除这道题目？相关提示和复习记录也会被删除。');
  if (!ok) return;
  let cancelled = false;
  const toastEl = document.createElement('div');
  toastEl.className = 'toast error';
  toastEl.setAttribute('role', 'status');
  toastEl.setAttribute('aria-live', 'polite');
  const undoLink = document.createElement('a');
  undoLink.href = '#';
  undoLink.style.cssText = 'color:#fff;text-decoration:underline;cursor:pointer';
  undoLink.textContent = '撤销';
  undoLink.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    cancelled = true;
    toastEl.remove();
    toast('已取消删除', 'success');
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
    toast('已切换收藏');
    closeModal('problemModal'); viewProblem(id);
  } catch(e) { toast(e.message, 'error'); }
}

async function loadHistory(id) {
  try {
    const history = await api(`/api/problems/${id}/history`);
    const el = document.getElementById('problemHistory');
    if (!history.length) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="card-title">复习轨迹</div>` +
      history.map(h => {
        const labels = {1:'❌忘记',2:'⚠模糊',3:'✓正确',4:'✅掌握'};
        const cls = h.result === '4' ? 'tag-green' : h.result === '3' ? 'tag-blue' : h.result === '2' ? 'tag-amber' : 'tag-red';
        return `<span class="tag ${cls}" style="margin:1px 4px" title="${h.due_date} · 间隔${h.interval_days}天">${labels[h.result]||h.result}</span>`;
      }).join(' ');
  } catch(e) {}
}

async function loadRelated(id) {
  try {
    const related = await api(`/api/problems/${id}/related`);
    const el = document.getElementById('relatedProblems');
    if (!related.length) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="card-title">同知识点题目</div>` +
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
    const ok = await confirmDialog(`确定批量删除 ${ids.length} 道题目？`);
    if (!ok) return;
  }
  try {
    await api('/api/problems/batch', { method: 'POST', body: { ids, action } });
    toast(`已处理 ${ids.length} 题`);
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
    el.innerHTML = `${escapeHtml(s.tip)}` +
      (s.due_tomorrow > 0 ? `<br><span class="text-muted">明日到期 ${s.due_tomorrow} 题</span>` : '') +
      (errParts ? `<br><span class="text-muted">错因分布：${escapeHtml(errParts)}</span>` : '');
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
      el.innerHTML = '<div class="empty"><p>今天没有待复习的题目，去看看概览页吧</p></div>';
      return;
    }
    const today = new Date().toISOString().slice(0, 10);
    const dueCount = list.filter(r => r.due_date <= today).length;
    document.getElementById('reviewProgress').innerHTML = `
      <div style="height:8px;background:var(--border);border-radius:4px;overflow:hidden;margin-bottom:10px">
        <div style="height:100%;width:0%;background:var(--accent);border-radius:4px;transition:width .3s" id="reviewProgressBar"></div>
      </div>
      <span class="text-sm text-muted" id="reviewProgressText">今日到期 ${dueCount} 题 · 完成 0</span>
    `;
    let completed = 0;
    const updateProgress = () => {
      completed++;
      const bar = document.getElementById('reviewProgressBar');
      if (bar) bar.style.width = (completed / (dueCount || 1) * 100).toFixed(0) + '%';
      const text = document.getElementById('reviewProgressText');
      if (text) text.textContent = `今日到期 ${dueCount} 题 · 完成 ${completed}`;
    };
    window._reviewUpdateProgress = updateProgress;
    el.innerHTML = list.map(r => `
      <div class="list-item">
        <div class="list-item-header">
          <span class="list-item-title">${escapeHtml(r.title)}${r.variant_id ? ' <span class="tag tag-blue">变式</span>' : ''}${r.feynman_gaps ? ` <span class="tag tag-warn" title="Feynman 自评漏点未清，优先重考">Feynman 漏点×${r.feynman_gaps}</span>` : ''}</span>
          <span class="tag ${r.due_date <= today ? 'tag-red' : 'tag-gray'}">
            ${r.due_date <= today ? '今日到期' : '即将到期'}
          </span>
        </div>
        <div class="list-item-meta">${escapeHtml(r.course)} · ${escapeHtml(r.topic)} · 到期日: ${r.due_date} · 间隔: ${r.interval_days}天</div>
        <div class="flex gap-8 mt-12 flex-wrap">
          <button class="btn btn-danger btn-sm" onclick="completeReview(${r.id},1)">忘记</button>
          <button class="btn btn-secondary btn-sm" onclick="completeReview(${r.id},2)">模糊</button>
          <button class="btn btn-secondary btn-sm" onclick="completeReview(${r.id},3)">基本正确</button>
          <button class="btn btn-primary btn-sm" onclick="completeReview(${r.id},4)">完全掌握</button>
          <button class="btn btn-secondary btn-sm" onclick="rescheduleReview(${r.id})">再复习一次</button>
          <button class="btn btn-secondary btn-sm" onclick="viewProblem(${r.problem_id})">查看题目</button>
        </div>
      </div>`).join('');
  } catch(e) { toast(e.message, 'error'); }
}

async function completeReview(id, rating) {
  try {
    const r = await api(`/api/reviews/${id}/complete`, { method: 'POST', body: { rating } });
    const labels = {1:'已标记为忘记',2:'已标记为模糊',3:'已标记为基本正确',4:'已标记为完全掌握'};
    toast(`${labels[rating]} · 下次复习: ${r.next_due} (${r.interval_days}天后)`);
    if (window._reviewUpdateProgress) window._reviewUpdateProgress();
    loadReviews();
    if (document.getElementById('page-dashboard').classList.contains('active')) loadDashboard();
  } catch(e) { toast(e.message, 'error'); }
}

async function rescheduleReview(id) {
  try {
    await api(`/api/reviews/${id}/reschedule`, { method: 'PUT' });
    toast('已提前到今天复习');
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
    <div style="font-size:13px;font-weight:600;margin-bottom:6px">已保存自评表</div>
    ${sr.gaps && sr.gaps.length ? `<div class="text-sm" style="margin-bottom:4px"><b style="color:var(--warning)">漏点：</b>${sr.gaps.map(g => `<span class="tag tag-warn" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
    ${sr.wrong && sr.wrong.length ? `<div class="text-sm" style="margin-bottom:4px"><b style="color:var(--danger,#ef4444)">讲错：</b>${sr.wrong.map(g => `<span class="tag tag-red" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
    ${sr.clear && sr.clear.length ? `<div class="text-sm"><b style="color:var(--success,#22c55e)">讲清：</b>${sr.clear.map(g => `<span class="tag tag-green" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
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
    document.getElementById('oralTopicDisplay').textContent = 'Feynman 口述反转';
    document.getElementById('oralTurn').textContent = '第 1 / 3 步';
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
    if (!sr) { toast('自评表为空', 'error'); return; }
    const chatEl = document.getElementById('oralChat');
    chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
      <div class="card-title" style="font-size:14px">自评表（确认后保存）</div>
      ${sr.gaps && sr.gaps.length ? `<div class="text-sm"><b>漏点：</b>${sr.gaps.map(g => `<span class="tag tag-warn" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
      ${sr.wrong && sr.wrong.length ? `<div class="text-sm"><b>讲错：</b>${sr.wrong.map(g => `<span class="tag tag-red" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
      ${sr.clear && sr.clear.length ? `<div class="text-sm"><b>讲清：</b>${sr.clear.map(g => `<span class="tag tag-green" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</div>` : ''}
      <div class="flex gap-8 mt-8">
        <button class="btn btn-primary btn-sm" onclick="confirmFeynmanSelfReview()">确认保存</button>
        <button class="btn btn-secondary btn-sm" onclick="this.closest('.bubble').remove()">关闭</button>
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
    toast('自评表已保存，漏点将进入复习队列优先重考');
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
      ` <span class="tag ${v.correct / v.total >= 0.8 ? 'tag-green' : 'tag-amber'}">正确率 ${v.correct}/${v.total}</span>` : '';
    return `<div class="hint-card"><h4>变式 ${i + 1}（${escapeHtml(v.mode || '未分类')}）${q}</h4>
      <p>${escapeHtml(v.title)}</p>
      <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(v.content)}</p>
      <p class="text-sm text-muted">答案：${escapeHtml(v.answer || '—')}</p></div>`;
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
        <h4>${escapeHtml(v.mode || '变式')} ${i + 1}</h4>
        <p>${escapeHtml(v.title)}</p>
        <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(v.content)}</p>
        <p class="text-sm text-muted">参考答案：${escapeHtml(v.answer || '—')}</p>
      </div>`).join('') || '<p class="text-sm text-muted">未能生成变式</p>';
    document.getElementById('saveVariantsBtn').classList.remove('hidden');
    toast(r.source === 'local' ? '已用离线模板生成变式（未配置 AI 时自动降级）' : '已生成 3 道变式草稿，确认后才会保存');
  } catch(e) { toast(e.message, 'error'); } finally { btn.disabled = false; }
}

async function saveVariants(id) {
  if (!draftVariants.length) return;
  try {
    const r = await api(`/api/problems/${id}/variants`, { method: 'POST', body: { variants: draftVariants } });
    draftVariants = [];
    document.getElementById('variantsArea').innerHTML = '';
    document.getElementById('saveVariantsBtn').classList.add('hidden');
    toast(`已保存 ${r.count} 道变式（共 ${r.total} 道）`);
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
      <div class="card-title" style="font-size:14px">复习卡草稿</div>
      <div class="text-sm"><b>标题：</b>${escapeHtml(d.title || '')}</div>
      <div class="text-sm" style="white-space:pre-wrap"><b>题目：</b>${escapeHtml(d.content || '')}</div>
      <div class="text-sm"><b>知识点：</b>${escapeHtml(d.topic || '')} · <b>错因：</b>${escapeHtml(d.error_type || '')}</div>
      <div class="flex gap-8 mt-8">
        <button class="btn btn-primary btn-sm" onclick="saveOralCard()">确认保存为错题</button>
        <button class="btn btn-secondary btn-sm" onclick="this.closest('.bubble').remove()">不要</button>
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
      title: d.title || '口试复盘', content: d.content || '', topic: d.topic || '',
      error_type: d.error_type || '概念理解', my_attempt: d.my_attempt || '',
      tags: d.tags || [],
    }});
    _oralDraft = null;
    toast('已保存为错题卡，可在「错题」页查看');
  } catch(e) { toast(e.message, 'error'); }
}

// ── 口试 ──
let oralSessionId = null;
let oralTurn = 0;

async function startOral() {
  _feynmanMode = false;
  const topic = document.getElementById('oralTopic').value.trim();
  if (!topic) { toast('请输入口试主题', 'error'); return; }
  try {
    const r = await api('/api/oral/start', { method: 'POST', body: { topic } });
    oralSessionId = r.session_id;
    oralTurn = 1;
    document.getElementById('oralStartCard').classList.add('hidden');
    document.getElementById('oralChatCard').classList.remove('hidden');
    document.getElementById('oralTopicDisplay').textContent = `主题: ${topic}`;
    document.getElementById('oralTurn').textContent = `第 1 / 5 轮`;
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
  div.innerHTML = '<div class="bubble">AI 思考中…</div>';
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
      document.getElementById('oralTurn').textContent = _feynmanMode ? 'Feynman 已完成' : '口试已结束';
      document.getElementById('oralAnswer').placeholder = '已结束，可重新开始';
      if (_feynmanMode) {
        chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
          <button class="btn btn-primary btn-sm" onclick="showFeynmanSelfReview()">生成并确认自评表</button>
          <span class="text-sm text-muted">漏点将标记到本题复习队列</span>
        </div></div>`;
      } else {
        chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
          <button class="btn btn-primary btn-sm" onclick="draftOralCard()">生成复习卡草稿</button>
          <span class="text-sm text-muted">将本场薄弱点转成一张错题卡（确认后才会保存）</span>
        </div></div>`;
      }
      chatEl.scrollTop = chatEl.scrollHeight;
    } else {
      document.getElementById('oralTurn').textContent = _feynmanMode ? `第 ${oralTurn + 1} / 3 步` : `第 ${oralTurn} / 5 轮`;
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
      <span class="text-sm text-muted" style="flex:1">近30天 ${t.recent_count} 题（${t.recent_pct}%） · 历史 ${t.total_pct}%</span>
      <span class="tag ${cls}">${arrow} ${up ? '+' : ''}${t.delta}%</span>
    </div>`;
  }).join('');
}

// ── C7 考试冲刺卡（倒计时 + 每日计划）──
function renderSprint(goal, stats) {
  const el = document.getElementById('sprintCard');
  if (!el) return;
  if (!goal || !goal.exam_date) { el.innerHTML = '未设置考试日期（设置页 → 学习者档案）'; return; }
  const days = Math.ceil((new Date(goal.exam_date) - new Date()) / 86400000);
  const target = goal.exam_target_score ? `目标 ${goal.exam_target_score} 分` : '';
  const total = stats ? (stats.total || 0) : 0;
  const mastered = stats ? (stats.mastered || 0) : 0;
  const remaining = Math.max(0, total - mastered);
  let plan = '';
  if (days > 0 && remaining > 0) {
    const perDay = Math.ceil(remaining / days);
    plan = `距考试 <b>${days} 天</b>，未掌握 <b>${remaining}</b> 题 → 每天至少 <b>${perDay}</b> 题`;
  } else if (days <= 0) {
    plan = `<span class="tag tag-red">考试已到/已过</span>`;
  } else {
    plan = '全部题目已掌握 🎉';
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
    let goalText = '未设定目标';
    if (goal.exam_date) {
      const days = Math.ceil((new Date(goal.exam_date) - new Date()) / 86400000);
      goalText = `考试 ${goal.exam_date}（剩 ${days} 天）` + (goal.exam_target_score ? `，目标 ${goal.exam_target_score} 分` : '');
    }
    el.innerHTML =
      `<b>知识点</b>：${topicLine || '暂无'}<br>` +
      `<b>错因</b>：${errLine || '无'}<br>` +
      `<b>节奏</b>：近7天复习 ${pace.week_reviews} 次、新增 ${pace.week_new_problems} 题，常活跃 ${pace.peak_hour} 时<br>` +
      `<b>目标</b>：${escapeHtml(goalText)}`;
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
    toast('目标已更新');
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
    if (!items.length) { toast('暂无题目可打印', 'warn'); return; }
    const area = document.getElementById('printArea');
    const sorted = [...items].sort((a, b) => (b.mastery || 0) - (a.mastery || 0));
    area.innerHTML = `<h2>物理错题集（共 ${items.length} 题，${new Date().toLocaleDateString()}）</h2>` +
      sorted.map(p => `<div class="print-item">
        <div class="print-title">${escapeHtml(p.title || '未命名')} · 掌握度 ${p.mastery}/5</div>
        <div class="print-meta">${escapeHtml(p.course || '')} · ${escapeHtml(p.topic || '')} · 错因：${escapeHtml(p.error_type || '待诊断')}</div>
        <pre>${escapeHtml(p.content || '')}</pre>
        ${p.my_attempt ? `<div class="print-hdr">我的尝试</div><pre>${escapeHtml(p.my_attempt)}</pre>` : ''}
        ${p.fix_action ? `<div class="print-hdr">对策</div><pre>${escapeHtml(p.fix_action)}</pre>` : ''}
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
    if (items.length < 3) { toast('题太少，至少 3 题才能组卷', 'warn'); return; }
    const quiz = [...items]
      .sort((a, b) => (a.mastery || 0) - (b.mastery || 0))
      .slice(0, 30);
    const buckets = {};
    for (const p of quiz) {
      const k = p.topic || '未分类';
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
      `<h2>物理考前自测卷（${quiz.length} 题 · 建议 ${minutes} 分钟 · ${new Date().toLocaleDateString()}）</h2>
      <p class="hint-text">本卷不含答案与对策；完成后请到系统中核对「我的尝试」。</p>
      ${ordered.map((p, i) => `<div class="print-item">
        <div class="print-title">第 ${i+1} 题 · ${escapeHtml(p.course || '')} · ${escapeHtml(p.topic || '')}</div>
        <pre>${escapeHtml(p.content || '')}</pre>
        <div class="print-answer-line">我的作答：</div>
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
  if (!list.length) { toast('今天没有待复习的题目', 'warn'); return; }
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
      <div class="print-hdr">我的尝试</div>
      <pre class="flash-content" id="flashAttempt"></pre>
      <div class="print-hdr">对策</div>
      <pre class="flash-content" id="flashFix"></pre>
    </div>
    <p class="hint-text text-center">点击卡片翻面查看答案；记住键盘 ← 忘了 / → 记得</p>`;
  document.querySelector('#flashModal .modal-footer').classList.remove('hidden');
  openModal('flashModal');
  _flashRender();
}

function _flashRender() {
  const r = _flashQueue[_flashIdx];
  if (!r) { _flashFinish(); return; }
  document.getElementById('flashCount').textContent =
    `第 ${_flashIdx + 1} / ${_flashQueue.length} 题（已答 ${_flashDone}）`;
  document.getElementById('flashMeta').textContent =
    `${escapeHtml(r.course || '')} · ${escapeHtml(r.topic || '')} · 错因：${escapeHtml(r.error_type || '待诊断')}`;
  document.getElementById('flashContent').textContent = r.content || '(无题干)';
  document.getElementById('flashAttempt').textContent = r.my_attempt || '(无记录)';
  document.getElementById('flashFix').textContent = r.fix_action || '(无对策)';
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
    <h3 style="margin-bottom:12px">🎉 完成闪电复习</h3>
    <p class="text-muted">本次共 ${_flashQueue.length} 题，全部已按记忆情况记入 FSRS 调度。</p>
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
    return '<p class="text-sm text-muted">暂无其他解法。可在复习时「换一种思路重做」，会记得更牢。</p>';
  }
  return '<div class="flex column gap-8">' + methods.map((m, i) =>
    `<div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px">
      <div class="flex-between mb-4">
        <b class="text-sm">解法 ${i + 1}</b>
        <button class="btn btn-secondary btn-sm" onclick="removeMethod(${id},${i})">删除</button>
      </div>
      <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(m)}</p>
    </div>`).join('') + '</div>';
}

async function addMethod(id) {
  const text = window.prompt('输入一种新解法（可多行，将追加到本题）：', '');
  if (text === null) return;
  const p = await api(`/api/problems/${id}`);
  const methods = [...(p.methods || []), text.trim()].filter(Boolean);
  try {
    await api(`/api/problems/${id}`, { method: 'PUT', body: { methods } });
    document.getElementById('methodsArea').innerHTML = renderMethods(methods, id);
    toast('解法已保存');
    renderMath(document.getElementById('methodsArea'));
  } catch(e) { toast(e.message, 'error'); }
}

async function removeMethod(id, idx) {
  const p = await api(`/api/problems/${id}`);
  const methods = (p.methods || []).filter((_, i) => i !== idx);
  await api(`/api/problems/${id}`, { method: 'PUT', body: { methods } });
  document.getElementById('methodsArea').innerHTML = renderMethods(methods, id);
  toast('解法已删除');
}

// ── 设置 ──
async function probeLocalModels() {
  const el = document.getElementById('ollamaStatus');
  if (!el) return;
  try {
    const r = await api('/api/models/probe');
    if (r.ollama && r.ollama.available) {
      const list = (r.ollama.models || []).slice(0, 5).join(', ');
      el.innerHTML = `本地模型可用：Ollama（${escapeHtml(list)}…）。API 地址填 <code>http://localhost:11434/v1</code>，密钥留空即可。`;
      el.style.color = 'var(--success)';
    } else {
      el.textContent = '未检测到本地 Ollama（可选）。需要本地模型请自行安装 Ollama，再填 http://localhost:11434/v1 并留空密钥。';
      el.style.color = 'var(--text-2)';
    }
  } catch(e) {
    el.textContent = 'Ollama 探测失败（可选功能，不影响云端使用）';
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
      `<span class="photo-preview"><img src="/${escapeHtml(p)}" alt="题目图片"></span>`).join('');
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
    toast('图片已上传，可作为附件保存', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function extractPhoto() {
  const path = _editPhotos[_editPhotos.length - 1];
  if (!path) return;
  const btn = document.getElementById('extractPhotoBtn');
  btn.disabled = true;
  btn.textContent = '识别中…';
  try {
    const r = await api('/api/ai/extract-photo', { method: 'POST', body: { media_path: path } });
    if (!r.draft) {
      toast(r.error || '未配置视觉模型，请手动录入题目', 'info');
      return;
    }
    const d = r.draft;
    if (d.title) document.getElementById('editTitle').value = d.title;
    if (d.topic) document.getElementById('editTopic').value = d.topic;
    if (d.content) document.getElementById('editContent').value = d.content;
    if (d.answer) {
      const hint = document.getElementById('editContent').value;
      document.getElementById('editContent').value = hint + (hint ? '\n\n' : '') +
        `【答案】${d.answer}` + (d.analysis ? `\n【解析】${d.analysis}` : '');
    }
    toast('已填入识别草稿，请核对修改后保存（确认制）', 'success');
  } catch(e) { toast(e.message, 'error'); }
  finally {
    btn.disabled = false;
    btn.textContent = 'AI 识别题目';
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
  return `<div class="rag-sources"><span class="text-sm text-muted">📚 教材出处：</span>` +
    sources.map(s =>
      `<button class="btn btn-link btn-sm" onclick="openRagSource('${encodeURIComponent(s.path)}')">` +
      `${escapeHtml(s.name)}${s.page ? ` · 第${s.page}页` : ''}</button>`).join('') + `</div>`;
}

async function openRagSource(encPath) {
  try {
    await api(`/api/rag/open?path=${encPath}`);
  } catch(e) { toast(e.message, 'error'); }
}

async function ingestRag() {
  const path = document.getElementById('ragPath').value.trim();
  if (!path) { toast('请输入路径', 'error'); return; }
  const status = document.getElementById('ragStatus');
  status.textContent = '正在摄取…';
  try {
    const r = await api('/api/rag/ingest', { method: 'POST', body: { path } });
    status.textContent = `已摄取 ${r.docs || 1} 个文档，共 ${r.chunks} 块` +
      (r.errors && r.errors.length ? `；跳过：${r.errors.join('；')}` : '');
    toast('摄取完成');
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
    if (!r.items.length) { el.innerHTML = '<p class="text-sm text-muted">尚未摄取任何文档</p>'; return; }
    el.innerHTML = r.items.map(d => `
      <div class="list-item" style="padding:8px 0">
        <div class="list-item-header">
          <span class="list-item-title">${escapeHtml(d.source_path)}</span>
          <span class="tag tag-gray">${d.chunk_count} 块</span>
        </div>
        <div class="flex gap-8 mt-8">
          <button class="btn btn-secondary btn-sm" onclick="openRagSource('${encodeURIComponent(d.source_path)}')">打开</button>
          <button class="btn btn-danger btn-sm" onclick="deleteRagDoc(${d.id})">移除</button>
        </div>
      </div>`).join('');
  } catch(e) { el.innerHTML = `<p class="text-sm text-muted">${escapeHtml(e.message)}</p>`; }
}

async function deleteRagDoc(id) {
  const ok = await confirmDialog('移除该文档的索引？（不删除原文件）');
  if (!ok) return;
  try {
    await api(`/api/rag/doc/${id}`, { method: 'DELETE' });
    toast('已移除索引');
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
    el.textContent = 'OCR 能力：' + parts.join(' · ') +
      (r.paddleocr ? '（扫描版可 OCR）' : '（扫描版需安装 paddleocr；文本层 PDF 不受影响）');
  } catch(e) { el.textContent = '能力探测失败: ' + e.message; }
}

function collectOcrTexts() {
  return Array.from(document.querySelectorAll('.ocr-text'))
    .map((t, i) => `【第 ${i + 1} 页】\n${t.value}`).join('\n\n');
}

async function runOcr() {
  const path = document.getElementById('ocrPath').value.trim();
  if (!path) { toast('请输入路径', 'error'); return; }
  const el = document.getElementById('ocrResultList');
  el.innerHTML = '<p class="text-sm text-muted">OCR 提取中（扫描版较慢）…</p>';
  _ocrResultText = '';
  try {
    const r = await api('/api/ocr/extract', { method: 'POST', body: { path } });
    const pages = r.pages || [];
    _ocrResultText = collectOcrTexts();
    el.innerHTML = `
      <p class="text-sm text-muted mb-8">引擎: ${escapeHtml(r.engine)} · ${pages.length} 项 · 平均置信度 ${pages[0] ? pages[0].confidence : '-'}</p>
      ${pages.map(p => `
        <div class="ocr-page mb-8">
          <div class="text-sm" style="font-weight:600;margin-bottom:4px">第 ${p.page} 项 <span class="text-muted">（置信度 ${p.confidence}）</span></div>
          <textarea class="form-input ocr-text" style="width:100%;min-height:120px;font-family:monospace" oninput="_ocrResultText = collectOcrTexts()">${escapeHtml(p.text)}</textarea>
        </div>`).join('')}`;
    _ocrResultText = collectOcrTexts();
    toast('OCR 完成，请人工核对');
  } catch(e) {
    el.innerHTML = `<p class="text-sm tag tag-red" style="white-space:pre-line">${escapeHtml(e.message)}</p>`;
  }
}

async function copyOcrResult() {
  if (!_ocrResultText) { toast('先执行 OCR 提取', 'error'); return; }
  try {
    await navigator.clipboard.writeText(_ocrResultText);
    toast('已复制全文');
  } catch(e) {
    const ta = document.createElement('textarea');
    ta.value = _ocrResultText; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
    toast('已复制全文');
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
  el.innerHTML = '<div class="loading">搜索中…</div>';
  try {
    const r = await api(`/api/rag/search?q=${encodeURIComponent(q)}&k=5`);
    if (!r.items.length) { el.innerHTML = '<p class="text-sm text-muted">没有匹配的教材片段</p>'; return; }
    el.innerHTML = r.items.map(hit => `
      <div class="hint-card" style="margin-bottom:8px">
        <div class="flex-between">
          <span class="text-sm" style="font-weight:600">${escapeHtml(hit.name)}${hit.page ? ` · 第${hit.page}页` : ''}</span>
          <span class="tag tag-gray">${hit.score}</span>
        </div>
        <p class="text-sm" style="margin:6px 0">${escapeHtml(hit.content)}</p>
        <button class="btn btn-link btn-sm" onclick="openRagSource('${encodeURIComponent(hit.source_path)}')">打开原文 ↗</button>
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
  if (!name) { toast('请输入试卷名称', 'error'); return; }
  try {
    const r = await api('/api/exam/papers', { method: 'POST', body: {
      name,
      exam_date: document.getElementById('examDate').value.trim(),
      target: parseInt(document.getElementById('examTarget').value, 10) || 80,
    }});
    toast('试卷已创建');
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
      ov.innerHTML = '<p class="text-sm text-muted mt-8">尚未创建试卷。录入真题考点后即可看到就绪度。</p>';
      el.innerHTML = '';
      return;
    }
    ov.innerHTML = `<div class="flex-between mb-8">
        <span class="text-sm">全局就绪度（全部试卷平均）</span>
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
            <span class="text-sm text-muted">目标 ${p.paper.target}%</span>
            <span class="tag ${ready >= p.paper.target ? 'tag-green' : ready >= p.paper.target * 0.75 ? 'tag-amber' : 'tag-red'}">就绪度 ${ready}%</span>
          </span>
        </div>
        <div class="list-item-meta">${escapeHtml(p.paper.exam_date || '未定日期')} · ${p.question_count} 题 · 考点命中率 ${p.hit_rate}%${p.gap_to_target > 0 ? ` · 距目标还差 ${p.gap_to_target}%` : ''}</div>
        ${examBar(ready)}
        ${p.gaps.length ? `<p class="text-sm mt-8"><b style="color:var(--warning)">薄弱考点：</b>${p.gaps.map(g => `<span class="tag tag-warn" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</p>` : '<p class="text-sm text-muted mt-8">✓ 全部考点掌握度达标</p>'}
        <div class="flex gap-8 mt-12">
          <button class="btn btn-secondary btn-sm" onclick="loadExamDetail(${p.paper.id})">查看/录入题目</button>
          <button class="btn btn-danger btn-sm" onclick="deleteExamPaper(${p.paper.id})">删除试卷</button>
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
        <h3>${escapeHtml(p.paper.name)} — 就绪度 ${p.readiness}%（目标 ${p.paper.target}%）</h3>
        <button class="modal-close" onclick="this.closest('.modal-overlay').classList.remove('active')">&times;</button>
      </div>
      <p class="text-sm text-muted">按行录入：题号 / 考点（须与错题本 topic 一致才能计入掌握度）/ 权重（默认 1）。</p>
      <textarea id="examQInput" rows="6" class="form-input" placeholder="每行：题号|考点|权重&#10;例如：&#10;1|牛顿第二定律|2&#10;2|动量守恒|1"></textarea>
      <div class="flex gap-12 mt-12">
        <button class="btn btn-primary btn-sm" onclick="saveExamQuestions(${id})">保存题目</button>
        <button class="btn btn-secondary btn-sm" onclick="this.closest('.modal-overlay').classList.remove('active')">关闭</button>
      </div>
      ${p.questions && p.questions.length ? `<table class="table" style="margin-top:12px"><thead><tr><th>题号</th><th>考点</th><th>权重</th></tr></thead><tbody>${rowsHtml}</tbody></table>` : ''}
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
  if (!text) { toast('请输入题目', 'error'); return; }
  const questions = [];
  for (const line of text.split('\n')) {
    const parts = line.split('|').map(s => s.trim());
    if (!parts[1]) continue;
    questions.push({ qno: parts[0], topic: parts[1], weight: parseFloat(parts[2]) || 1 });
  }
  if (!questions.length) { toast('没有有效行（需：题号|考点|权重）', 'error'); return; }
  try {
    await api(`/api/exam/papers/${paperId}/questions`, { method: 'POST', body: { questions } });
    toast(`已添加 ${questions.length} 题`);
    loadExam();
    const ov = document.querySelector('.modal-overlay');
    if (ov) ov.remove();
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteExamPaper(id) {
  const ok = await confirmDialog('删除该试卷及其全部题目？');
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
    document.getElementById('setApiKey').placeholder = s.has_api_key ? '••••••••（已配置，留空则不变）' : 'sk-...';
    document.getElementById('setModel').value = s.model || '';
    document.getElementById('setFastModel').value = s.fast_model || '';
    document.getElementById('setHeavyModel').value = s.heavy_model || '';
    document.getElementById('setVisionModel').value = s.vision_model || '';
    document.getElementById('setMasterPassword').value = '';
    document.getElementById('setMasterPassword').placeholder = s.key_source === 'keyfile' ? '••••••••（已从 keys.enc 读取密钥）' : '用于加密保存 API Key';
    document.getElementById('setTemp').value = s.temperature || '0.3';
    const srcLabel = {
      environment: '当前使用环境变量中的密钥（优先级最高，不写入数据库）',
      keyfile: '密钥已从工作区 keys.enc 解密加载（加密文件，不写入数据库）',
      runtime: '密钥已录入本次运行的内存（重启后失效，不写入数据库）',
      none: '尚未配置密钥',
    };
    document.getElementById('keyStatus').textContent = srcLabel[s.key_source] || srcLabel.none;
    loadPrefs();
  } catch(e) { toast(e.message, 'error'); }
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
    toast('偏好已保存');
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
      el.textContent = 'FSRS 未启用（vendor 缺失），当前使用 SM-2 调度。';
      el.style.color = 'var(--warning)';
      return;
    }
    const src = s.params_source === 'trained'
      ? `<b>个性化参数</b>（训练于 ${escapeHtml(s.trained_at)}，样本 ${s.sample_count} 条）`
      : '<b>默认参数</b>（用复习历史训练后会更贴合你的记忆曲线）';
    let extra = '';
    if (s.training) extra = ' <span class="tag tag-amber">训练中…</span>';
    else if (s.last_train) extra = ` <span class="tag tag-green">上次训练成功</span>`;
    else if (s.train_error) extra = ` <span class="tag tag-red">上次训练未成功</span>`;
    el.innerHTML = `FSRS 调度已启用 · ${src} · 目标保持率 ${s.desired_retention}${extra}`;
    const ret = document.getElementById('fsrsRetention');
    if (ret) { ret.value = s.desired_retention; document.getElementById('fsrsRetentionVal').textContent = s.desired_retention; }
    if (s.training) { setTimeout(loadFsrsStatus, 3000); }
  } catch(e) { el.textContent = 'FSRS 状态加载失败'; }
}

async function saveFsrsRetention() {
  try {
    const r = await api('/api/fsrs/retention', {
      method: 'POST',
      body: { value: parseFloat(document.getElementById('fsrsRetention').value) },
    });
    toast(r.ok ? '目标保持率已保存' : '值需在 0.75-0.97 之间', r.ok ? '' : 'error');
    if (r.ok) loadFsrsStatus();
  } catch(e) { toast(e.message, 'error'); }
}

async function trainFsrs() {
  const btn = document.getElementById('trainFsrsBtn');
  btn.disabled = true;
  try {
    const r = await api('/api/fsrs/train', { method: 'POST', body: {} });
    if (r.started) {
      toast(`已开始训练（${r.sample_count} 条复习记录）…`);
      setTimeout(loadFsrsStatus, 2000);
    } else {
      toast('无法训练：' + (r.error || '未知原因'), 'error');
    }
  } catch(e) { toast(e.message, 'error'); }
  btn.disabled = false;
}

async function resetFsrs() {
  try {
    const r = await api('/api/fsrs/reset', { method: 'POST', body: {} });
    toast(r.ok ? '已重置为默认参数' : '重置失败', r.ok ? '' : 'error');
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
    toast('设置已保存');
    loadSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function testSettings() {
  try {
    const r = await api('/api/settings/test', { method: 'POST', body: {} });
    if (r.ok) toast('连接成功: ' + r.reply);
  } catch(e) { toast('连接失败: ' + e.message, 'error'); }
}

// ── 数据导入 / 导出 ──
function _downloadFromApi(path, filename) {
  return fetch(path, { headers: { 'X-Requested-With': 'PhysicsStudyOS' } })
    .then(r => { if (!r.ok) throw new Error(`导出失败 (${r.status})`); return r.blob(); })
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
    toast('已导出 Anki-CSV（Anki 桌面端可导入）');
  } catch(e) { toast(e.message, 'error'); }
}

async function exportIcs() {
  try {
    await _downloadFromApi('/api/export?format=ics', `physics_study_review_${new Date().toISOString().slice(0, 10)}.ics`);
    toast('已导出复习日程 .ics');
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
    toast('已导出数据');
  } catch(e) { toast(e.message, 'error'); }
}

// ── 一键备份 / 还原（全库 JSON）──
async function exportBackup() {
  try {
    await _downloadFromApi('/api/export/backup', `physics-study-backup-${new Date().toISOString().slice(0, 10)}.json`);
    toast('已导出全量备份（含考试/知识图谱/口语/复习）');
  } catch(e) { toast(e.message, 'error'); }
}

async function importBackup(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const ok = await confirmDialog('还原将覆盖当前全部数据（现库会自动保存为 .bak 备份）。确定继续？');
    if (!ok) { input.value = ''; return; }
    const r = await api('/api/import/restore', { method: 'POST', body: { backup: text } });
    const n = Object.values(r.restored || {}).reduce((s, x) => s + x, 0);
    toast(`已还原 ${n} 条记录`);
    loadDashboard();
  } catch(e) { toast('还原失败: ' + e.message, 'error'); }
  input.value = '';
}

async function importData(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    const ok = await confirmDialog('导入将覆盖当前全部题目数据（已自动备份）。确定继续？');
    if (!ok) { input.value = ''; return; }
    const r = await api('/api/import', { method: 'POST', body: data });
    toast(`已导入 ${r.imported} 题（备份: ${r.backup.split(/[\\/]/).pop()}）`);
    loadDashboard();
  } catch(e) { toast('导入失败: ' + e.message, 'error'); }
  input.value = '';
}

// ── 公式速查 ──
const _FORMULAS = [
  {cat:'运动学',eqs:['v = v₀ + at','s = v₀t + ½at²','v² − v₀² = 2as','ω = dθ/dt']},
  {cat:'动力学',eqs:['F = ma','F_f ≤ μN','F = −kx（胡克定律）','p = mv']},
  {cat:'功与能',eqs:['W = F·s·cosθ','K = ½mv²','W = ΔK','U_g = mgh','U_e = ½kx²']},
  {cat:'动量守恒',eqs:['p_i = p_f','J = Δp = FΔt','完全弹性碰撞：v₁'+"'"+' = (m₁−m₂)/(m₁+m₂)·v₁']},
  {cat:'圆周运动',eqs:['a_c = v²/r = ω²r','F_c = mv²/r','v = ωr','T = 2π/ω']},
  {cat:'静电场',eqs:['F = kQq/r²','E = F/q','E = kQ/r²','U = Ed（匀强）']},
  {cat:'电路',eqs:['V = IR','P = IV = I²R','R_s = R₁+R₂+...','1/R_p = 1/R₁+1/R₂+...']},
  {cat:'磁场',eqs:['F = qvB·sinθ','F = ILB·sinθ','Φ = BA·cosθ','ε = −dΦ/dt']},
  {cat:'热学',eqs:['PV = nRT','ΔU = Q − W','η = 1 − T_c/T_h','ΔS = Q_rev/T']},
  {cat:'波动与光学',eqs:['v = fλ','n = c/v','n₁sinθ₁ = n₂sinθ₂','dsinθ = mλ（双缝）']},
  {cat:'SI词头',eqs:['n 10⁻⁹','μ 10⁻⁶','m 10⁻³','c 10⁻²','k 10³','M 10⁶','G 10⁹']},
];
function toggleFormulaPanel() {
  const p = document.getElementById('formulaPanel');
  const content = document.getElementById('formulaContent');
  if (p.classList.contains('hidden')) {
    content.innerHTML = _FORMULAS.map(c =>
      `<div style="margin-bottom:8px"><strong>${c.cat}</strong>: ${c.eqs.map(e=>escapeHtml(e)).join(' &nbsp;| ')}</div>`
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
  switchPage(PAGES.includes(initial) ? initial : 'dashboard');
})();
document.getElementById('searchInput').addEventListener('input', onSearchInput);
loadOcrProbe();
// C7 PWA：注册 Service Worker（仅 http/https，离线缓存静态资源）
if ('serviceWorker' in navigator && /^https?:$/.test(location.protocol)) {
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}
