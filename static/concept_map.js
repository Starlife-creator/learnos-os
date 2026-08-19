// ── F2 双语：静态独立页轻度 i18n ──
const GRAPH_LOCALES = ['zh-CN', 'en-US'];
let _dict = {};
function _curLang() {
  const s = localStorage.getItem('lang');
  if (GRAPH_LOCALES.includes(s)) return s;
  const nav = (navigator.language || '').toLowerCase();
  if (nav.startsWith('en')) return 'en-US';
  return 'zh-CN';
}
function t(k, fb) { return _dict[k] || fb || k; }
function applyGraphI18n(root) {
  const scope = root || document;
  scope.querySelectorAll('[data-i18n]').forEach(el => {
    const text = t(el.getAttribute('data-i18n'));
    if (String(text)) el.textContent = text;
  });
  scope.querySelectorAll('[data-i18n-ph]').forEach(el => {
    el.setAttribute('placeholder', t(el.getAttribute('data-i18n-ph')));
  });
  if (scope === document) document.title = t('graph.title') + ' - Physics';
}
async function bootGraph() {
  const lang = _curLang();
  try {
    _dict = await (await fetch(`/locale/${lang}.json`, { cache: 'no-cache' })).json();
  } catch { _dict = {}; }
  applyGraphI18n(document);
  await initGraphSubject();
  loadGraph();
}
const svg = document.getElementById('svg');
const NS = 'http://www.w3.org/2000/svg';
let graphData = null;
let selectedId = null;
let positions = new Map();        // id -> {x, y}
let nodeEls = new Map();          // id -> {hit, circle, label}
let edges = [];                   // {a, b, path}
let edgeByNode = new Map();       // id -> [edge indices]
let descendants = new Map();      // id -> [ids]
let expanded = new Set();         // 已展开的章节 id
let view = { x: 40, y: 30, scale: 1 };
const SAVE_KEY = 'conceptMapPositions.v1';

