// 核心：i18n / API / 弹窗 / 导航 / 主题 / 标签 / 通用工具（全局函数，按序加载）
// 个人学习 OS — 前端逻辑（抽离自 inline，便于静态语法校验）
const API = '';
const X_HEADER = 'X-Requested-With';
const X_VALUE = 'LearnOS';

// ── 导出令牌（§16.6 + R1）：同源应用每次导出/还原前 POST /api/export/challenge 取一次性挑战令牌，
//    注入 X-Export-Token。令牌 60s 内单次有效、用后即焚（防重放）；EXPORT_TOKEN 仅作服务端签名密钥，
//    永不随响应外发，故跨源恶意网页无法导出整库。每次调用都签发新令牌（不缓存）。
async function ensureExportToken() {
  try {
    const r = await fetch('/api/export/challenge', {
      method: 'POST',
      headers: { [X_HEADER]: X_VALUE },
    });
    if (!r.ok) return null;
    const d = await r.json();
    return d.token || null;
  } catch (e) {
    return null;
  }
}
const _EXPORT_PATHS = ['/api/export', '/api/import/restore'];
function _isExportPath(path) {
  const clean = String(path).split('?')[0];
  return _EXPORT_PATHS.some(p => clean === p || clean.startsWith(p + '/'));
}

// ── 多学科：当前学科上下文（URL ?subject= 优先，其次 localStorage，最后默认 physics）──
const BUILTIN_SUBJECTS = ['physics', 'chemistry', 'math'];
const BUILTIN_SUBJECT_TITLES = { physics: '物理', chemistry: '化学', math: '数学' };
function currentSubject() {
  const fromUrl = new URLSearchParams(location.search).get('subject');
  if (fromUrl) return fromUrl;
  return localStorage.getItem('subject') || 'physics';
}
function setSubject(id) {
  if (!id) return;
  localStorage.setItem('subject', id);
  const url = new URL(location.href);
  url.searchParams.set('subject', id);
  location.href = url.toString();
}
async function loadSubjectOptions(select, current) {
  let subjects = BUILTIN_SUBJECTS.map(id => ({ id, title: BUILTIN_SUBJECT_TITLES[id] || id, builtin: true }));
  try {
    const data = await api('/api/subjects');
    const list = data.subjects || [];
    if (list.length) {
      subjects = list.map(s => ({ id: s.id, title: s.title || s.id, builtin: !!s.builtin }));
    }
  } catch (e) { /* 离线时仅内置三科 */ }
  window.SUBJECT_LIST = subjects;
  if (!select) return;
  const cur = current || subjects[0].id;
  select.innerHTML = subjects.map(s =>
    `<option value="${escapeHtml(s.id)}">${escapeHtml(s.title)}</option>`
  ).join('');
  select.value = subjects.some(s => s.id === cur) ? cur : subjects[0].id;
  return select.value;
}
function withSubject(path) {
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'subject=' + encodeURIComponent(currentSubject());
}

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
  // 回收站卡片在仪表盘（与「数据备份」同页），故仅仪表盘加载
  if (page === 'page-dashboard') { loadDashboard(); loadTrash(); }
  else if (page === 'page-bank') { loadBankUnits(); loadBank(); }
  else if (page === 'page-problems') { loadProblems(1); loadUnlinked(); }
  else if (page === 'page-review') loadReviews();
  else if (page === 'page-oral') { /* 动态对话内容不翻译 */ }
  else if (page === 'page-rag') { loadMatDocs(); loadRagDocs(); loadRagStatus(); }
  else if (page === 'page-exam') loadExamPapers();
  else if (page === 'page-settings') { loadSettings(); loadPrefs(); loadFsrsStatus(); }
}

