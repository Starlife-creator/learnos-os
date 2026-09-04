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
  if (scope === document) document.title = t('graph.title');
}
// 标题跟随当前学科显示名（学科下拉已载入外文名/别名）
function setGraphTitle() {
  const sel = document.getElementById('graphSubject');
  const lab = sel && sel.selectedOptions && sel.selectedOptions[0] ? sel.selectedOptions[0].textContent : graphSubject();
  document.title = t('graph.title') + ' - ' + String(lab || '').trim();
}
async function bootGraph() {
  const lang = _curLang();
  try {
    _dict = await (await fetch(`/locale/${lang}.json`, { cache: 'no-cache' })).json();
  } catch { _dict = {}; }
  applyGraphI18n(document);
  await initGraphSubject();
  const lmSel = document.getElementById('graphLayout');
  if (lmSel) lmSel.value = layoutMode;
  buildEdgeToggles();
  loadGraph();
  loadGraphLearningPath();
}
// 边类型开关条（默认只显骨干先修/演进；其余按需开启 → 降噪）
function buildEdgeToggles() {
  const wrap = document.getElementById('edgeToggles');
  if (!wrap) return;
  wrap.innerHTML = KIND_RELATIONS.map(rel =>
    '<label class="ln" style="cursor:pointer" data-i18n="graph.rel_' + rel + '">' +
    '<input type="checkbox" data-rel="' + rel + '" ' + (edgeVis[rel] ? 'checked' : '') +
    ' onchange="toggleEdgeType(\'' + rel + '\', this.checked)" style="accent-color:var(--accent)">' +
    '<span>' + t('graph.rel_' + rel, rel) + '</span></label>').join('');
}
function toggleEdgeType(rel, on) {
  if (!(rel in edgeVis)) return;
  edgeVis[rel] = on;
  redrawAndFit();
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
// ── 性能索引：loadGraph 后预建，消除 filter/find 的 O(N²)（2650 节点学科每次重绘省数百 ms）──
let nodeById = new Map();         // id -> node
let kidsOf = new Map();           // parent_id -> [child nodes]
let byLevel = { unit: [], chapter: [], concept: [] };
// A2 章内 hub 扇出：度数/邻接索引（一张图只算一次，切换学科后重置）
let _fanDeg = null, _fanLink = null;
function fanIndex() {
  if (_fanLink) return { link: _fanLink, deg: _fanDeg };
  const deg = new Map(), link = new Map();
  for (const n of graphData.nodes) deg.set(n.id, 0);
  for (const l of graphData.links) {
    deg.set(l.concept_a, (deg.get(l.concept_a) || 0) + 1);
    deg.set(l.concept_b, (deg.get(l.concept_b) || 0) + 1);
    if (!link.has(l.concept_a)) link.set(l.concept_a, new Set());
    if (!link.has(l.concept_b)) link.set(l.concept_b, new Set());
    link.get(l.concept_a).add(l.concept_b);
    link.get(l.concept_b).add(l.concept_a);
  }
  _fanDeg = deg; _fanLink = link;
  return { link, deg };
}
function indexGraph() {
  nodeById = new Map(graphData.nodes.map(n => [n.id, n]));
  kidsOf = new Map();
  _fanLink = null; _fanDeg = null;   // 图谱/学科切换后重建 A2 索引
  byLevel = { unit: [], chapter: [], concept: [] };
  for (const n of graphData.nodes) {
    if (n.level === 0) byLevel.unit.push(n);
    else if (n.level === 1) byLevel.chapter.push(n);
    else byLevel.concept.push(n);
    if (n.parent_id) {
      if (!kidsOf.has(n.parent_id)) kidsOf.set(n.parent_id, []);
      kidsOf.get(n.parent_id).push(n);
    }
  }
}
const SAVE_KEY = 'conceptMapPositions.v1';      // 目录视图（沿用旧键，保留用户拖拽）
const LAYOUT_MODES = ['hier', 'chain', 'mastery', 'difficulty'];
function saveKeyForMode(mode) {
  return mode === 'hier' ? 'conceptMapPositions.v1' : `conceptMapPositions.${mode}.v1`;
}
// 当前布局预设（本地持久化；每个预设独立记忆拖拽位置）
let layoutMode = (() => {
  try {
    const m = localStorage.getItem('graphLayoutMode');
    return LAYOUT_MODES.includes(m) ? m : 'hier';
  } catch { return 'hier'; }
})();
// ── LOD 分级渲染：2650 节点学科下全量 paint 是卡顿主因（14522 元素）──
// 缩放过小 (<0.55) 时节点在屏幕上只是几个像素的点，文字/次要边/大点击圈都不可读，
// 跳过它们能砍掉 ~60% 的 SVG 元素；放大到阈值以上再补齐。
const LOD_TEXT_SCALE = 0.55;    // 低于此缩放：不画文字标签
const LOD_HIT_MAX = 700;        // 可见节点超过此数：不画放大点击圈（circle 本身仍可点）
const LOD_EDGE_SCALE = 0.4;     // 低于此缩放：只保留 prerequisite 先修边（related/contrast 省略）
let lodText = true;             // 当前绘制是否含文字（跨阈值时按需重绘）
let lodHit = true;
let lodEdge = true;
// ── 边类型可见性（默认只显"骨干"，其余按需开；用于降噪）──
const KIND_RELATIONS = ['prerequisite', 'progression', 'inclusion', 'analogy', 'contrast', 'related'];
const edgeVis = { prerequisite: true, progression: true, inclusion: false, analogy: false, contrast: false, related: false };

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

function unitColorList() {
  const colors = getComputedStyle(document.documentElement).getPropertyValue('--unit-colors').split(',').map(s => s.trim());
  return colors.length ? colors : ['#6366f1'];
}
function unitColor(idx) {
  const colors = unitColorList();
  return colors[idx % colors.length] || '#6366f1';
}

function visibleNodeIds() {
  const vis = new Set(byLevel.unit.map(u => u.id));
  for (const c of byLevel.chapter) {
    vis.add(c.id);
    if (expanded.has(c.id)) {
      for (const x of kidsOf.get(c.id) || []) vis.add(x.id);
    }
  }
  return vis;
}

// ── 布局：单元分列，章节堆叠，展开的章节在下方平铺概念 ──
// A2：章内 hub 扇出——用占用栅格把高连度概念放章节中线列，并把它在本章内的邻居
// 环绕其上下/左右错开排布（扇出）；其余概念按行优先补进空位。全程占用栅格保证不重叠。
function placeChapterKids(kids, cols, startX, startY, colW, rowH) {
  const rows = Math.max(1, Math.ceil(kids.length / cols));
  const { link, deg } = fanIndex();
  const K = 4;
  const hubs = kids
    .filter(k => (deg.get(k.id) || 0) >= K)
    .sort((a, b) => (deg.get(b.id) || 0) - (deg.get(a.id) || 0));
  const posOut = new Map(), occ = new Set(), placed = new Set();
  const isFree = (r, c) => r >= 0 && r < rows && c >= 0 && c < cols && !occ.has(r + ':' + c);
  const take = (id, r, c) => {
    if (!isFree(r, c)) return false;
    occ.add(r + ':' + c);
    posOut.set(id, { x: startX + c * colW, y: startY + r * rowH });
    return true;
  };
  const centerC = Math.max(0, Math.floor((cols - 1) / 2));
  // 1) hub 放章节中线列
  for (const h of hubs)
    for (let r = 0; r < rows; r++) if (take(h.id, r, centerC)) { placed.add(h.id); break; }
  // 2) 每个 hub 的本章内邻居环绕其八邻域错开局位
  const RING = [[-1, 0], [1, 0], [0, -1], [0, 1], [-1, -1], [1, -1], [-1, 1], [1, 1]];
  for (const h of hubs) {
    const hp = posOut.get(h.id);
    if (!hp) continue;
    const hr = Math.round((hp.y - startY) / rowH), hc = Math.round((hp.x - startX) / colW);
    const nbrs = link.get(h.id)
      ? Array.from(link.get(h.id)).filter(nid => !placed.has(nid) && kids.some(k => k.id === nid))
      : [];
    for (const nid of nbrs) {
      for (const [dr, dc] of RING) if (take(nid, hr + dr, hc + dc)) { placed.add(nid); break; }
    }
  }
  // 3) 其余按行优先补满
  for (const kk of kids) {
    if (placed.has(kk.id)) continue;
    outer: for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++)
      if (take(kk.id, r, c)) { placed.add(kk.id); break outer; }
  }
  return posOut;
}