function make(tag, attrs, text) {
  const el = document.createElementNS(NS, tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  if (text !== undefined) el.textContent = text;
  return el;
}

function nodeColor(m) {
  if (!m || m <= 0.02) return '#cbd5e1';
  if (m >= 0.6) return '#22c55e';
  if (m >= 0.35) return '#f59e0b';
  return '#ef4444';
}

function unitColor(idx) {
  const colors = getComputedStyle(document.documentElement).getPropertyValue('--unit-colors').split(',').map(s => s.trim());
  return colors[idx % colors.length] || '#6366f1';
}

function visibleNodeIds() {
  const vis = new Set(graphData.nodes.filter(n => n.level === 0).map(u => u.id));
  for (const c of graphData.nodes.filter(n => n.level === 1)) {
    vis.add(c.id);
    if (expanded.has(c.id)) {
      graphData.nodes.filter(x => x.parent_id === c.id).forEach(x => vis.add(x.id));
    }
  }
  return vis;
}

// ── 布局：单元分列，章节堆叠，展开的章节在下方平铺概念 ──
function layoutNodes(nodes) {
  const units = nodes.filter(n => n.level === 0);
  const chapters = nodes.filter(n => n.level === 1);
  const concepts = nodes.filter(n => n.level === 2);
  const pos = new Map();
  const TOP = 46, ROW_H = 46, COL_W = 118, CH_GAP = 26, UNIT_GAP = 90;
  let x = 60;
  units.forEach(u => {
    const chs = chapters.filter(c => c.parent_id === u.id);
    let y = TOP + 40;
    let colW = 0;
    for (const ch of chs) {
      const kids = expanded.has(ch.id) ? concepts.filter(c => c.parent_id === ch.id) : [];
      const rows = Math.max(1, Math.ceil(kids.length / 5));
      const cols = Math.min(5, Math.max(1, kids.length));
      const startKX = x + 190;
      const startKY = y + 52;
      const conceptW = kids.length ? 190 + (cols - 1) * COL_W : 0;
      pos.set(ch.id, { x: x + 90, y: y + 16 });
      kids.forEach((k, i) => {
        const r = Math.floor(i / cols), c = i % cols;
        pos.set(k.id, { x: startKX + c * COL_W, y: startKY + r * ROW_H });
      });
      colW = Math.max(colW, conceptW);
      y += (kids.length ? 52 + rows * ROW_H : 40) + CH_GAP;
    }
    pos.set(u.id, { x: x + colW / 2, y: TOP });
    x += colW + UNIT_GAP;
  });
  return pos;
}

function loadSavedPositions() {
  try {
    const raw = localStorage.getItem(SAVE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch { return {}; }
}
function savePositions() {
  const obj = {};
  positions.forEach((p, id) => { obj[id] = { x: Math.round(p.x), y: Math.round(p.y) }; });
  try { localStorage.setItem(SAVE_KEY, JSON.stringify(obj)); } catch {}
}

// ── 绘制 ──
function draw(useSaved = true) {
  const saved = useSaved ? loadSavedPositions() : {};
  const auto = layoutNodes(graphData.nodes);
  const vis = visibleNodeIds();
  positions = new Map();
  for (const n of graphData.nodes) {
    if (!vis.has(n.id)) continue;
    const p = (saved[n.id] && isFinite(saved[n.id].x)) ? saved[n.id] : auto.get(n.id);
    if (p) positions.set(n.id, { x: p.x, y: p.y });
  }

  svg.innerHTML = '';
  nodeEls = new Map();
  descendants = new Map();
  const gMain = make('g');
  svg.appendChild(gMain);
  const gEdges = make('g');
  const gNodes = make('g');
  gMain.appendChild(gEdges);
  gMain.appendChild(gNodes);

  // 后代关系（拖单元/章节时子节点跟随）
  const byParent = new Map();
  for (const n of graphData.nodes) {
    if (n.parent_id && vis.has(n.parent_id) && vis.has(n.id)) {
      if (!byParent.has(n.parent_id)) byParent.set(n.parent_id, []);
      byParent.get(n.parent_id).push(n.id);
    }
  }
  const computeDesc = (id) => {
    const kids = byParent.get(id) || [];
    const out = [];
    for (const k of kids) { out.push(k, ...computeDesc(k)); }
    return out;
  };
  for (const id of vis) descendants.set(id, computeDesc(id));

  // 边（曲线，只画两端可见的）
  edges = [];
  edgeByNode = new Map();
  for (const l of graphData.links) {
    if (!positions.has(l.concept_a) || !positions.has(l.concept_b)) continue;
    const a = positions.get(l.concept_a), b = positions.get(l.concept_b);
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const path = make('path', {
      d: `M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}`,
      class: 'edge edge-' + l.relation, 'stroke-width': 1.2,
    });
    gEdges.appendChild(path);
    const idx = edges.length;
    edges.push({ a: l.concept_a, b: l.concept_b, path });
    if (!edgeByNode.has(l.concept_a)) edgeByNode.set(l.concept_a, []);
    if (!edgeByNode.has(l.concept_b)) edgeByNode.set(l.concept_b, []);
    edgeByNode.get(l.concept_a).push(idx);
    edgeByNode.get(l.concept_b).push(idx);
  }

  // 节点
  const units = graphData.nodes.filter(n => n.level === 0);
  const unitIdx = new Map(units.map((u, i) => [u.id, i]));
  for (const n of graphData.nodes) {
    if (!positions.has(n.id)) continue;
    const p = positions.get(n.id);
    const col = n.level === 0 ? unitColor(unitIdx.get(n.id) || 0)
      : n.level === 1 ? '#94a3b8'
      : nodeColor(n.mastery_est || 0);
    const r = n.level === 0 ? 26 : n.level === 1 ? 15 : 12;
    const group = make('g', { 'data-id': n.id });
    group.style.cursor = 'move';
    const hit = make('circle', {
      cx: p.x, cy: p.y, r: Math.max(14 / view.scale, r + 10), class: 'node-hit',
    });
    group.appendChild(hit);
    const circle = make('circle', {
      cx: p.x, cy: p.y, r: r,
      class: 'node-' + (n.level === 0 ? 'unit' : n.level === 1 ? 'chapter' : 'concept'),
      fill: col, 'fill-opacity': n.level === 0 ? 0.18 : 0.9,
      'stroke-width': n.level === 0 ? 2.4 : n.level === 1 ? 2 : 1.6,
      'data-id': n.id, 'pointer-events': 'all',
    });
    group.appendChild(circle);
    group.addEventListener('click', ev => {
      ev.stopPropagation();
      if (justDragged) { justDragged = false; return; }
      if (n.level === 1) toggleChapter(n.id);
      selectNode(n.id);
    });
    gNodes.appendChild(group);
    const labelCls = n.level === 0 ? 'label label-unit' : n.level === 1 ? 'label label-chapter' : 'label';
    const labelDy = n.level === 0 ? -38 : n.level === 1 ? 32 : 24;
    const suffix = n.level === 1 ? (expanded.has(n.id) ? ' ▼' : ' ▶') : '';
    const label = make('text', {
      x: p.x, y: p.y + labelDy, 'text-anchor': 'middle', class: labelCls,
    }, n.name + suffix);
    gNodes.appendChild(label);
    nodeEls.set(n.id, { hit, circle, label });
  }
  applyView();
}

function applyView() {
  const g = svg.firstElementChild;
  if (!g) return;
  g.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.scale})`);
}

function updateNode(id) {
  const p = positions.get(id);
  const el = nodeEls.get(id);
  if (!p || !el) return;
  el.circle.setAttribute('cx', p.x);
  el.circle.setAttribute('cy', p.y);
  el.hit.setAttribute('cx', p.x);
  el.hit.setAttribute('cy', p.y);
  el.label.setAttribute('x', p.x);
  const n = graphData.nodes.find(n => n.id === id);
  el.label.setAttribute('y', p.y + (n && n.level === 0 ? -38 : n && n.level === 1 ? 32 : 24));
  const ids = edgeByNode.get(id);
  if (ids) for (const i of ids) rebindEdge(i);
}

function refreshHitRadii() {
  for (const el of nodeEls.values()) {
    const r = parseFloat(el.circle.getAttribute('r'));
    el.hit.setAttribute('r', Math.max(14 / view.scale, r + 10));
  }
}

function collapseAll() { expanded.clear(); redrawAndFit(); }
function expandAll() {
  graphData.nodes.filter(n => n.level === 1).forEach(c => expanded.add(c.id));
  redrawAndFit();
}
function toggleChapter(id) {
  if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
  redrawAndFit();
}
function redrawAndFit() {
  draw();
  requestAnimationFrame(fitToView);
  savePositions();
}

function rebindEdge(i) {
  const e = edges[i];
  if (!e) return;
  const a = positions.get(e.a), b = positions.get(e.b);
  if (!a || !b) return;
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  e.path.setAttribute('d', `M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}`);
}

function fitToView() {
  if (!positions.size) return;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  positions.forEach(p => {
    minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
    minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
  });
  const w = svg.clientWidth || 800, h = svg.clientHeight || 600;
  const pad = 80;
  const bw = maxX - minX + pad * 2, bh = maxY - minY + pad * 2;
  view.scale = Math.max(0.35, Math.min(w / bw, h / bh, 1.1));
  view.x = w / 2 - ((minX + maxX) / 2) * view.scale;
  view.y = h / 2 - ((minY + maxY) / 2) * view.scale;
  applyView();
}

function autoLayout() {
  draw(false);
  fitToView();
  savePositions();
  toast(t('graph.autoLayout') + ' ✓');
}
function resetLayout() {
  try { localStorage.removeItem(SAVE_KEY); } catch {}
  autoLayout();
}
function zoomBy(factor) {
  const rect = svg.getBoundingClientRect();
  const px = rect.width / 2, py = rect.height / 2;
  const newScale = Math.min(3, Math.max(0.35, view.scale * factor));
  const wx = (px - view.x) / view.scale, wy = (py - view.y) / view.scale;
  view.scale = newScale;
  view.x = px - wx * view.scale;
  view.y = py - wy * view.scale;
  applyView();
  refreshHitRadii();
}

// ── 交互：缩放 / 平移 / 拖动节点（Pointer Events，兼容鼠标与触屏）──
svg.addEventListener('wheel', e => {
  e.preventDefault();
  const rect = svg.getBoundingClientRect();
  const px = e.clientX - rect.left, py = e.clientY - rect.top;
  const factor = e.deltaY > 0 ? 0.9 : 1.1;
  const newScale = Math.min(3, Math.max(0.35, view.scale * factor));
  const wx = (px - view.x) / view.scale, wy = (py - view.y) / view.scale;
  view.scale = newScale;
  view.x = px - wx * view.scale;
  view.y = py - wy * view.scale;
  applyView();
  refreshHitRadii();
}, { passive: false });

let dragNode = null;
let dragStart = null;
let justDragged = false;
svg.addEventListener('pointerdown', e => {
  const g = e.target.closest ? e.target.closest('g[data-id]') : null;
  if (g) {
    const raw = g.getAttribute('data-id');
    const node = graphData.nodes.find(n => String(n.id) === raw);
    if (!node) return;
    dragNode = { id: node.id, sx: e.clientX, sy: e.clientY, moved: false };
    e.preventDefault();
    return;
  }
  dragStart = { x: e.clientX, y: e.clientY };
  svg.classList.add('dragging');
});
window.addEventListener('pointermove', e => {
  if (dragNode) {
    const p = positions.get(dragNode.id);
    if (!p) return;
    const dx = e.clientX - dragNode.sx, dy = e.clientY - dragNode.sy;
    if (!dragNode.moved && Math.abs(dx) + Math.abs(dy) > 4) dragNode.moved = true;
    const stepX = dx / view.scale, stepY = dy / view.scale;
    p.x += stepX;
    p.y += stepY;
    const desc = descendants.get(dragNode.id);
    if (desc) for (const d of desc) {
      const q = positions.get(d);
      if (!q) continue;
      q.x += stepX; q.y += stepY;
    }
    dragNode.sx = e.clientX; dragNode.sy = e.clientY;
    updateNode(dragNode.id);
    if (desc) for (const d of desc) updateNode(d);
    return;
  }
  if (!dragStart) return;
  view.x += e.clientX - dragStart.x;
  view.y += e.clientY - dragStart.y;
  dragStart = { x: e.clientX, y: e.clientY };
  applyView();
});
window.addEventListener('pointerup', () => {
  if (dragNode) {
    justDragged = dragNode.moved;
    savePositions();
    dragNode = null;
  }
  dragStart = null;
  svg.classList.remove('dragging');
});

// ── 搜索高亮（命中概念自动展开其章节）──
function applySearch(q) {
  const kw = (q || '').trim().toLowerCase();
  if (kw) {
    let needRedraw = false;
    for (const n of graphData.nodes) {
      if (n.level === 2 && n.name.toLowerCase().includes(kw)) {
        const ch = graphData.nodes.find(x => x.id === n.parent_id);
        if (ch && !expanded.has(ch.id)) { expanded.add(ch.id); needRedraw = true; }
      }
    }
    if (needRedraw) { draw(); requestAnimationFrame(fitToView); }
  }
  for (const n of graphData.nodes) {
    const el = nodeEls.get(n.id);
    if (!el) continue;
    const hit = kw && n.name.toLowerCase().includes(kw);
    el.circle.classList.toggle('node-dim', !!kw && !hit);
    el.label.classList.toggle('node-dim', !!kw && !hit);
    el.label.classList.toggle('label-search', !!hit);
  }
}

// ── 详情 ──
async function selectNode(id) {
  selectedId = id;
  const n = graphData.nodes.find(x => x.id === id);
  if (!n) return;
  document.getElementById('welcome').classList.add('hidden');
  document.getElementById('detail').classList.remove('hidden');
  document.getElementById('dTitle').textContent = n.name;
  const levelName = n.level === 0 ? t('graph.unit') : n.level === 1 ? t('graph.chapter') : t('graph.concept');
  document.getElementById('dLevel').textContent = levelName;
  document.getElementById('dDiff').textContent = n.difficulty;
  const m = (n.mastery_est || 0);
  document.getElementById('dMastery').textContent = Math.round(m * 100) + '%';
  document.getElementById('dBar').style.width = Math.round(m * 100) + '%';
  document.getElementById('dLooms').textContent = n.looms_in || 0;
  document.getElementById('dAliases').value = n.aliases || '';
  const byId = new Map(graphData.nodes.map(x => [x.id, x]));
  const prereq = graphData.links.filter(l => l.concept_b === id && l.relation === 'prerequisite').map(l => byId.get(l.concept_a));
  const succ = graphData.links.filter(l => l.concept_a === id && l.relation === 'prerequisite').map(l => byId.get(l.concept_b));
  const cont = graphData.links.filter(l => (l.concept_a === id || l.concept_b === id) && l.relation === 'contrast').map(l => byId.get(l.concept_a === id ? l.concept_b : l.concept_a));
  const dPrereq = document.getElementById('dPrereq');
  dPrereq.innerHTML = '';
  if (prereq.length) dPrereq.innerHTML += `<p class="kv"><b>${t('graph.prereq')}</b>${prereq.map(p => p.name).join('、')}</p>`;
  if (succ.length) dPrereq.innerHTML += `<p class="kv"><b>${t('graph.succ')}</b>${succ.map(s => s.name).join('、')}</p>`;
  if (cont.length) dPrereq.innerHTML += `<p class="kv"><b>${t('graph.contrast')}</b>${cont.map(c => c.name).join('、')}</p>`;
  document.getElementById('dProblems').innerHTML = '';
  if (n.level === 2) loadRelatedProblems(id);
}

async function loadRelatedProblems(id) {
  const el = document.getElementById('dProblems');
  el.innerHTML = '<p class="muted">' + t('msg.loading') + '</p>';
  try {
    const resp = await fetch(`/api/graph/problems?concept=${id}`);
    const items = await resp.json();
    el.innerHTML = items.length
      ? '<p class="muted">' + t('graph.relatedLabel') + '</p><ul id="problemList">' + items.map(p =>
          `<li>${p.title}（${t('graph.masteryOf')} ${p.mastery}/5）</li>`).join('') + '</ul>'
      : '<p class="muted">' + t('graph.noRelated') + '</p>';
  } catch { el.innerHTML = '<p class="muted">' + t('graph.loadFail') + '</p>'; }
}

function prereqMode() {
  const cid = selectedId;
  window.open(`/#page-problems?prereq=${cid}`, '_blank');
}

