// 个人物理学习 OS — 前端逻辑（抽离自 inline，便于静态语法校验）
const API = '';
const X_HEADER = 'X-Requested-With';
const X_VALUE = 'PhysicsStudyOS';

function uid() {
  return 'req-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}

async function api(path, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  const headers = { 'Content-Type': 'application/json', [X_HEADER]: X_VALUE };
  // 写操作携带幂等键，避免网络重试产生重复数据
  if (method !== 'GET') headers['X-Request-Id'] = uid();
  const body = opts.body ? JSON.stringify(opts.body) : undefined;

  const doFetch = () => fetch(API + path, { method, headers, body });
  try {
    const res = await doFetch();
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || '请求失败');
    return data;
  } catch (err) {
    // 仅对网络层失败（非 HTTP 错误）重试一次（GET）
    if (err instanceof TypeError && method === 'GET') {
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
    document.getElementById('confirmMsg').textContent = message;
    openModal('confirmModal');
    const ok = document.getElementById('confirmOk');
    const cancel = document.getElementById('confirmCancel');
    const cleanup = () => {
      ok.removeEventListener('click', onOk);
      cancel.removeEventListener('click', onCancel);
      closeModal('confirmModal');
    };
    const onOk = () => { cleanup(); resolve(true); };
    const onCancel = () => { cleanup(); resolve(false); };
    ok.addEventListener('click', onOk);
    cancel.addEventListener('click', onCancel);
  });
}

// ── 导航 + 深链 ──
const PAGES = ['dashboard', 'problems', 'review', 'oral', 'settings'];
function switchPage(page) {
  if (!PAGES.includes(page)) page = 'dashboard';
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === page));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + page).classList.add('active');
  if (location.hash !== '#' + page) history.replaceState(null, '', '#' + page);
  if (page === 'dashboard') loadDashboard();
  if (page === 'problems') loadProblems(1);
  if (page === 'review') loadReviews();
  if (page === 'settings') loadSettings();
}
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
  if (theme === 'dark') root.style.colorScheme = 'dark';
  else if (theme === 'light') root.style.colorScheme = 'light';
  else root.style.colorScheme = '';
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
  el.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const d = await api('/api/dashboard');
    document.getElementById('statTotal').textContent = d.stats.total || 0;
    document.getElementById('statDue').textContent = d.due || 0;
    document.getElementById('statMastered').textContent = d.stats.mastered || 0;
    document.getElementById('statAvg').textContent = (d.stats.avg_mastery || 0).toFixed(1);

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
      el.innerHTML = '<div class="empty"><p>暂无知识点数据</p></div>';
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
    drawTrend();
  } catch(e) { toast(e.message, 'error'); }
}