function layoutNodes(sortKids) {
  const pos = new Map();
  const TOP = 46, ROW_H = 46, COL_W = 118, CH_GAP = 26, UNIT_GAP = 90;
  let x = 60;
  for (const u of byLevel.unit) {
    const chs = kidsOf.get(u.id) || [];
    let y = TOP + 40;
    let colW = 0;
    for (const ch of chs) {
      let kids = expanded.has(ch.id) ? (kidsOf.get(ch.id) || []) : [];
      if (sortKids) kids = kids.slice().sort(sortKids);
      const rows = Math.max(1, Math.ceil(kids.length / 5));
      const cols = Math.min(5, Math.max(1, kids.length));
      const startKX = x + 190;
      const startKY = y + 52;
      const conceptW = kids.length ? 190 + (cols - 1) * COL_W : 0;
      pos.set(ch.id, { x: x + 90, y: y + 16 });
      if (kids.length) {
        const cmap = placeChapterKids(kids, cols, startKX, startKY, COL_W, ROW_H);
        cmap.forEach((p, id) => pos.set(id, p));
      }
      colW = Math.max(colW, conceptW);
      y += (kids.length ? 52 + rows * ROW_H : 40) + CH_GAP;
    }
    pos.set(u.id, { x: x + colW / 2, y: TOP });
    x += colW + UNIT_GAP;
  }
  return pos;
}

// ── 多视图预设分发：hier=目录 / chain=先修链(Sugiyama) / mastery·difficulty=属性排序 ──
function computeLayout(mode) {
  if (mode === 'chain') return chainLayout();
  if (mode === 'mastery') return layoutNodes((a, b) => (b.mastery_est || 0) - (a.mastery_est || 0));
  if (mode === 'difficulty') return layoutNodes((a, b) => (b.difficulty || 0) - (a.difficulty || 0));
  return layoutNodes();
}

// 先修链视图：按先修 DAG「最长路径分层」+ 层内 barycenter 最小交叉。
// 概念左→右按先修顺流（先修总在左），同层按 barycenter 往复重排减交叉。
// 章/单元只作"分组点"画在其子概念质心，保留三级身份（不破坏模型）。
function chainLayout() {
  expanded = new Set(byLevel.chapter.map(c => c.id)); // 先修链视图展开全部概念
  const pos = new Map();
  const set = byLevel.concept;
  if (!set.length) return pos;
  const pred = new Map(), succ = new Map();
  for (const l of graphData.links) {
    if (l.relation !== 'prerequisite') continue;
    const a = nodeById.get(l.concept_a), b = nodeById.get(l.concept_b);
    if (!a || !b || a.level !== 2 || b.level !== 2) continue; // 仅概念参与分层
    if (!pred.has(l.concept_b)) pred.set(l.concept_b, []);
    pred.get(l.concept_b).push(l.concept_a);
    if (!succ.has(l.concept_a)) succ.set(l.concept_a, []);
    succ.get(l.concept_a).push(l.concept_b);
  }
  const ids = set.map(c => c.id);
  // 最长路径分层：ranks 0..max（连续）
  const rank = new Map(ids.map(i => [i, 0]));
  let changed = true;
  for (let pass = 0; changed && pass < 300; pass++) {
    changed = false;
    for (const id of ids) {
      const ps = pred.get(id) || [];
      const m = ps.length ? Math.max(...ps.map(p => rank.get(p) || 0)) + 1 : 0;
      if (m !== (rank.get(id) || 0)) { rank.set(id, m); changed = true; }
    }
  }
  // 分组成层
  const groups = new Map();
  for (const id of ids) {
    const r = rank.get(id);
    if (!groups.has(r)) groups.set(r, []);
    groups.get(r).push(id);
  }
  // 层内 barycenter 排序（6 轮往复，关键：减交叉）
  const centered = new Map();
  for (const [r, layer] of groups) centered.set(r, new Map(layer.map((id, i) => [id, i])));
  for (let pass = 0; pass < 6; pass++) {
    const ranks = [...groups.keys()].sort((a, b) => a - b);
    for (const r of ranks) {
      if (groups.get(r).length < 2) continue;
      const toSucc = pass % 2 === 0; // 奇偶轮交替以邻居层取向
      const idx = new Map();
      for (const id of groups.get(r)) {
        const nb = (toSucc ? succ.get(id) : pred.get(id)) || [];
        const vals = [];
        for (const n of nb) {
          const nr = rank.get(n);
          const cm = centered.get(nr);
          if (nr !== r && cm && cm.has(n)) vals.push(cm.get(n));
        }
        idx.set(id, vals.length ? vals.reduce((x, y) => x + y, 0) / vals.length : (centered.get(r).get(id) || 0));
      }
      groups.get(r).sort((x, y) => (idx.get(x) || 0) - (idx.get(y) || 0));
      centered.set(r, new Map(groups.get(r).map((id, i) => [id, i])));
    }
  }
  // 坐标：x=rank 列，y=层内序；每列纵向居中使画布紧凑
  const colW = 196, rowH = 40;
  const y0 = new Map();
  for (const [r, layer] of groups) y0.set(r, -((layer.length - 1) * rowH) / 2);
  for (const [r, layer] of groups)
    for (let i = 0; i < layer.length; i++) pos.set(layer[i], { x: r * colW, y: y0.get(r) + i * rowH });
  // 章/单元 = 其子节点质心（层级身份点）
  const agg = (parents) => {
    for (const p of parents) {
      const kids = (kidsOf.get(p.id) || []).filter(k => pos.has(k.id));
      if (kids.length) {
        pos.set(p.id, {
          x: kids.reduce((s, k) => s + pos.get(k.id).x, 0) / kids.length,
          y: kids.reduce((s, k) => s + pos.get(k.id).y, 0) / kids.length,
        });
      }
    }
  };
  agg(byLevel.chapter); agg(byLevel.unit);
  return pos;
}