function uid() {
  return 'req-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}

// ── data-action 事件委托（渐进替换内联 onclick，最终可收紧 script-src-attr）──
// 元素写法：<button data-action="close-modal" data-modal-id="xxx"> / data-action="switch-page" data-page="bank"
document.addEventListener('click', (e) => {
  const el = e.target && e.target.closest ? e.target.closest('[data-action]') : null;
  if (!el) return;
  const action = el.dataset.action;
  if (action === 'close-modal' && el.dataset.modalId) closeModal(el.dataset.modalId);
  else if (action === 'switch-page' && el.dataset.page) switchPage(el.dataset.page);
});

// ── 全局搜索面板（Ctrl+K / Cmd+K）+ g 前缀页面快捷键 ──
let _searchTimer = null;
let _searchItems = [];   // 扁平化结果，供键盘导航
let _searchSel = -1;
let _gChordAt = 0;       // g 和弦：800ms 内按第二键跳页
const _G_PAGES = { d: 'dashboard', b: 'bank', p: 'problems', r: 'review', o: 'oral', m: 'rag', e: 'exam', h: 'help', s: 'settings' };

document.addEventListener('keydown', (e) => {
  // 输入框/文本域内不拦截
  const tag = (e.target && e.target.tagName) || '';
  const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (e.target && e.target.isContentEditable);
  if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    openModal('searchModal');
    const input = document.getElementById('searchPaletteInput');
    input.value = '';
    document.getElementById('searchResults').innerHTML = '';
    _searchItems = []; _searchSel = -1;
    setTimeout(() => input.focus(), 50);
    return;
  }
  if (e.key === 'Escape') {
    const m = document.getElementById('searchModal');
    if (m && m.classList.contains('active')) closeModal('searchModal');
    return;
  }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  // g 和弦跳页：g 然后 d/b/p/r/o/m/e/h/s
  const now = Date.now();
  if (_gChordAt && now - _gChordAt < 800) {
    const page = _G_PAGES[e.key.toLowerCase()];
    _gChordAt = 0;
    if (page) { e.preventDefault(); switchPage(page); }
    return;
  }
  if (e.key.toLowerCase() === 'g') _gChordAt = now;
});

function searchPaletteInput() {
  clearTimeout(_searchTimer);
  const q = document.getElementById('searchPaletteInput').value.trim();
  trackEvent('search.use'); // P2-5：搜索事件埋点
  if (!q) { document.getElementById('searchResults').innerHTML = ''; _searchItems = []; return; }
  _searchTimer = setTimeout(() => searchPaletteRun(q), 250);
}

async function searchPaletteRun(q) {
  try {
    const r = await api('/api/search?q=' + encodeURIComponent(q));
    const groups = [];
    if (r.problems?.length) groups.push({ label: t('search.problems'), items: r.problems.map(p => ({
      text: p.title, sub: p.topic, go: () => { closeModal('searchModal'); viewProblem(p.id); } })) });
    if (r.concepts?.length) groups.push({ label: t('search.concepts'), items: r.concepts.map(c => ({
      text: c.name, sub: '', go: () => { closeModal('searchModal');
        window.open('concept_map.html?subject=' + encodeURIComponent(currentSubject()) + '&focus=' + encodeURIComponent(c.name), '_self'); } })) });
    if (r.bank?.length) groups.push({ label: t('search.bank'), items: r.bank.map(b => {
      const _typeKey = { single: 'qtype.single', multiple: 'qtype.multiple', fill: 'qtype.fill', subjective: 'qtype.subjective', composite: 'qtype.composite' }[b.type];
      return {
        text: (_typeKey ? '【' + t(_typeKey) + '】' : '') + b.stem,
        sub: b.concept, go: () => { closeModal('searchModal'); switchPage('bank'); }
      };
    }) });
    if (r.docs?.length) groups.push({ label: t('search.docs'), items: r.docs.map(d => ({
      text: d.name + (d.page ? t('rag.pageSuffix').replace('{p}', d.page) : ''), sub: '', go: () => { closeModal('searchModal'); openRagSource(encodeURIComponent(d.path)); } })) });
    _searchItems = groups.flatMap(g => g.items);
    _searchSel = -1;
    const el = document.getElementById('searchResults');
    if (!_searchItems.length) { el.innerHTML = `<p class="text-sm text-muted">${t('search.none')}</p>`; return; }
    el.innerHTML = groups.map(g => `
      <div class="text-sm text-muted" style="margin:8px 0 4px;font-weight:600">${escapeHtml(g.label)}</div>
      ${g.items.map(it => `<div class="search-hit" data-idx="${_searchItems.indexOf(it)}" onclick="searchPaletteGo(${_searchItems.indexOf(it)})" style="padding:6px 8px;border-radius:6px;cursor:pointer">
        <div class="text-sm">${highlight(it.text, q)}</div>
        ${it.sub ? `<div class="text-sm text-muted">${escapeHtml(it.sub)}</div>` : ''}
      </div>`).join('')}`).join('');
  } catch(e) { document.getElementById('searchResults').innerHTML = `<p class="text-sm text-muted">${escapeHtml(e.message)}</p>`; }
}