async function drawTrend() {
  const svg = document.getElementById('trendSvg');
  const hint = document.getElementById('trendHint');
  try {
    const log = await api('/api/trend');
    if (!log.length) { svg.innerHTML = ''; return; }
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
    hint.textContent = `最近 ${n} 次复习 · 当前均值 ${last.avg_mastery}`;
  } catch(e) { /* 趋势可选，失败不阻塞 */ }
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
  const params = new URLSearchParams({ page, limit: 20, q });
  try {
    const data = await api(`/api/problems?${params.toString()}`);
    problemPages = data.pages || 1;
    const items = data.items || data;
    if (!items.length) {
      listEl.innerHTML = '<div class="empty"><p>暂无题目，点击"新增题目"开始</p></div>';
    } else {
      let sorted = items.slice();
      if (sort === 'mastery') sorted.sort((a, b) => a.mastery - b.mastery);
      listEl.innerHTML = sorted.map(p => `
        <div class="list-item" onclick="viewProblem(${p.id})">
          <div class="list-item-header">
            <span class="list-item-title">${escapeHtml(p.title)}</span>
            ${masteryTag(p.mastery)}
          </div>
          <div class="list-item-meta">${escapeHtml(p.course)} · ${escapeHtml(p.topic)} · ${escapeHtml(p.error_type)} · ${escapeHtml(p.created_at)}</div>
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
    document.getElementById('modalTitle').textContent = p.title;
    let html = `
      <div class="flex gap-8 mb-8">
        <span class="tag tag-blue">${escapeHtml(p.course || '未分类')}</span>
        <span class="tag tag-gray">${escapeHtml(p.topic || '无知识点')}</span>
        ${masteryTag(p.mastery)}
      </div>
      <div class="card" style="border-color:var(--border);margin-bottom:12px">
        <div class="card-title">题目内容</div>
        <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(p.content)}</p>
      </div>`;
    if (p.my_attempt) {
      html += `<div class="card" style="border-color:var(--border);margin-bottom:12px">
        <div class="card-title">我的尝试</div>
        <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(p.my_attempt)}</p>
      </div>`;
    }
    html += `<div class="card-title mt-16">分级提示</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},1)" id="hint1btn">一级提示</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},2)" id="hint2btn">二级提示</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},3)" id="hint3btn">三级提示</button>
      </div>
      <div id="hintsArea"></div>`;
    if (p.hints && p.hints.length) {
      p.hints.forEach(h => {
        html += `<div class="hint-card"><h4>第 ${h.level} 级提示</h4><p>${escapeHtml(h.content)}</p></div>`;
      });
    }
    html += `<div class="flex gap-12 mt-16">
      <button class="btn btn-secondary btn-sm" onclick="editProblem(${id})">编辑</button>
      <button class="btn btn-danger btn-sm" onclick="deleteProblem(${id})">删除</button>
    </div>`;
    document.getElementById('modalBody').innerHTML = html;
    renderMath(document.getElementById('modalBody'));
    openModal('problemModal');
  } catch(e) { toast(e.message, 'error'); }
}

async function getHint(id, level) {
  const btn = document.getElementById(`hint${level}btn`);
  btn.disabled = true; btn.textContent = '加载中...';
  try {
    const r = await api(`/api/problems/${id}/hint`, { method: 'POST', body: { level } });
    const area = document.getElementById('hintsArea');
    const srcTag = r.source === 'ai' ? '<span class="tag tag-green">AI</span>' :
                   r.source === 'fallback' ? '<span class="tag tag-amber">降级</span>' : '<span class="tag tag-gray">缓存</span>';
    area.innerHTML += `<div class="hint-card"><h4>第 ${level} 级提示 ${srcTag}</h4><p>${escapeHtml(r.content)}</p></div>`;
    renderMath(area);
    btn.textContent = '已查看'; btn.style.opacity = '0.5';
  } catch(e) {
    toast(e.message, 'error'); btn.disabled = false; btn.textContent = `第${'一二三'[level - 1]}级提示`;
  }
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
    } catch(e) { toast(e.message, 'error'); return; }
  } else {
    titleEl.textContent = '新增题目';
    document.getElementById('editId').value = '';
    ['editTitle','editCourse','editTopic','editContent','editAttempt'].forEach(i => document.getElementById(i).value = '');
    document.getElementById('editErrorType').value = '待诊断';
    document.getElementById('editMastery').value = 1;
  }
  openModal('editModal');
}

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
  };
  if (!body.title.trim() || !body.content.trim()) { toast('标题和题目内容不能为空', 'error'); return; }
  try {
    if (id) {
      await api(`/api/problems/${id}`, { method: 'PUT', body });
    } else {
      await api('/api/problems', { method: 'POST', body });
    }
    toast(id ? '已更新' : '已创建');
    closeModal('editModal');
    loadProblems(problemPage);
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteProblem(id) {
  const ok = await confirmDialog('确定删除这道题目？相关提示和复习记录也会被删除。');
  if (!ok) return;
  try {
    await api(`/api/problems/${id}`, { method: 'DELETE' });
    toast('已删除');
    closeModal('problemModal');
    loadProblems(problemPage);
  } catch(e) { toast(e.message, 'error'); }
}

// ── 复习 ──
async function loadReviews() {
  const el = document.getElementById('reviewList');
  el.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const list = await api('/api/reviews');
    if (!list.length) {
      el.innerHTML = '<div class="empty"><p>今天没有待复习的题目，去看看概览页吧</p></div>';
      return;
    }
    const today = new Date().toISOString().slice(0, 10);
    el.innerHTML = list.map(r => `
      <div class="list-item">
        <div class="list-item-header">
          <span class="list-item-title">${escapeHtml(r.title)}</span>
          <span class="tag ${r.due_date <= today ? 'tag-red' : 'tag-gray'}">
            ${r.due_date <= today ? '今日到期' : '即将到期'}
          </span>
        </div>
        <div class="list-item-meta">${escapeHtml(r.course)} · ${escapeHtml(r.topic)} · 到期日: ${r.due_date}</div>
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

// ── 口试 ──
let oralSessionId = null;
let oralTurn = 0;

async function startOral() {
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
      document.getElementById('oralTurn').textContent = '口试已结束';
      document.getElementById('oralAnswer').placeholder = '口试已结束，可重新开始';
    } else {
      document.getElementById('oralTurn').textContent = `第 ${oralTurn} / 5 轮`;
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

// ── 设置 ──
async function loadSettings() {
  try {
    const s = await api('/api/settings');
    document.getElementById('setApiBase').value = s.api_base || '';
    document.getElementById('setApiKey').value = '';
    document.getElementById('setApiKey').placeholder = s.has_api_key ? '••••••••（已配置，留空则不变）' : 'sk-...';
    document.getElementById('setModel').value = s.model || '';
    document.getElementById('setTemp').value = s.temperature || '0.3';
    const srcLabel = {
      environment: '当前使用环境变量中的密钥（优先级最高，不写入数据库）',
      runtime: '密钥已录入本次运行的内存（重启后失效，不写入数据库）',
      local: '密钥已配置（存储在本地数据库）',
      none: '尚未配置密钥',
    };
    document.getElementById('keyStatus').textContent = srcLabel[s.key_source] || srcLabel.none;
  } catch(e) { toast(e.message, 'error'); }
}

async function saveSettings() {
  const body = {
    api_base: document.getElementById('setApiBase').value,
    model: document.getElementById('setModel').value,
    temperature: document.getElementById('setTemp').value,
  };
  const key = document.getElementById('setApiKey').value;
  if (key) body.api_key = key;
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

// ── 初始化 ──
const initial = (location.hash || '').replace('#', '');
switchPage(PAGES.includes(initial) ? initial : 'dashboard');
document.getElementById('searchInput').addEventListener('input', onSearchInput);