function loadSavedPositions(mode) {
  try {
    const raw = localStorage.getItem(saveKeyForMode(mode || layoutMode));
    return raw ? JSON.parse(raw) : {};
  } catch { return {}; }
}
function savePositions(mode) {
  const obj = {};
  positions.forEach((p, id) => { obj[id] = { x: Math.round(p.x), y: Math.round(p.y) }; });
  try { localStorage.setItem(saveKeyForMode(mode || layoutMode), JSON.stringify(obj)); } catch {}
}

// ── 绘制 ──
function draw(useSaved = true) {
  const saved = useSaved ? loadSavedPositions(layoutMode) : {};
  const auto = computeLayout(layoutMode);
  const vis = visibleNodeIds();
  positions = new Map();
  for (const n of graphData.nodes) {
    if (!vis.has(n.id)) continue;
    const p = (saved[n.id] && isFinite(saved[n.id].x)) ? saved[n.id] : auto.get(n.id);
    if (p) positions.set(n.id, { x: p.x, y: p.y });
  }

  // LOD 分级：按当前缩放与可见规模决定画什么（跨阈值时由 scheduleLodRedraw 触发重绘）
  lodText = view.scale >= LOD_TEXT_SCALE;
  lodHit = vis.size <= LOD_HIT_MAX;
  lodEdge = view.scale >= LOD_EDGE_SCALE;

  svg.innerHTML = '';
  nodeEls = new Map();
  descendants = new Map();
  const gMain = make('g');
  svg.appendChild(gMain);
  // 有向边箭头 marker（先修/演进）
  const defs = make('defs');
  defs.innerHTML =
    '<marker id="arrP" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto" markerUnits="strokeWidth">' +
    '<path d="M0,0 L9,4.5 L0,9 Z" fill="#3b82f6"/></marker>' +
    '<marker id="arrProg" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto" markerUnits="strokeWidth">' +
    '<path d="M0,0 L9,4.5 L0,9 Z" fill="#f97316"/></marker>';
  gMain.appendChild(defs);
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
  // 迭代式展开（避免递归 + 数组 spread 在长链上的 O(N²)/爆栈风险）
  const computeDesc = (id) => {
    const out = [];
    const stack = [id];
    while (stack.length) {
      const cur = stack.pop();
      for (const k of byParent.get(cur) || []) { out.push(k); stack.push(k); }
    }
    return out;
  };
  for (const id of vis) descendants.set(id, computeDesc(id));

  // 边（曲线，只画两端可见的；LOD 缩小时省略次要边）
  edges = [];
  edgeByNode = new Map();
  // ── 边路由：chain=正交化（横干竖端）；hier=跨章束带 + 章内 A3 曲率扇 ──
  const dat = [];
  const deg = new Map();
  for (const l of graphData.links) {
    if (!edgeVis[l.relation]) continue;      // 显隐由用户边类型开关决定
    if (!positions.has(l.concept_a) || !positions.has(l.concept_b)) continue;
    dat.push({ a: l.concept_a, b: l.concept_b, rel: l.relation });
    deg.set(l.concept_a, (deg.get(l.concept_a) || 0) + 1);
    deg.set(l.concept_b, (deg.get(l.concept_b) || 0) + 1);
  }
  const hubOf = dat.map(e => (deg.get(e.a) >= deg.get(e.b) ? e.a : e.b));
  // A3 曲率错开（章内 quad 边用）：按高连度端分组、按角度排序，给垂直偏移
  const off = new Map();
  const groups = new Map();
  dat.forEach((e, i) => { if (!groups.has(hubOf[i])) groups.set(hubOf[i], []); groups.get(hubOf[i]).push(i); });
  const GAP = 5;
  for (const [hub, idxs] of groups) {
    if (idxs.length < 2) { idxs.forEach(i => off.set(i, 0)); continue; }
    const sp = positions.get(hub);
    const byAng = idxs.map(i => {
      const e = dat[i];
      const o = positions.get(e.a === hub ? e.b : e.a);
      return { i, ang: Math.atan2(o.y - sp.y, o.x - sp.x) };
    });
    byAng.sort((x, y) => x.ang - y.ang);
    byAng.forEach((row, k) => off.set(row.i, (k - (idxs.length - 1) / 2) * GAP));
  }
  // 跨章束带：按"章对"分组，同组共享中心 C，控制点都拉向 C → 中途收敛成带
  const zoneOf = id => { const r = nodeById.get(id); return (r && r.chapter_id) ? r.chapter_id : id; };
  const bgroups = new Map();
  dat.forEach((e, i) => {
    const za = zoneOf(e.a), zb = zoneOf(e.b);
    if (za === zb || layoutMode === 'chain') return;   // 同章→quad；chain→ortho
    const key = za < zb ? za + '|' + zb : zb + '|' + za;
    if (!bgroups.has(key)) bgroups.set(key, []);
    bgroups.get(key).push(i);
  });
  const bundle = new Map();
  for (const [, idxs] of bgroups) {
    if (idxs.length < 2) continue;              // 单条跨章边不束（走 quad）
    let mx = 0, my = 0;
    for (const i of idxs) { const e = dat[i], pa = positions.get(e.a), pb = positions.get(e.b); mx += (pa.x + pb.x) / 2; my += (pa.y + pb.y) / 2; }
    const n = idxs.length; mx /= n; my /= n;
    idxs.forEach((i, k) => bundle.set(i, {
      ctrl: { x: mx, y: my }, fan: (k - (n - 1) / 2) * 2.5, fan2: -(k - (n - 1) / 2) * 1.2,
    }));
  }
  dat.forEach((e, i) => {
    let kind = 'quad';
    if (layoutMode === 'chain') kind = 'ortho';
    else if (bundle.has(i)) kind = 'bundle';
    const bd = bundle.get(i) || { ctrl: { x: 0, y: 0 }, fan: 0, fan2: 0 };
    const desc = { a: e.a, b: e.b, rel: e.rel, kind, hub: hubOf[i], other: e.a === hubOf[i] ? e.b : e.a, off: off.get(i) || 0, ctrl: bd.ctrl, fan: bd.fan, fan2: bd.fan2 };
    const dirMark = e.rel === 'prerequisite' ? 'url(#arrP)' : e.rel === 'progression' ? 'url(#arrProg)' : null;
    const attrs = { d: edgeDescD(desc), class: 'edge edge-' + e.rel, 'stroke-width': 1.2 };
    if (dirMark) attrs['marker-end'] = dirMark;
    const path = make('path', attrs);
    gEdges.appendChild(path);
    const idx = edges.length;
    edges.push(Object.assign(desc, { path }));
    if (!edgeByNode.has(e.a)) edgeByNode.set(e.a, []);
    if (!edgeByNode.has(e.b)) edgeByNode.set(e.b, []);
    edgeByNode.get(e.a).push(idx);
    edgeByNode.get(e.b).push(idx);
  })

  // 节点（单元色每轮只取一次样式变量；点击走 svg 级事件委托，不再逐节点挂监听）
  const unitIdx = new Map(byLevel.unit.map((u, i) => [u.id, i]));
  const unitCols = unitColorList();
  for (const n of graphData.nodes) {
    if (!positions.has(n.id)) continue;
    const p = positions.get(n.id);
    const col = n.level === 0 ? (unitCols[unitIdx.get(n.id) || 0] || '#6366f1')
      : n.level === 1 ? '#94a3b8'
      : nodeColor(n.mastery_est || 0);
    const r = n.level === 0 ? 26 : n.level === 1 ? 15 : 12;
    const group = make('g', { 'data-id': n.id });
    group.style.cursor = 'move';
    // 大图缩小时不放大点击圈：circle 本身 pointer-events:all 已可点，省 1/3 元素
    const hit = lodHit ? make('circle', {
      cx: p.x, cy: p.y, r: Math.max(14 / view.scale, r + 10), class: 'node-hit',
    }) : null;
    if (hit) group.appendChild(hit);
    const circle = make('circle', {
      cx: p.x, cy: p.y, r: r,
      class: 'node-' + (n.level === 0 ? 'unit' : n.level === 1 ? 'chapter' : 'concept'),
      fill: col, 'fill-opacity': n.level === 0 ? 0.18 : 0.9,
      'stroke-width': n.level === 0 ? 2.4 : n.level === 1 ? 2 : 1.6,
      'data-id': n.id, 'pointer-events': 'all',
    });
    group.appendChild(circle);
    gNodes.appendChild(group);
    // LOD 缩小时跳过文字标签（2650 个 <text> 是 paint 大头，缩小后也看不清）
    let label = null;
    if (lodText) {
      const labelCls = n.level === 0 ? 'label label-unit' : n.level === 1 ? 'label label-chapter' : 'label';
      const labelDy = n.level === 0 ? -38 : n.level === 1 ? 32 : 24;
      const suffix = n.level === 1 ? (expanded.has(n.id) ? ' ▼' : ' ▶') : '';
      label = make('text', {
        x: p.x, y: p.y + labelDy, 'text-anchor': 'middle', class: labelCls,
      }, n.name + suffix);
      gNodes.appendChild(label);
    }
    nodeEls.set(n.id, { hit, circle, label });
  }
  applyView();
  if (focusedId && nodeEls.has(focusedId)) _dimGraph(focusedId);   // 重绘后保持聚焦态
}