function highlight(text, q) {
  const s = escapeHtml(text);
  // 切片必须用转义后的 needle 长度：原始 q 含 & < > 等时转义变长，用 q.length 会截断实体
  const needle = escapeHtml(q).toLowerCase();
  const i = s.toLowerCase().indexOf(needle);
  if (i < 0) return s;
  return s.slice(0, i) + '<mark>' + s.slice(i, i + needle.length) + '</mark>' + s.slice(i + needle.length);
}

function searchPaletteGo(idx) { const it = _searchItems[idx]; if (it) it.go(); }

function searchPaletteKeys(e) {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    if (!_searchItems.length) return;
    _searchSel = (_searchSel + (e.key === 'ArrowDown' ? 1 : -1) + _searchItems.length) % _searchItems.length;
    document.querySelectorAll('.search-hit').forEach(el => el.style.background = '');
    const el = document.querySelector(`.search-hit[data-idx="${_searchSel}"]`);
    if (el) { el.style.background = 'var(--hover,#f0f4ff)'; el.scrollIntoView({ block: 'nearest' }); }
  } else if (e.key === 'Enter') {
    if (_searchSel >= 0) searchPaletteGo(_searchSel);
  }
}

async function api(path, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  const headers = { 'Content-Type': 'application/json', [X_HEADER]: X_VALUE };
  // 写操作携带幂等键，避免网络重试产生重复数据
  if (method !== 'GET') headers['X-Request-Id'] = uid();
  // 导出/还原端点注入导出令牌（§16.6，令牌取自 /api/bootstrap）
  if (_isExportPath(path)) {
    const tok = await ensureExportToken();
    if (tok) headers['X-Export-Token'] = tok;
  }
  const body = opts.body ? JSON.stringify(opts.body) : undefined;
  const url = withSubject(path);

  let fetchFailed = false;
  // no-store：绕开 HTTP 缓存与 SW Cache 存写的互锁路径，API 必须实时
  const doFetch = () => fetch(API + url, { method, headers, body, cache: 'no-store', signal: opts.signal })
    .catch(e => { fetchFailed = true; throw e; });
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
    // 网络层异常（断网/后端不可达等）：转换为本地化友好文案，避免裸露原始 TypeError
    if (err instanceof TypeError && fetchFailed) throw new Error(t('msg.networkError'));
    throw err;
  }
}

// 按钮锁：网络往返期间置灰防止重复触发，结束后恢复原状态
async function withButtonLock(btn, fn) {
  if (!btn) return fn();
  const prev = btn.disabled; btn.disabled = true;
  try { return await fn(); } finally { btn.disabled = prev; }
}