async function saveAliases() {
  if (!selectedId) return;
  try {
    const resp = await fetch(`/api/graph/concepts/${selectedId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'LearnOS' },
      body: JSON.stringify({ aliases: document.getElementById('dAliases').value }),
    });
    const data = await resp.json();
    if (data.ok) {
      const n = graphData.nodes.find(x => x.id === selectedId);
      if (n) n.aliases = document.getElementById('dAliases').value;
      alert(t('graph.aliasSaved'));
    } else alert(data.error || 'fail');
  } catch { alert(t('graph.loadFail')); }
}

async function addConcept() {
  const name = document.getElementById('newName').value.trim();
  const parent = document.getElementById('newParent').value.trim();
  if (!name) return;
  const resp = await fetch('/api/graph/concepts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'LearnOS' },
    body: JSON.stringify({ name, parent_id: parent ? parseInt(parent, 10) : 0, subject: graphSubject() }),
  });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || t('graph.addFail')); return; }
  document.getElementById('newName').value = '';
  document.getElementById('newParent').value = '';
  await loadGraph();
}

function toast(msg) {
  const el = document.createElement('div');
  el.style.cssText = 'position:fixed;top:14px;right:14px;z-index:99;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px 14px;font-size:13px;box-shadow:0 4px 14px rgba(0,0,0,.12)';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 1800);
}

function graphSubject() {
  return new URLSearchParams(location.search).get('subject') || localStorage.getItem('subject') || 'physics';
}

async function loadGraph() {
  const resp = await fetch('/api/graph/concepts?subject=' + encodeURIComponent(graphSubject()));
  graphData = await resp.json();
  if (!graphData.nodes || graphData.nodes.length === 0) { alert(t('graph.empty')); return; }
  draw();
  requestAnimationFrame(fitToView);
  // URL ?focus=概念名：全局搜索跳转后自动选中该概念
  const focusName = new URLSearchParams(location.search).get('focus');
  if (focusName) {
    const node = graphData.nodes.find(n => n.name === focusName);
    if (node) setTimeout(() => selectNode(node.id), 200);
  }
}

function switchGraphSubject(id) {
  if (!id) return;
  localStorage.setItem('subject', id);
  const url = new URL(location.href);
  url.searchParams.set('subject', id);
  location.href = url.toString();
}

async function initGraphSubject() {
  const sel = document.getElementById('graphSubject');
  const subjects = ['physics', 'chemistry', 'math'];
  try {
    const data = await (await fetch('/api/subjects?subject=' + encodeURIComponent(graphSubject()))).json();
    for (const s of data.subjects || []) {
      if (!subjects.includes(s.id)) subjects.push(s.id);
    }
  } catch (e) { /* 内置三科兜底 */ }
  const cur = graphSubject();
  sel.innerHTML = subjects.map(s => `<option value="${s}">${s}</option>`).join('');
  if (subjects.includes(cur)) sel.value = cur;
  else sel.value = subjects[0];
}

bootGraph();