// LOD 跨阈值重绘：缩放/平移后 scale 变化可能让文字/次要边需要出现或隐藏，
// 但不必每个 wheel tick 都重建 DOM——debounce 到缩放停顿后再重绘。
let lodTimer = 0;
function scheduleLodRedraw() {
  if (lodTimer) return;
  lodTimer = setTimeout(() => {
    lodTimer = 0;
    const wantText = view.scale >= LOD_TEXT_SCALE;
    const wantEdge = view.scale >= LOD_EDGE_SCALE;
    if (wantText !== lodText || wantEdge !== lodEdge) {
      draw();
      savePositions();
    }
  }, 180);
}

// svg 级 click 委托：N 个节点只挂 1 个监听（旧实现逐 group 挂，2650 节点 = 2650 个闭包）
svg.addEventListener('click', e => {
  if (justDragged) { justDragged = false; return; }
  const g = e.target.closest ? e.target.closest('g[data-id]') : null;
  if (!g) { clearFocus(); return; }            // 点空白 → 退出聚焦
  const node = nodeById.get(Number(g.getAttribute('data-id')));
  if (!node) return;
  if (linkMode) { onLinkClick(node.id); return; }
  if (node.level === 1) toggleChapter(node.id);
  selectNode(node.id);
  if (node.level !== 0) setFocus(node.id);     // 聚焦该节点邻域（单元级略过）
});

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
  if (el.hit) { el.hit.setAttribute('cx', p.x); el.hit.setAttribute('cy', p.y); }
  if (el.label) {
    const n = nodeById.get(id);
    el.label.setAttribute('x', p.x);
    el.label.setAttribute('y', p.y + (n && n.level === 0 ? -38 : n && n.level === 1 ? 32 : 24));
  }
  const ids = edgeByNode.get(id);
  if (ids) for (const i of ids) rebindEdge(i);
}

function refreshHitRadii() {
  for (const el of nodeEls.values()) {
    if (!el.hit) continue;
    const r = parseFloat(el.circle.getAttribute('r'));
    el.hit.setAttribute('r', Math.max(14 / view.scale, r + 10));
  }
}

function collapseAll() { expanded.clear(); redrawAndFit(); }
function expandAll() {
  for (const c of byLevel.chapter) expanded.add(c.id);
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
  e.path.setAttribute('d', edgeDescD(e));   // 拖动时按当前路由方式重算
}

// 边路径：ortho=正交（横干竖端）；bundle=束带(控制点拉向共享中心)；quad=A3 曲率扇
function edgeDescD(dd) {
  const A = positions.get(dd.a), B = positions.get(dd.b);
  if (!A || !B) return '';
  if (dd.kind === 'ortho') {
    const my = (A.y + B.y) / 2;
    return `M ${A.x} ${A.y} L ${A.x} ${my} L ${B.x} ${my} L ${B.x} ${B.y}`;
  }
  if (dd.kind === 'bundle') {
    const C = dd.ctrl;
    const d1x = C.x - A.x, d1y = C.y - A.y, L1 = Math.hypot(d1x, d1y) || 1;
    const p1x = A.x + d1x * 0.5 + (-d1y / L1) * dd.fan, p1y = A.y + d1y * 0.5 + (d1x / L1) * dd.fan;
    const d2x = C.x - B.x, d2y = C.y - B.y, L2 = Math.hypot(d2x, d2y) || 1;
    const p2x = B.x + d2x * 0.5 + (-d2y / L2) * dd.fan2, p2y = B.y + d2y * 0.5 + (d2x / L2) * dd.fan2;
    return `M ${A.x} ${A.y} C ${p1x} ${p1y} ${p2x} ${p2y} ${B.x} ${B.y}`;
  }
  const H = positions.get(dd.hub), O = positions.get(dd.other);
  const dx = O.x - H.x, dy = O.y - H.y, len = Math.hypot(dx, dy) || 1;
  const m = dd.off || 0;
  return `M ${H.x} ${H.y} Q ${(H.x + O.x) / 2 + (-dy / len) * m} ${(H.y + O.y) / 2 + (dx / len) * m} ${O.x} ${O.y}`;
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
  // fit 改变 scale 后可能跨过 LOD 阈值，按需重绘（如初始加载后 2650 文字应消失）
  scheduleLodRedraw();
}