// ── P2-5 轻量本地事件埋点（零第三方依赖；供 N3 复盘关键 UX 指标）──
const _TELE_KEY = 'learnos.telemetry';
function trackEvent(name) {
  try {
    const tele = JSON.parse(localStorage.getItem(_TELE_KEY) || '{}');
    tele[name] = (tele[name] || 0) + 1;
    localStorage.setItem(_TELE_KEY, JSON.stringify(tele));
  } catch (e) { /* 存储异常，静默 */ }
}
function telemetryReport() {
  return JSON.parse(localStorage.getItem(_TELE_KEY) || '{}');
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

// 错因枚举 → 本地化显示；旧中文/未知值原样返回（兼容存量数据）
function errLabel(et) {
  const map = {
    '待诊断': 'errType.diagnose',
    concept_misunderstood: 'errType.concept',
    calculation: 'errType.calculation',
    careless: 'errType.careless',
    time_pressure: 'errType.timePressure',
    misread: 'errType.misread',
    blank_in_facts: 'errType.blankFacts',
    heuristic_trap: 'errType.heuristicTrap',
  };
  const key = map[et];
  return key ? t(key) : et;
}

function escapeHtml(s) {
  // 字符替换式转义：textContent 序列化不转义引号，放进属性/onclick 单引号上下文会被逃逸
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// C7：localStorage 安全收口——隐私模式/配额满/脏 JSON 均兜底，不再打断主流程
const store = {
  get(k, fb) {
    try { const v = localStorage.getItem(k); return v === null ? fb : v; } catch (_) { return fb; }
  },
  getJSON(k, fb) {
    try {
      const v = localStorage.getItem(k);
      if (v === null) return fb;
      const parsed = JSON.parse(v);
      return parsed === null || parsed === undefined ? fb : parsed;
    } catch (_) {
      try { localStorage.removeItem(k); } catch (_) {}  // 脏数据直接清掉
      return fb;
    }
  },
  set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
};

let _katexLoading = null;
function loadKatex() {
  if (window.renderMathInElement) return Promise.resolve();
  if (_katexLoading) return _katexLoading;
  _katexLoading = new Promise((resolve, reject) => {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/vendor/katex.min.css';
    document.head.appendChild(css);
    const s = document.createElement('script');
    s.src = '/vendor/katex.min.js';
    s.onload = () => {
      const a = document.createElement('script');
      a.src = '/vendor/auto-render.min.js';
      a.onload = resolve;
      a.onerror = reject;
      document.head.appendChild(a);
    };
    s.onerror = reject;
    document.head.appendChild(s);
  });
  return _katexLoading;
}

function renderMath(el) {
  const target = el || document.body;
  if (window.renderMathInElement) {
    renderMathInElement(target, {
      delimiters: [
        {left: '$$', right: '$$', display: true},
        {left: '$', right: '$', display: false},
        {left: '\\[', right: '\\]', display: true},
        {left: '\\(', right: '\\)', display: false},
      ],
      throwOnError: false,
    });
    return;
  }
  loadKatex().then(() => {
    if (window.renderMathInElement) renderMathInElement(target, {
      delimiters: [
        {left: '$$', right: '$$', display: true},
        {left: '$', right: '$', display: false},
        {left: '\\[', right: '\\]', display: true},
        {left: '\\(', right: '\\)', display: false},
      ],
      throwOnError: false,
    });
  }).catch(() => {});
}

// ── 弹窗：焦点陷阱 + Esc/遮罩关闭 ──
let _lastFocus = null;
// 编辑弹窗未保存守卫：基于「打开时快照 vs 当前值」对比判定 dirty，覆盖字段/标签/星标/媒体变化
let _editModalLast = null;
function editSnapshot() {
  const vals = ['editTitle','editCourse','editTopic','editContent','editAttempt','editErrorType','editMastery','editMediaPath']
    .map(i => { const el = document.getElementById(i); return el ? el.value : ''; });
  const star = document.getElementById('editStarred');
  return JSON.stringify([vals, star ? (star.checked ? 1 : 0) : 0, (currentTags || []).map(tg => tg.text)]);
}
function editDirty() { return _editModalLast !== null && editSnapshot() !== _editModalLast; }
// 草稿自动保存（P1-c2，nudge 优于硬拦截）：关闭/刷新时自动写 localStorage（key 限学科），重开新建弹窗时回填
const _draftKey = () => 'learnos.draft.' + (currentSubject() || 'physics');
function saveDraft() {
  try {
    localStorage.setItem(_draftKey(), JSON.stringify({
      title: document.getElementById('editTitle').value,
      course: document.getElementById('editCourse').value,
      topic: document.getElementById('editTopic').value,
      content: document.getElementById('editContent').value,
      my_attempt: document.getElementById('editAttempt').value,
      error_type: document.getElementById('editErrorType').value,
      mastery: document.getElementById('editMastery').value,
      starred: document.getElementById('editStarred').checked ? 1 : 0,
      tags: (currentTags || []).map(tg => tg.text),
      media_path: document.getElementById('editMediaPath').value,
    }));
  } catch (e) { /* 存储满/隐私模式，静默 */ }
}
function loadDraft() {
  try { return JSON.parse(localStorage.getItem(_draftKey()) || 'null'); } catch (e) { return null; }
}
function clearDraft() {
  try { localStorage.removeItem(_draftKey()); } catch (e) { /* 静默 */ }
}
function autoSaveInterval() {
  if (!document.getElementById('editModal').classList.contains('active')) return;
  if (editDirty()) saveDraft(); // 静默自动保存，不留提示避免打断
}
window.addEventListener('beforeunload', () => {
  // 草稿已保全数据，无需浏览器原生拦截弹窗
  if (editDirty()) saveDraft();
});
function openModal(id) {
  const overlay = document.getElementById(id);
  if (!overlay) return;
  _lastFocus = document.activeElement;
  overlay.classList.add('active');
  // P2-1：弹窗开启时锁 body 滚动 + 主内容 inert（屏幕阅读器不穿透背景）
  document.body.style.overflow = 'hidden';
  const main = document.querySelector('.container');
  if (main && !main.hasAttribute('inert')) main.setAttribute('inert', '');
  const focusables = overlay.querySelectorAll('input, textarea, select, button, [tabindex]');
  if (focusables.length) focusables[0].focus();
}

function unlockOverlay() {
  // 仅当没有任何弹窗处于开启状态时还原（多弹窗叠层时最上层关闭不还原）
  if (!document.querySelector('.modal-overlay.active')) {
    document.body.style.overflow = '';
    const main = document.querySelector('.container');
    if (main) main.removeAttribute('inert');
  }
}

// 每 30s 自动保存一次当前编辑草稿（P1-c2 防数据丢失）
setInterval(autoSaveInterval, 30000);
// P2-3：在飞 AI 请求可取消（LAN 下高延迟重评/口试场景）——复用 P1-g 的 api() signal 支持
// 按弹窗归组：弹窗关闭即 abort 其内未完成的 AI fetch，避免请求堆积、串台或后台空跑
const _aiAborts = new Map(); // modalId -> Set<AbortController>
function trackModalAI(modalId) {
  const ac = new AbortController();
  if (!_aiAborts.has(modalId)) _aiAborts.set(modalId, new Set());
  const set = _aiAborts.get(modalId);
  set.add(ac);
  ac.signal.addEventListener('abort', () => { set.delete(ac); if (!set.size) _aiAborts.delete(modalId); });
  return ac.signal;
}
function abortModalAI(modalId) {
  const set = _aiAborts.get(modalId);
  if (!set) return;
  set.forEach(ac => { try { ac.abort(); } catch (e) {} });
  set.clear();
  _aiAborts.delete(modalId);
}
function closeModal(id) {
  const overlay = document.getElementById(id);
  if (!overlay) return;
  // 编辑弹窗未保存守卫（P1-c2）：dirty 时自动存草稿后关闭，无需 confirm（nudge 优于硬拦截）
  if (id === 'editModal' && editDirty()) {
    saveDraft();
    _editModalLast = null; // 已入草稿，此后 closeModalDirect 不再重入
  }
  closeModalDirect(id);
}
function closeModalDirect(id) {
  const overlay = document.getElementById(id);
  if (!overlay) return;
  abortModalAI(id); // P2-3：弹窗关闭即取消其中未完成的 AI fetch
  overlay.classList.remove('active');
  unlockOverlay();
  if (_lastFocus && _lastFocus.focus) _lastFocus.focus();
  // 清理弹窗的键盘快捷键监听。
  // 旧实现依赖 overlay._onKey 句柄，但注册端每次都覆盖该句柄，导致旧监听器无法移除、
  // 连开 N 题即累积 N 个（按 1/2/3/4 会向多个旧题目并发发请求）。
  // 改用 AbortController：注册与注销共用同一个 signal，物理上不可能失同步。
  if (overlay._keyAc) {
    overlay._keyAc.abort();
    overlay._keyAc = null;
  }
  overlay._onKey = null;
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
  const signal = trackModalAI('editModal'); // P2-3：弹窗关闭可取消
  try {
    const data = await api('/api/ai/extract-tags', {
      method: 'POST', signal,
      body: {
        title: document.getElementById('editTitle').value,
        content: document.getElementById('editContent').value,
        course: document.getElementById('editCourse').value,
        topic: document.getElementById('editTopic').value,
      },
    });
    currentTags = (data.tags || []).map(tg => ({
      text: String(tg),
      pending: data.confidence < 0.9 || data.pending === true,
    }));
    renderTags();
    const source = data.source === 'ai' ? 'AI' : t('tag.sourceRule');
    const conf = Math.round((data.confidence || 0) * 100);
    document.getElementById('editTagsHint').textContent =
      t('tag.extractResult').replace('{s}', source).replace('{c}', conf).replace('{d}', data.source !== 'ai' ? t('tag.degraded') : '') + '。';
  } catch(e) {
    if (e.name !== 'AbortError') toast(e.message, 'error'); // P2-3：取消不报错
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
const PAGES = ['dashboard', 'bank', 'problems', 'review', 'oral', 'cards', 'rag', 'exam', 'help', 'settings'];
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
  if (page === 'dashboard') { loadDashboard(); loadLearningPath('lpContent'); loadTrash(); }
  if (page === 'bank') { loadBankUnits(); loadBank(); }
  if (page === 'problems') loadProblems(1);
  if (page === 'review') loadReviews();
  if (page === 'cards') {
    loadCards(); loadLearningPath();
    const _pc = window.__pendingCardConcept;
    if (_pc) { window.__pendingCardConcept = null; makeCardForPath(_pc); }
  }
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
  el.addEventListener('click', () => {
    if (el.classList.contains('nav-more-toggle')) return;
    switchPage(el.dataset.page);
  });
});
document.addEventListener('click', (e) => {
  const more = document.getElementById('moreToggle');
  const menu = more && more.nextElementSibling;
  if (!more || !menu) return;
  const inMenu = more.contains(e.target) || menu.contains(e.target);
  if (!inMenu) menu.classList.remove('open');
});
const _moreToggle = document.getElementById('moreToggle');
if (_moreToggle) {
  _moreToggle.addEventListener('click', (e) => {
    e.stopPropagation();
    const menu = _moreToggle.nextElementSibling;
    const open = menu.classList.toggle('open');
    _moreToggle.setAttribute('aria-expanded', String(open));
  });
}
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
    return;
  }
  if (e.key === 'n' || e.key === 'N') {
    switchPage('problems');
    openProblemModal();
  } else if (e.key === 'g' || e.key === 'G') {
    window.location.href = 'concept_map.html';
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