function autoLayout() {
  draw(false);
  fitToView();
  savePositions();
  toast(t('graph.autoLayout') + ' ✓');
}
function resetLayout() {
  try { localStorage.removeItem(saveKeyForMode(layoutMode)); } catch {}
  autoLayout();
}
function switchLayoutMode(mode) {
  if (!LAYOUT_MODES.includes(mode) || mode === layoutMode) return;
  savePositions();                       // 离开前保存当前预设的位置
  layoutMode = mode;
  try { localStorage.setItem('graphLayoutMode', mode); } catch {}
  autoLayout();                          // 用新模式自动布局 + 适配视口
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
  scheduleLodRedraw();
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
  scheduleLodRedraw();
}, { passive: false });

let dragNode = null;
let dragStart = null;
let justDragged = false;
svg.addEventListener('pointerdown', e => {
  const g = e.target.closest ? e.target.closest('g[data-id]') : null;
  if (g) {
    const node = nodeById.get(Number(g.getAttribute('data-id')));
    if (!node) return;
    dragNode = { id: node.id, sx: e.clientX, sy: e.clientY, moved: false };
    e.preventDefault();
    return;
  }
  dragStart = { x: e.clientX, y: e.clientY };
  svg.classList.add('dragging');
});
// 用 rAF 合并高频 pointermove：每帧只重绘一次，避免后代多时 60~120/s 的同步 DOM 更新掉帧
let _pmScheduled = false;
let _pmLastEvent = null;
function _flushPointerMove() {
  _pmScheduled = false;
  const e = _pmLastEvent;
  if (!e) return;
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
}
window.addEventListener('pointermove', e => {
  _pmLastEvent = e;
  if (!_pmScheduled) { _pmScheduled = true; requestAnimationFrame(_flushPointerMove); }
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
    for (const n of byLevel.concept) {
      if (n.name.toLowerCase().includes(kw)) {
        if (n.parent_id && !expanded.has(n.parent_id)) { expanded.add(n.parent_id); needRedraw = true; }
      }
    }
    if (needRedraw) { draw(); requestAnimationFrame(fitToView); }
  }
  for (const [id, el] of nodeEls) {
    const n = nodeById.get(id);
    if (!n) continue;
    const hit = kw && n.name.toLowerCase().includes(kw);
    el.circle.classList.toggle('node-dim', !!kw && !hit);
    if (el.label) {
      el.label.classList.toggle('node-dim', !!kw && !hit);
      el.label.classList.toggle('label-search', !!hit);
    }
  }
}

// ── 详情 ──
async function selectNode(id) {
  selectedId = id;
  const n = nodeById.get(id);
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
  document.getElementById('dExplanation').value = n.explanation || '';
  updateExplanationBadge(n);
  // G2：掌握判据（evidence JSON 数组 → 每行一条）+ 口试模板
  let evLines = [];
  try { evLines = Array.isArray(JSON.parse(n.evidence || '[]')) ? JSON.parse(n.evidence || '[]') : []; } catch (e) {}
  document.getElementById('dEvidence').value = evLines.join('\n');
  document.getElementById('dAssessPrompt').value = n.assessment_prompt || '';
  // 单遍扫 links（旧实现三遍 filter + 每次重建 byId Map）；G1：边带理由/锚点 tooltip
  const prereq = [], succ = [], cont = [];
  const escAttr = s => _escGraphLP(String(s || '')).replace(/"/g, '&quot;');
  for (const l of graphData.links) {
    const tip = l.reason ? ` title="${escAttr(l.reason + (l.evidence_ref ? ' · ' + l.evidence_ref : ''))} [${l.strength || 'soft'}]"` : '';
    if (l.relation === 'prerequisite') {
      if (l.concept_b === id) { const x = nodeById.get(l.concept_a); if (x) prereq.push(`<span${tip}>${_escGraphLP(x.name)}</span>`); }
      else if (l.concept_a === id) { const x = nodeById.get(l.concept_b); if (x) succ.push(`<span${tip}>${_escGraphLP(x.name)}</span>`); }
    } else if (l.relation === 'contrast' && (l.concept_a === id || l.concept_b === id)) {
      const x = nodeById.get(l.concept_a === id ? l.concept_b : l.concept_a);
      if (x) cont.push(`<span${tip}>${_escGraphLP(x.name)}</span>`);
    }
  }
  const dPrereq = document.getElementById('dPrereq');
  dPrereq.innerHTML = '';
  if (prereq.length) dPrereq.innerHTML += `<p class="kv"><b>${t('graph.prereq')}</b>${prereq.join('、')}</p>`;
  if (succ.length) dPrereq.innerHTML += `<p class="kv"><b>${t('graph.succ')}</b>${succ.join('、')}</p>`;
  if (cont.length) dPrereq.innerHTML += `<p class="kv"><b>${t('graph.contrast')}</b>${cont.join('、')}</p>`;
  document.getElementById('dProblems').innerHTML = '';
  // 做闪卡：仅为章/概念级显示（单元级不在闪卡概念下拉内）
  const mk = document.getElementById('btnMakeCardWrap');
  if (mk) mk.style.display = n.level === 0 ? 'none' : 'flex';
  const delBtn = document.getElementById('btnDeleteConcept');
  if (delBtn) {
    const blocked = (descendants.get(id) || []).length > 0 || (n.looms_in || 0) > 0;
    delBtn.disabled = blocked;
    delBtn.title = blocked ? t('graph.deleteBlocked') : '';
  }
  if (n.level === 2) loadRelatedProblems(id);
}

// 图谱选中概念 → 跳转闪卡页并自动为该概念建卡（深链，含学科对齐）
function goMakeCard() {
  if (!selectedId) return;
  try { localStorage.setItem('subject', graphSubject()); } catch (e) {}
  location.href = 'index.html#cards?concept=' + selectedId;
}

async function loadRelatedProblems(id) {
  const el = document.getElementById('dProblems');
  el.innerHTML = '<p class="muted">' + t('msg.loading') + '</p>';
  try {
    const resp = await fetch(`/api/graph/problems?concept=${id}`);
    const data = await resp.json();
    const items = Array.isArray(data) ? data : (data.items || []);  // 后端返回 {items, chain_count}
    el.innerHTML = items.length
      ? '<p class="muted">' + t('graph.relatedLabel') + '</p><ul id="problemList">' + items.map(p =>
          `<li>${_escGraphLP(p.title)}（${t('graph.masteryOf')} ${p.mastery}/5）</li>`).join('') + '</ul>'
      : '<p class="muted">' + t('graph.noRelated') + '</p>';
  } catch { el.innerHTML = '<p class="muted">' + t('graph.loadFail') + '</p>'; }
}

function prereqMode() {
  const cid = selectedId;
  // 同 app-review.js：#problems 是路由名，#page-problems 是 DOM id，深链必须用前者。
  window.open(`/#problems?prereq=${cid}`, '_blank');
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

async function saveExplanation() {
  if (!selectedId) return;
  const val = document.getElementById('dExplanation').value;
  try {
    const resp = await fetch(`/api/graph/concepts/${selectedId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'LearnOS' },
      body: JSON.stringify({
        aliases: document.getElementById('dAliases').value,
        explanation: val,
      }),
    });
    const data = await resp.json();
    if (data.ok) {
      const n = graphData.nodes.find(x => x.id === selectedId);
      if (n) {
        n.aliases = document.getElementById('dAliases').value;
        const edited = !!(val && val.trim());
        n.explanation_user = edited ? val : null;
        n.explanation = edited ? val : (n.explanation_seed || '');
      }
      updateExplanationBadge(n);
      toast(t('graph.explainSaved'));
    } else alert(data.error || 'fail');
  } catch { alert(t('graph.loadFail')); }
}

// G2：保存掌握判据（每行一条）+ 口试模板（可含 {{name}} 占位）
async function saveEvidence() {
  if (!selectedId) return;
  const lines = document.getElementById('dEvidence').value
    .split('\n').map(s => s.trim()).filter(Boolean);
  const prompt = document.getElementById('dAssessPrompt').value.trim();
  try {
    const resp = await fetch(`/api/graph/concepts/${selectedId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'LearnOS' },
      body: JSON.stringify({ evidence: lines, assessment_prompt: prompt }),
    });
    const data = await resp.json();
    if (data.ok) {
      const n = graphData.nodes.find(x => x.id === selectedId);
      if (n) { n.evidence = JSON.stringify(lines); n.assessment_prompt = prompt; }
      toast(t('graph.evidenceSaved'));
    } else alert(data.error || 'fail');
  } catch { alert(t('graph.loadFail')); }
}

async function generateExplanation() {
  if (!selectedId) return;
  const n = nodeById.get(selectedId);
  if (!n) return;
  const btn = document.getElementById('btnGenExplain');
  if (btn) { btn.disabled = true; btn.textContent = t('graph.explainGenning'); }
  try {
    // 收集真实先修/后继/易混，发往 AI 使其生成与种子一致的「定义+结构」分块风格
    const pre = [], suc = [], con = [];
    for (const l of graphData.links) {
      if (l.relation === 'prerequisite') {
        if (l.concept_b === selectedId) { const x = nodeById.get(l.concept_a); if (x) pre.push(x.name); }
        else if (l.concept_a === selectedId) { const x = nodeById.get(l.concept_b); if (x) suc.push(x.name); }
      } else if (l.relation === 'contrast' && (l.concept_a === selectedId || l.concept_b === selectedId)) {
        const x = nodeById.get(l.concept_a === selectedId ? l.concept_b : l.concept_a);
        if (x) con.push(x.name);
      }
    }
    const payload = {
      name: n.name,
      subject: graphSubject(),
      aliases: n.aliases || '',
      prereq: pre.join('、'),
      succ: suc.join('、'),
      contrast: con.join('、'),
    };
    // U2 引用面板：注入开关开启且已填入引用段落 → 透传 context_ref（后端优先依据该资料）
    if (refInjectOn) {
      const ref = document.getElementById('dRefText') ? document.getElementById('dRefText').value.trim() : '';
      if (ref) payload.context_ref = ref;
    }
    const resp = await fetch('/api/ai/concept-explanation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'LearnOS' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.explanation) {
      document.getElementById('dExplanation').value = data.explanation;
      toast(t('graph.explainGenDone'));
    } else alert(data.error || t('graph.loadFail'));
  } catch { alert(t('graph.loadFail')); }
  finally { if (btn) { btn.disabled = false; btn.textContent = t('graph.explainGen'); } }
}

// ── U2 引用面板：选资料段落 / 子图摘要 → 实时 token 估算与占比 → 确认注入 AI 提示 ──
let refInjectOn = false;
let _refTimer = null;

function toggleRefPanel() {
  const body = document.getElementById('refPanelBody');
  const btn = document.getElementById('btnRefToggle');
  if (!body) return;
  const show = body.style.display === 'none';
  body.style.display = show ? '' : 'none';
  if (btn) btn.textContent = show ? t('graph.refClose') : t('graph.refOpen');
  if (show) updateRefEstimate();
}

function onRefInput() {
  clearTimeout(_refTimer);
  _refTimer = setTimeout(updateRefEstimate, 250);
}

async function updateRefEstimate() {
  const el = document.getElementById('dRefEstimate');
  const text = document.getElementById('dRefText') ? document.getElementById('dRefText').value : '';
  if (!el) return;
  if (!text.trim()) { el.textContent = t('graph.refEmpty'); return; }
  el.textContent = t('graph.refCalc');
  try {
    const resp = await fetch('/api/ai/context/estimate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'LearnOS' },
      body: JSON.stringify({ text }),
    });
    const d = await resp.json();
    if (d.tokens !== undefined) {
      el.textContent = t('graph.refTokens').replace('{n}', d.tokens).replace('{p}', ((d.ratio || 0) * 100).toFixed(1));
    } else {
      el.textContent = t('graph.refError');
    }
  } catch { el.textContent = t('graph.refError'); }
}

function toggleRefInject() {
  refInjectOn = !refInjectOn;
  const btn = document.getElementById('btnRefInject');
  if (btn) {
    btn.textContent = refInjectOn ? t('graph.refInjectOn') : t('graph.refInjectOff');
    btn.className = refInjectOn ? 'btn btn-primary' : 'btn';
  }
}

async function refSearchOwnMaterial() {
  const q = (nodeById.get(selectedId) || {}).name || '';
  if (!q) return;
  const btn = document.getElementById('btnRefSearch');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = t('graph.refSearching'); }
  try {
    const resp = await fetch(`/api/rag/search?q=${encodeURIComponent(q)}&k=5`);
    const d = await resp.json();
    const items = Array.isArray(d.items) ? d.items : [];
    if (!items.length) { toast(t('graph.refNoHit')); return; }
    const top = items[0];
    const label = top.name ? `${top.name}${top.page ? ' 第' + top.page + '页' : ''}` : '';
    const ta = document.getElementById('dRefText');
    if (ta) ta.value = (label ? `[${label}] ` : '') + (top.content || '');
    updateRefEstimate();
  } catch { toast(t('graph.refError')); }
  finally { if (btn) { btn.disabled = false; btn.textContent = old; } }
}

// 详解徽标：有用户覆盖层显示「已编辑」并可回退，否则显示「默认(种子)」
function updateExplanationBadge(n) {
  const badge = document.getElementById('dExplanationBadge');
  const revertBtn = document.getElementById('btnRevertExplain');
  if (!badge || !revertBtn || !n) return;
  const hasOverride = !!(n.explanation_user && String(n.explanation_user).trim());
  if (hasOverride) {
    badge.textContent = t('graph.explainEdited');
    badge.className = 'tag tag-warn';
    badge.style.display = '';
    revertBtn.style.display = '';
  } else {
    badge.textContent = t('graph.explainSeedDefault');
    badge.className = 'tag';
    badge.style.display = '';
    revertBtn.style.display = 'none';
  }
}

// 回退到种子：清空用户覆盖层，显示值落回种子基线
async function revertExplanation() {
  if (!selectedId) return;
  const n = nodeById.get(selectedId);
  if (!n) return;
  const seed = (n.explanation_seed != null && String(n.explanation_seed) !== '')
    ? n.explanation_seed : (n.explanation || '');
  try {
    const resp = await fetch(`/api/graph/concepts/${selectedId}/explanation-override`, {
      method: 'DELETE',
      headers: { 'X-Requested-With': 'LearnOS' },
    });
    const data = await resp.json();
    if (data.ok) {
      n.explanation_user = null;
      n.explanation = seed;
      document.getElementById('dExplanation').value = seed;
      updateExplanationBadge(n);
      toast(t('graph.explainReverted'));
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
  // 学科 id 全局小写归一：避免历史 Music/music 双副本导致加载到空壳科（后端亦已归一，此处为前端防御）
  const raw = new URLSearchParams(location.search).get('subject') || localStorage.getItem('subject') || 'physics';
  return String(raw || '').trim().toLowerCase();
}

// ── 学习路径（按先修链，Phase 3）——本页自包含（无 app-core/api 依赖）──
function _escGraphLP(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
async function loadGraphLearningPath() {
  const el = document.getElementById('graphLP');
  if (!el) return;
  let d;
  try {
    d = await (await fetch('/api/learn/path?subject=' + encodeURIComponent(graphSubject()), { cache: 'no-cache' })).json();
  } catch { el.textContent = '—'; return; }
  let html = '';
  if (d.now) {
    const isPrereq = d.now.reason === 'prerequisite';
    html += `<div style="border:1px solid var(--accent);border-radius:8px;padding:8px 10px;margin-bottom:8px">
      <div class="muted">${isPrereq ? '先补先修「' + _escGraphLP(d.now.for || '') + '」' : '现在就学'}</div>
      <b>${_escGraphLP(d.now.name)}</b>
      <span class="tag ${d.now.mastery < 40 ? 'tag-mid' : 'tag-warn'}">掌握 ${d.now.mastery}%</span>
    </div>`;
  }
  const rw = (d.ready_weak || []).slice(0, 6);
  if (rw.length) {
    html += `<div class="muted" style="margin:4px 0">可立即强化（${d.ready_weak.length}）：</div>`;
    html += rw.map(w => `<div style="padding:2px 0">${_escGraphLP(w.name)} <span class="tag tag-warn">${w.mastery}%</span></div>`).join('');
  } else if (!d.now) {
    html += '<span class="muted">当前概念掌握度都不错 🎉</span>';
  }
  const bl = (d.blocked || []).slice(0, 4);
  if (bl.length) {
    html += `<div class="muted" style="margin:4px 0">被先修卡住待补（${d.blocked.length}）：</div>`;
    html += bl.map(b => `<div style="padding:2px 0">${_escGraphLP(b.name)} → 需 <b>${_escGraphLP((b.missing || []).join('、') || '')}</b></div>`).join('');
  }
  el.innerHTML = html || '<span class="muted">—</span>';
}

async function loadGraph() {
  const resp = await fetch('/api/graph/concepts?subject=' + encodeURIComponent(graphSubject()));
  graphData = await resp.json();
  if (!graphData.nodes || graphData.nodes.length === 0) { alert(t('graph.empty')); return; }
  indexGraph();
  // 先算布局+适应视口，再 draw——保证首帧就按最终 scale 走 LOD，
  // 避免先全量渲染 14522 元素再精简（2650 节点下省一次数百 ms 的重绘）
  const saved = loadSavedPositions(layoutMode);
  const auto = computeLayout(layoutMode);
  const vis = visibleNodeIds();
  positions = new Map();
  for (const n of graphData.nodes) {
    if (!vis.has(n.id)) continue;
    const p = (saved[n.id] && isFinite(saved[n.id].x)) ? saved[n.id] : auto.get(n.id);
    if (p) positions.set(n.id, { x: p.x, y: p.y });
  }
  fitToView();
  draw();
  savePositions();
  // URL ?focus=概念名：全局搜索跳转后自动选中该概念
  const focusName = new URLSearchParams(location.search).get('focus');
  if (focusName) {
    const node = nodeById.get(graphData.nodes.find(n => n.name === focusName)?.id);
    if (node) setTimeout(() => selectNode(node.id), 200);
  }
}

function switchGraphSubject(id) {
  if (!id) return;
  const sid = String(id || '').trim().toLowerCase();
  localStorage.setItem('subject', sid);
  const url = new URL(location.href);
  url.searchParams.set('subject', sid);
  location.href = url.toString();
}

async function initGraphSubject() {
  const sel = document.getElementById('graphSubject');
  const subjects = ['physics', 'chemistry', 'math'];
  const titles = {};
  try {
    const data = await (await fetch('/api/subjects?subject=' + encodeURIComponent(graphSubject()))).json();
    for (const s of data.subjects || []) {
      if (!subjects.includes(s.id)) subjects.push(s.id);
      if (s.title) titles[s.id] = s.title;
    }
  } catch (e) { /* 内置三科兜底 */ }
  const cur = graphSubject();
  sel.innerHTML = subjects.map(s => `<option value="${_escGraphLP(s)}">${_escGraphLP(titles[s] || s)}</option>`).join('');
  if (subjects.includes(cur)) sel.value = cur;
  else sel.value = subjects[0];
  setGraphTitle();
}

// ── A4 交互降噪：悬停/点击节点 → 高亮其关联边、淡化其余边与无关节点 ──
let _hoverT = 0;
let focusedId = null;            // 聚焦节点：点击节点置位，点背景/连线模式清除
function _dimGraph(id) {
  const conn = new Set(edgeByNode.get(id) || []);
  const near = new Set([id]);
  edges.forEach((e, i) => { if (conn.has(i)) { near.add(e.a); near.add(e.b); } });
  edges.forEach((e, i) => {
    const hot = conn.has(i);
    e.path.classList.toggle('edge-hot', hot);
    e.path.classList.toggle('edge-dim', !hot);
  });
  nodeEls.forEach((el, nid) => { el.circle.classList.toggle('node-fade', !near.has(nid)); });
}
function hoverNode(id) { clearTimeout(_hoverT); _dimGraph(id); }
function unhoverNode() {
  clearTimeout(_hoverT);
  edges.forEach(e => e.path.classList.remove('edge-hot', 'edge-dim'));
  nodeEls.forEach(el => el.circle.classList.remove('node-fade'));
}
function setFocus(id) { focusedId = id || null; focusedId ? _dimGraph(focusedId) : unhoverNode(); }
function clearFocus() { focusedId = null; unhoverNode(); }
svg.addEventListener('mouseover', e => {
  if (focusedId) return;                              // 聚焦态由点击主导，悬停不再覆盖
  const g = e.target.closest ? e.target.closest('g[data-id]') : null;
  if (!g) return;
  hoverNode(Number(g.getAttribute('data-id')));
});
svg.addEventListener('mouseleave', () => {
  if (focusedId) return;
  _hoverT = setTimeout(unhoverNode, 150);
});

// ── Phase 3：手动删除 + 手动画线（连线模式）──
let linkMode = false, linkSrc = null, _linkAB = null;
function toggleLinkMode() {
  clearFocus();                     // 进入/退出连线都退出聚焦
  linkMode = !linkMode;
  linkSrc = null; _linkAB = null;
  clearLinkHighlight();
  const chooser = document.getElementById('linkChooser');
  if (chooser) chooser.style.display = 'none';
  const btn = document.getElementById('btnLink');
  if (btn) btn.classList.toggle('link-active', linkMode);
  if (linkMode) toast(t('graph.linkHint'));
}
function linkPick(id) {
  linkSrc = id;
  highlightLinkSrc(id);
}
async function onLinkClick(id) {
  if (!linkSrc) { linkPick(id); return; }
  if (id === linkSrc) { linkSrc = null; clearLinkHighlight(); return; }
  _linkAB = { a: linkSrc, b: id };          // a=先点 → 作为先修方向
  const chooser = document.getElementById('linkChooser');
  if (chooser) chooser.style.display = 'flex';
}
function cancelLink() {
  const chooser = document.getElementById('linkChooser');
  if (chooser) chooser.style.display = 'none';
  const reasonEl = document.getElementById('linkReason');
  if (reasonEl) reasonEl.value = '';
  linkMode = false; linkSrc = null; _linkAB = null;
  clearLinkHighlight();
  const btn = document.getElementById('btnLink');
  if (btn) btn.classList.remove('link-active');
}
async function doLink(relation) {
  const { a, b } = _linkAB || {};
  const reasonEl = document.getElementById('linkReason');
  const reason = (reasonEl && reasonEl.value || '').trim();
  const strengthEl = document.getElementById('linkStrength');
  const strength = strengthEl ? strengthEl.value : 'soft';
  if (!reason) { toast(t('graph.linkReasonNeed')); return; }  // G1：溯源强制填写理由
  cancelLink();
  if (!a || !b) return;
  try {
    const resp = await fetch(`/api/graph/concepts/${a}/link?subject=${encodeURIComponent(graphSubject())}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'LearnOS' },
      body: JSON.stringify({ b, relation, reason, strength }),
    });
    const j = await resp.json().catch(() => ({}));
    if (!resp.ok) { toast(j.error || t('graph.linkFail')); return; }
    toast(t('graph.linkOk'));
    draw(); savePositions();
  } catch (e) { toast(t('graph.linkFail')); }
}
function highlightLinkSrc(id) { const el = nodeEls.get(id); if (el) el.circle.classList.add('link-src'); }
function clearLinkHighlight() { if (nodeEls) nodeEls.forEach(el => el.circle.classList.remove('link-src')); }

async function deleteConcept() {
  if (!selectedId) return;
  if (!confirm(t('graph.deleteConfirm'))) return;
  try {
    const resp = await fetch(`/api/graph/concepts/${selectedId}?subject=${encodeURIComponent(graphSubject())}`, {
      method: 'DELETE', headers: { 'X-Requested-With': 'LearnOS' },
    });
    const j = await resp.json().catch(() => ({}));
    if (!resp.ok) { toast(j.error || t('graph.deleteFail')); return; }
    toast(t('graph.deleteOk'));
    document.getElementById('detail').classList.add('hidden');
    document.getElementById('welcome').classList.remove('hidden');
    selectedId = null;
    await loadGraph();
  } catch (e) { toast(t('graph.deleteFail')); }
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && linkMode) toggleLinkMode();
});

// 可拖拽侧边栏：在左侧分隔条上拖动调整 #side 宽度（pointer events + 键盘可达 + 持久化）
(function initSideResizer() {
  const side = document.getElementById('side');
  const rz = document.getElementById('sideResizer');
  if (!side || !rz) return;
  const MIN = 260, MAX = 680, KEY = 'learnos.sideWidth';
  const clamp = w => Math.max(MIN, Math.min(MAX, w));
  function apply(w) {
    const v = clamp(w);
    side.style.width = v + 'px';
    rz.setAttribute('aria-valuemin', String(MIN));
    rz.setAttribute('aria-valuemax', String(MAX));
    rz.setAttribute('aria-valuenow', String(v));
  }
  try { const s = parseInt(localStorage.getItem(KEY), 10); if (s) apply(s); } catch (_) {}
  let dragging = false, startX = 0, startW = 0;
  function onMove(e) { if (!dragging) return; apply(startW + (startX - e.clientX)); }
  function onUp() {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    try { localStorage.setItem(KEY, parseInt(side.style.width, 10)); } catch (_) {}
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
  }
  rz.addEventListener('pointerdown', e => {
    dragging = true;
    startX = e.clientX;
    startW = parseInt(getComputedStyle(side).width, 10) || 360;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    try { rz.setPointerCapture(e.pointerId); } catch (_) {}
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    e.preventDefault();
  });
  rz.addEventListener('keydown', e => {
    let w = parseInt(side.style.width, 10) || 360;
    const step = 16;
    if (e.key === 'ArrowLeft') w += step;
    else if (e.key === 'ArrowRight') w -= step;
    else if (e.key === 'Home') w = MIN;
    else if (e.key === 'End') w = MAX;
    else return;
    apply(w);
    try { localStorage.setItem(KEY, parseInt(side.style.width, 10)); } catch (_) {}
    e.preventDefault();
  });
  window.addEventListener('resize', () => {
    const w = parseInt(side.style.width, 10) || 360;
    const cap = Math.round(window.innerWidth * 0.6);
    if (w > cap) apply(cap);
  });
})();

bootGraph();
