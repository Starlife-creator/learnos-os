/* 学习台 P0 前端：教材列表 / 渲染 / 目录 / 全文搜索 / AI 助手。
 * 独立页模式（同 concept_map.html）：不加载 app-core.js，工具函数页面自带。
 * 注意：本页 escapeHtml 为本地实现（勿与全局版混淆——concept_map.js 教训）。
 */
'use strict';

// ── 基础工具 ─────────────────────────────────────────────────────────────

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function api(path, opts = {}) {
  const headers = Object.assign({ 'X-Requested-With': 'LearnOS' }, opts.headers || {});
  if (opts.body !== undefined && typeof opts.body === 'string') {
    headers['Content-Type'] = 'application/json';
  }
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  let data = null;
  try { data = await resp.json(); } catch (_) { /* pdf 等非 JSON 响应 */ }
  if (!resp.ok) {
    throw new Error((data && data.error) || ('HTTP ' + resp.status));
  }
  return data;
}

function currentSubject() {
  const qs = new URLSearchParams(location.search);
  return qs.get('subject') || localStorage.getItem('subject') || 'physics';
}

// ── i18n（迷你版：与 app-core 的 lang 约定一致）───────────────────────────

const LOCALES = ['zh-CN', 'en-US'];
let _dict = null;

function currentLang() {
  const saved = localStorage.getItem('lang');
  if (saved && LOCALES.includes(saved)) return saved;
  const nav = (navigator.language || 'zh-CN');
  return nav.startsWith('zh') ? 'zh-CN' : 'en-US';
}

async function initI18n() {
  const lang = currentLang();
  document.documentElement.lang = lang;
  try {
    _dict = await (await fetch('/locale/' + lang + '.json')).json();
  } catch (_) { _dict = {}; }
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const v = _dict[el.getAttribute('data-i18n')];
    if (v != null) el.textContent = v;
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const v = _dict[el.getAttribute('data-i18n-ph')];
    if (v != null) el.setAttribute('placeholder', v);
  });
}

// ── KaTeX 懒加载（vendor 本地资源，离线可用）─────────────────────────────

let _katexLoading = null;
function loadKatex() {
  if (_katexLoading) return _katexLoading;
  _katexLoading = new Promise((resolve, reject) => {
    if (window.renderMathInElement) return resolve();
    const css = document.createElement('link');
    css.rel = 'stylesheet'; css.href = '/vendor/katex.min.css';
    document.head.appendChild(css);
    const s = document.createElement('script');
    s.src = '/vendor/katex.min.js';
    s.onload = () => {
      const ar = document.createElement('script');
      ar.src = '/vendor/auto-render.min.js';
      ar.onload = resolve; ar.onerror = reject;
      document.head.appendChild(ar);
    };
    s.onerror = () => { _katexLoading = null; reject(new Error('KaTeX 加载失败')); };
    document.head.appendChild(s);
  });
  return _katexLoading;
}

function renderMath(el) {
  loadKatex().then(() => {
    if (window.renderMathInElement) {
      renderMathInElement(el, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '$', right: '$', display: false },
          { left: '\\(', right: '\\)', display: false },
          { left: '\\[', right: '\\]', display: true },
        ],
        throwOnError: false,
      });
    }
  }).catch(() => {}); // 公式渲染失败不影响正文
}

// ── 学科上下文 ───────────────────────────────────────────────────────────

let subject = currentSubject();
let curMaterialId = null;

async function initSubjects() {
  const sel = document.getElementById('subjectSel');
  try {
    const data = await api('/api/subjects');
    sel.innerHTML = (data.subjects || []).map(s =>
      `<option value="${esc(s.id)}"${s.id === subject ? ' selected' : ''}>${esc(s.title)}</option>`
    ).join('');
  } catch (_) {
    sel.innerHTML = `<option value="${esc(subject)}">${esc(subject)}</option>`;
  }
  sel.onchange = () => {
    subject = sel.value;
    localStorage.setItem('subject', subject);
    const u = new URL(location.href); u.searchParams.set('subject', subject);
    location.replace(u.toString().replace(/&material=\d+/, ''));
  };
}

// ── 教材列表 ─────────────────────────────────────────────────────────────

const FMT_LABEL = { md: 'md', txt: 'txt', html: 'html', pdf: 'pdf' };

async function loadMaterials() {
  const box = document.getElementById('materialList');
  try {
    const data = await api('/api/learn/materials?subject=' + encodeURIComponent(subject));
    const items = data.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="muted" data-i18n="learn.emptyMaterials"></div>`;
      applyDict(box);
      return;
    }
    box.innerHTML = items.map(m => `
      <div class="mat-item${m.id === curMaterialId ? ' active' : ''}" onclick="openMaterial(${m.id}, '${esc(m.fmt)}')">
        <span class="fmt">${esc(FMT_LABEL[m.fmt] || m.fmt)}</span>
        <span class="name" title="${esc(m.title)}">${esc(m.title)}</span>
        <button class="del" title="删除" onclick="delMaterial(event, ${m.id}, this)">✕</button>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
}

function applyDict(root) {
  root.querySelectorAll('[data-i18n]').forEach(el => {
    const v = _dict && _dict[el.getAttribute('data-i18n')];
    if (v != null) el.textContent = v;
  });
}

async function delMaterial(ev, id, btn) {
  ev.stopPropagation();
  const dictKey = _dict && _dict['learn.confirmDelete'] ? _dict['learn.confirmDelete'] : '确定删除该教材的登记？（磁盘文件不会被删除）';
  if (!confirm(dictKey)) return;
  try {
    await api('/api/learn/materials/' + id, { method: 'DELETE' });
    if (id === curMaterialId) closeReader();
    loadMaterials();
  } catch (e) { alert(e.message); }
}

// ── 打开 / 渲染教材 ──────────────────────────────────────────────────────

async function openMaterial(id, fmt) {
  if (fmt === 'pdf') { openPdf(id); return; }  // P0：pdf 走浏览器原生查看器（P1.5 换 pdf.js）
  try {
    const data = await api('/api/learn/materials/' + id + '/content?subject=' + encodeURIComponent(subject));
    if (data instanceof Blob || !data) return; // pdf 字节流已在新窗口处理
    curMaterialId = id;
    document.getElementById('readerPlaceholder').classList.add('hidden');
    const reader = document.getElementById('reader');
    reader.classList.remove('hidden');
    reader.innerHTML = data.content || '<p></p>';
    reader.dataset.title = data.title || '';
    renderMath(reader);
    buildOutline(reader);
    loadAnnotations();
    document.getElementById('readerScroll').scrollTop = 0;
    loadMaterials(); // 刷新高亮
  } catch (e) { alert(e.message); }
}

// ── P1.5 PDF 查看器（pdf.js + textLayer：划词四连在 PDF 上生效）──────────

let _pdfState = null;      // { doc, id }
let _pdfObserver = null;
let _annos = [];           // 当前教材的批注缓存

function loadPdfJs() {
  if (window.pdfjsLib) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/vendor/pdf.min.js';
    s.onload = () => {
      pdfjsLib.GlobalWorkerOptions.workerSrc = '/vendor/pdf.worker.min.js';
      resolve();
    };
    s.onerror = () => reject(new Error('pdf.js 加载失败（vendor 资源缺失）'));
    document.head.appendChild(s);
  });
}

async function openPdf(id) {   // P1.5：内嵌查看器取代 window.open
  closeReader();
  document.getElementById('readerPlaceholder').classList.add('hidden');
  const pv = document.getElementById('pdfViewer');
  pv.classList.remove('hidden');
  pv.innerHTML = `<div class="loading">${esc(t_('learn.pdfLoading'))}</div>`;
  curMaterialId = id;
  try {
    await loadPdfJs();
    const doc = await pdfjsLib.getDocument(
      '/api/learn/materials/' + id + '/content?subject=' + encodeURIComponent(subject)
    ).promise;
    pv.innerHTML = '';
    _pdfState = { doc, id };
    // P2 工具栏：选择 / 矢量图形批注（箭头·矩形·椭圆·画笔）
    const tb = document.createElement('div');
    tb.className = 'pdf-toolbar';
    tb.innerHTML =
      ['select', 'arrow', 'rect', 'ellipse', 'pen'].map(tName =>
        `<button class="pdf-tool${tName === 'select' ? ' active' : ''}" data-tool="${tName}" ` +
        `onclick="setPdfTool('${tName}')">${esc(t_('learn.tool' + tName.charAt(0).toUpperCase() + tName.slice(1)))}</button>`).join('') +
      ['e11d48', 'f59e0b', '4f7cff'].map((c, i) =>
        `<button class="pdf-color${i === 0 ? ' active' : ''}" style="background:#${c}" ` +
        `onclick="setShapeColor(this,'${c}')"></button>`).join('');
    pv.appendChild(tb);
    for (let i = 1; i <= doc.numPages; i++) {
      const d = document.createElement('div');
      d.className = 'pdf-page';
      d.dataset.page = i;
      d.innerHTML = `<div class="loading">…</div>`;
      pv.appendChild(d);
    }
    // 滚动懒渲染：进入视口附近才渲染 canvas + textLayer
    _pdfObserver = new IntersectionObserver(entries => {
      entries.forEach(en => {
        if (en.isIntersecting) { _pdfObserver.unobserve(en.target); renderPdfPage(en.target); }
      });
    }, { root: document.getElementById('readerScroll'), rootMargin: '400px' });
    pv.querySelectorAll('.pdf-page').forEach(d => _pdfObserver.observe(d));
    loadMaterials(); // 列表高亮
  } catch (e) {
    pv.innerHTML = `<div class="loading">${esc(e.message)}</div>`;
  }
}

async function renderPdfPage(container) {
  if (!container || container.dataset.rendered || !_pdfState) return;
  container.dataset.rendered = '1';
  try {
    const pageNo = +container.dataset.page;
    const page = await _pdfState.doc.getPage(pageNo);
    // 按可视宽度适配缩放（上限 2 倍）
    const availW = document.getElementById('readerScroll').clientWidth - 48;
    const base = page.getViewport({ scale: 1 });
    const scale = Math.min(2, Math.max(0.8, availW / base.width));
    const vp = page.getViewport({ scale });
    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(vp.width * devicePixelRatio);
    canvas.height = Math.floor(vp.height * devicePixelRatio);
    canvas.style.width = vp.width + 'px';
    canvas.style.height = vp.height + 'px';
    container.style.width = vp.width + 'px';
    container.style.height = vp.height + 'px';
    container.querySelector('.loading')?.remove();
    container.appendChild(canvas);
    await page.render({ canvasContext: canvas.getContext('2d'),
                        viewport: vp, transform: devicePixelRatio > 1 ?
                        [devicePixelRatio, 0, 0, devicePixelRatio, 0, 0] : null }).promise;
    const tc = await page.getTextContent();
    const tl = document.createElement('div');
    tl.className = 'textLayer';
    container.appendChild(tl);
    await pdfjsLib.renderTextLayer({ textContent: tc, container: tl, viewport: vp }).promise;
    // 渲染完文本层后补打本页批注（高亮）+ 矢量图形
    _annos.filter(a => a.anchor && a.anchor.page === pageNo)
          .forEach(a => { if (a.kind !== 'shape') tryApplyAnnotation(tl, a); });
    renderSvgOverlay(container);
  } catch (e) { console.warn('PDF 页渲染失败', e); }
}

// ── P2 矢量图形批注（仅 PDF：归一化坐标，缩放不失真）────────────────────

let _pdfTool = 'select';
let _shapeColor = 'e11d48';
const SVG_NS = 'http://www.w3.org/2000/svg';

function setPdfTool(t) {
  _pdfTool = t;
  document.querySelectorAll('.pdf-tool').forEach(b =>
    b.classList.toggle('active', b.dataset.tool === t));
}

function setShapeColor(btn, c) {
  _shapeColor = c;
  document.querySelectorAll('.pdf-color').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

function renderSvgOverlay(pageEl) {
  const pageNo = +pageEl.dataset.page;
  pageEl.querySelector('svg.overlay')?.remove();
  if (!pageEl.querySelector('canvas')) return;   // 页面尚未渲染完
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'overlay');
  const w = pageEl.clientWidth || 1, h = pageEl.clientHeight || 1;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  for (const a of _annos) {
    if (a.kind !== 'shape' || !a.anchor || a.anchor.page !== pageNo) continue;
    let geo;
    try { geo = JSON.parse(a.body); } catch (_) { continue; }
    if (!geo || !geo.type) continue;
    const el = drawShape(svg, geo, w, h);
    el.dataset.annoId = a.id;
    el.addEventListener('click', async ev => {
      ev.stopPropagation();
      if (_pdfTool !== 'select') return;         // 绘制模式下不触发删除
      if (!confirm(t_('learn.hlDelete'))) return;
      try {
        await api('/api/learn/annotations/' + a.id, { method: 'DELETE' });
        _annos = _annos.filter(x => x.id !== a.id);
        renderSvgOverlay(pageEl);
      } catch (err) { alert(err.message); }
    });
  }
  pageEl.appendChild(svg);
}

function drawShape(svg, geo, w, h) {
  const px = x => (x || 0) * w, py = y => (y || 0) * h;
  const color = '#' + (geo.color || 'e11d48');
  let el;
  if (geo.type === 'pen') {
    el = document.createElementNS(SVG_NS, 'polyline');
    el.setAttribute('points', (geo.points || []).map(p => `${px(p[0])},${py(p[1])}`).join(' '));
    el.setAttribute('fill', 'none');
  } else {
    const x0 = px(geo.x0), y0 = py(geo.y0), x1 = px(geo.x1), y1 = py(geo.y1);
    if (geo.type === 'arrow' || geo.type === 'line') {
      el = document.createElementNS(SVG_NS, 'line');
      el.setAttribute('x1', x0); el.setAttribute('y1', y0);
      el.setAttribute('x2', x1); el.setAttribute('y2', y1);
      if (geo.type === 'arrow') {
        const ang = Math.atan2(y1 - y0, x1 - x0), hl = 10;
        const head = document.createElementNS(SVG_NS, 'polygon');
        head.setAttribute('points',
          `${x1},${y1} ${x1 - hl * Math.cos(ang - 0.45)},${y1 - hl * Math.sin(ang - 0.45)} ` +
          `${x1 - hl * Math.cos(ang + 0.45)},${y1 - hl * Math.sin(ang + 0.45)}`);
        head.setAttribute('fill', color);
        head.setAttribute('data-shape', 'head');
        finishShapeAttr(head, geo);
        svg.appendChild(head);
      }
    } else if (geo.type === 'rect') {
      el = document.createElementNS(SVG_NS, 'rect');
      el.setAttribute('x', Math.min(x0, x1)); el.setAttribute('y', Math.min(y0, y1));
      el.setAttribute('width', Math.abs(x1 - x0)); el.setAttribute('height', Math.abs(y1 - y0));
      el.setAttribute('fill', 'none');
    } else { // ellipse
      el = document.createElementNS(SVG_NS, 'ellipse');
      el.setAttribute('cx', (x0 + x1) / 2); el.setAttribute('cy', (y0 + y1) / 2);
      el.setAttribute('rx', Math.abs(x1 - x0) / 2); el.setAttribute('ry', Math.abs(y1 - y0) / 2);
      el.setAttribute('fill', 'none');
    }
  }
  finishShapeAttr(el, geo);
  svg.appendChild(el);
  return el;
}

function finishShapeAttr(el, geo) {
  el.setAttribute('stroke', '#' + (geo.color || 'e11d48'));
  el.setAttribute('stroke-width', '2');
  el.setAttribute('data-shape', geo.type);
}

let _draw = null;   // {pageEl, geo, previewEls}

function pdfMouseDown(e) {
  if (_pdfTool === 'select' || e.button !== 0) return;
  const pageEl = e.target.closest('.pdf-page');
  if (!pageEl || !pageEl.dataset.rendered) return;
  e.preventDefault();
  const rect = pageEl.getBoundingClientRect();
  _draw = {
    pageEl,
    geo: { type: _pdfTool, color: _shapeColor,
           x0: (e.clientX - rect.left) / rect.width, y0: (e.clientY - rect.top) / rect.height,
           x1: 0, y1: 0, points: [] },
  };
  if (_pdfTool === 'pen') _draw.geo.points.push([_draw.geo.x0, _draw.geo.y0]);
  _draw.preview = drawShape(getOverlay(pageEl), _draw.geo,
                            pageEl.clientWidth, pageEl.clientHeight);
}

function pdfMouseMove(e) {
  if (!_draw) return;
  const pageEl = _draw.pageEl;
  const rect = pageEl.getBoundingClientRect();
  const nx = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
  const ny = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
  const g = _draw.geo;
  if (_pdfTool === 'pen') {
    g.points.push([nx, ny]);
    _draw.preview.setAttribute('points',
      g.points.map(p => `${p[0] * rect.width},${p[1] * rect.height}`).join(' '));
  } else {
    g.x1 = nx; g.y1 = ny;
    renderSvgPreview(pageEl, g);
  }
}

function renderSvgPreview(pageEl, g) {
  const svg = getOverlay(pageEl);
  svg.querySelectorAll('[data-preview]').forEach(el => el.remove());
  const clone = JSON.parse(JSON.stringify(g));
  const el = drawShape(svg, clone, pageEl.clientWidth, pageEl.clientHeight);
  el.setAttribute('data-preview', '1');
}

function getOverlay(pageEl) {
  let svg = pageEl.querySelector('svg.overlay');
  if (!svg) {
    svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'overlay');
    svg.setAttribute('viewBox', `0 0 ${pageEl.clientWidth} ${pageEl.clientHeight}`);
    pageEl.appendChild(svg);
  }
  return svg;
}

async function pdfMouseUp(e) {
  if (!_draw) return;
  const { pageEl, geo } = _draw;
  _draw = null;
  pageEl.querySelectorAll('[data-preview]').forEach(el => el.remove());
  const done = geo.type === 'pen'
    ? geo.points.length > 3
    : geo.x1 !== undefined && (Math.abs(geo.x1 - geo.x0) * pageEl.clientWidth +
        Math.abs((geo.y1 || 0) - geo.y0) * pageEl.clientHeight) > 6;
  if (!done) { renderSvgOverlay(pageEl); return; }   // 误触（位移过小）丢弃
  try {
    const data = await api('/api/learn/materials/' + curMaterialId + '/annotations', {
      method: 'POST',
      body: JSON.stringify({ kind: 'shape', anchor: { page: +pageEl.dataset.page },
                             body: JSON.stringify(geo) }),
    });
    _annos.push({ id: data.id, kind: 'shape', anchor: { page: +pageEl.dataset.page }, body: JSON.stringify(geo) });
    renderSvgOverlay(pageEl);
  } catch (err) { alert(err.message); }
}

function buildOutline(reader) {
  const box = document.getElementById('outline');
  const heads = reader.querySelectorAll('h1, h2, h3');
  if (!heads.length) {
    box.innerHTML = `<div class="muted">${esc(t_('learn.outlineEmpty'))}</div>`;
    return;
  }
  heads.forEach((h, i) => { h.id = 'sec-' + i; });
  box.innerHTML = Array.from(heads).map(h =>
    `<a class="ol-item ol-${h.tagName.toLowerCase()}" href="#${h.id}">${esc(h.textContent)}</a>`
  ).join('');
}

function closeReader() {
  curMaterialId = null;
  _annos = [];
  _draw = null;
  document.getElementById('reader').classList.add('hidden');
  const pv = document.getElementById('pdfViewer');
  pv.classList.add('hidden');
  pv.innerHTML = '';
  if (_pdfObserver) { _pdfObserver.disconnect(); _pdfObserver = null; }
  if (_pdfState) { try { _pdfState.doc.destroy(); } catch (_) {} _pdfState = null; }
  document.getElementById('readerPlaceholder').classList.remove('hidden');
  document.getElementById('outline').innerHTML =
    `<div class="muted">${esc(t_('learn.outlineEmpty'))}</div>`;
}

function t_(key) {
  return (_dict && _dict[key] != null) ? _dict[key] : key;
}

// ── 上传 / 新建 ──────────────────────────────────────────────────────────

function pickUpload() {
  document.getElementById('fileInput').click();
}

async function handleFile(file) {
  if (!file) return;
  try {
    // 1) 原始字节流上传 → uploads/
    const resp = await fetch('/api/material/upload?name=' + encodeURIComponent(file.name), {
      method: 'POST',
      headers: { 'X-Requested-With': 'LearnOS' },
      body: file,
    });
    const up = await resp.json();
    if (!resp.ok) throw new Error(up.error || ('HTTP ' + resp.status));
    // 2) 登记为教材
    const reg = await api('/api/learn/materials', {
      method: 'POST', body: JSON.stringify({ path: up.path, subject }),
    });
    await loadMaterials();
    if ((up.path || '').toLowerCase().endsWith('.pdf')) openPdf(reg.id);
    else openMaterial(reg.id, learn.fmtHint(up.path));
  } catch (e) { alert(e.message); }
}

function fmtHint(path) {
  const s = (path || '').toLowerCase();
  if (s.endsWith('.pdf')) return 'pdf';
  if (s.endsWith('.html') || s.endsWith('.htm')) return 'html';
  if (s.endsWith('.txt')) return 'txt';
  return 'md';
}

function toggleNewForm() {
  document.getElementById('newMatForm').classList.toggle('open');
}

async function submitNew(ev) {
  ev.preventDefault();
  const title = document.getElementById('newTitle').value.trim();
  const content = document.getElementById('newContent').value;
  if (!title) { alert(t_('learn.newTitlePh')); return false; }
  try {
    const reg = await api('/api/learn/materials', {
      method: 'POST', body: JSON.stringify({ title, content, subject }),
    });
    toggleNewForm();
    document.getElementById('newTitle').value = '';
    document.getElementById('newContent').value = '';
    await loadMaterials();
    openMaterial(reg.id);
  } catch (e) { alert(e.message); }
  return false;
}

// ── 全文搜索 ─────────────────────────────────────────────────────────────

let _searchTimer = null;

function onSearchInput() {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(runSearch, 350);
}

async function runSearch() {
  const q = document.getElementById('searchBox').value.trim();
  const box = document.getElementById('searchResults');
  if (q.length < 2) { box.innerHTML = ''; return; }
  try {
    const data = await api('/api/learn/search?q=' + encodeURIComponent(q) +
      '&k=8&subject=' + encodeURIComponent(subject));
    box.innerHTML = (data.items || []).map(r => `
      <div class="sr-item" onclick="openMaterial(${r.material_id})">
        <div class="sr-title">${esc(r.title)}</div>
        <div class="sr-snippet">…${esc(r.snippet)}…</div>
      </div>`).join('') ||
      `<div class="muted">${esc(t_('learn.noResult'))}</div>`;
  } catch (e) {
    box.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
}

// ── AI 助手 ──────────────────────────────────────────────────────────────

let _asking = false;

async function ask() {
  if (_asking) return;
  const input = document.getElementById('askInput');
  const q = input.value.trim();
  if (!q) return;
  input.value = '';
  addMsg('user', esc(q));
  _asking = true;
  const thinking = addMsg('ai', '<em>…</em>');
  try {
    const body = { question: q, subject };
    if (curMaterialId) body.material_id = curMaterialId;
    const data = await api('/api/learn/ask', { method: 'POST', body: JSON.stringify(body) });
    thinking.innerHTML = esc(data.answer || '');
    renderMath(thinking);
  } catch (e) {
    thinking.classList.add('err');
    thinking.textContent = e.message;
  } finally {
    _asking = false;
    const box = document.getElementById('chatBox');
    box.scrollTop = box.scrollHeight;
  }
}

function addMsg(kind, html) {
  const div = document.createElement('div');
  div.className = 'msg ' + kind;
  div.innerHTML = html;
  document.getElementById('chatBox').appendChild(div);
  document.getElementById('chatBox').scrollTop = document.getElementById('chatBox').scrollHeight;
  return div;
}

// ── P0.5 划词四连：选区弹层 + 批注锚定 ──────────────────────────────────

const normTxt = s => String(s || '').replace(/\s+/g, ' ').trim();

function currentAnchor() {
  const sel = getSelection();
  if (!sel.rangeCount || sel.isCollapsed) return null;
  const r = sel.getRangeAt(0);
  const quote = normTxt(sel.toString());
  if (!quote || quote.length > 300) return null;
  const before = r.startContainer.nodeType === 3 ? r.startContainer.nodeValue.slice(0, r.startOffset) : '';
  const after = r.endContainer.nodeType === 3 ? r.endContainer.nodeValue.slice(r.endOffset) : '';
  const anchor = {
    prefix: normTxt(before).slice(-20),
    quote: quote,
    suffix: normTxt(after).slice(0, 20),
  };
  // PDF 选区：记录页码（批注按 page+quote 锚定到 textLayer）
  const startEl = (r.startContainer.nodeType === 3 ? r.startContainer.parentElement : r.startContainer);
  const pageEl = startEl && startEl.closest ? startEl.closest('.pdf-page') : null;
  if (pageEl) anchor.page = +pageEl.dataset.page;
  return anchor;
}

function hideSelPopup() { document.getElementById('selPopup').classList.add('hidden'); }

document.addEventListener('mouseup', e => {
  if (e.target.closest('#selPopup')) return; // 点弹层按钮本身不隐藏
  setTimeout(() => {
    const sel = getSelection();
    const reader = document.getElementById('reader');
    const pdfViewer = document.getElementById('pdfViewer');
    const node = sel.anchorNode;
    if (sel.isCollapsed || !(reader.contains(node) || pdfViewer.contains(node))) {
      hideSelPopup(); return;
    }
    _popupAnchor = currentAnchor();
    if (!_popupAnchor) { hideSelPopup(); return; }
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const pop = document.getElementById('selPopup');
    pop.classList.remove('hidden');
    pop.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - 280)) + 'px';
    pop.style.top = Math.min(rect.bottom + 6, window.innerHeight - 48) + 'px';
  }, 0);
});
document.addEventListener('mousedown', e => {
  if (!e.target.closest('#selPopup')) hideSelPopup();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideSelPopup(); });

let _popupAnchor = null;

async function saveAnnotation(kind, body) {
  if (!_popupAnchor || !curMaterialId) return;
  try {
    await api('/api/learn/materials/' + curMaterialId + '/annotations', {
      method: 'POST',
      body: JSON.stringify({ kind, anchor: _popupAnchor, body: body || '' }),
    });
    loadAnnotations();
    addMsg('ai', `<span style="color:var(--green)">✓ ${esc(t_('learn.saved'))}</span>`);
  } catch (err) { alert(err.message); }
}

function actHighlight() { hideSelPopup(); saveAnnotation('highlight', ''); }
function actNote() {
  hideSelPopup();
  const body = prompt(t_('learn.notePh'));
  if (body && body.trim()) saveAnnotation('note', body.trim());
}

async function actExplain() {
  hideSelPopup();
  const quote = _popupAnchor && _popupAnchor.quote;
  if (!quote) return;
  addMsg('user', esc(t_('learn.actExplain') + '：') + '<em>' + esc(quote.slice(0, 80)) + '…</em>');
  const thinking = addMsg('ai', '<em>…</em>');
  try {
    const data = await api('/api/learn/ask', {
      method: 'POST',
      body: JSON.stringify({
        question: t_('learn.explainAsk') + '\n「' + quote + '」',
        subject, material_id: curMaterialId,
      }),
    });
    thinking.innerHTML = esc(data.answer || '');
    renderMath(thinking);
  } catch (err) { thinking.classList.add('err'); thinking.textContent = err.message; }
}

let _draftCards = [];

async function actCards() {
  hideSelPopup();
  const quote = _popupAnchor && _popupAnchor.quote;
  if (!quote) return;
  const thinking = addMsg('ai', '<em>…</em>');
  try {
    const data = await api('/api/material/cards', {
      method: 'POST',
      body: JSON.stringify({ text: quote, subject }),
    });
    _draftCards = (data.cards || []).filter(c => c.question && c.answer);
    if (!_draftCards.length) { thinking.textContent = t_('learn.noResult'); return; }
    let html = '<b>' + esc(t_('learn.cardsDraft')) + '</b>';
    _draftCards.forEach((c, i) => {
      html += `<div style="margin-top:6px"><b>${i + 1}. Q:</b> ${esc(c.question)}<br>` +
              `<b>A:</b> ${esc(c.answer)}</div>`;
    });
    html += `<button class="btn btn-primary" style="margin-top:8px" ` +
            `onclick="applyDraftCards(this)">${esc(t_('learn.cardsApply'))}</button>`;
    thinking.innerHTML = html;
  } catch (err) { thinking.classList.add('err'); thinking.textContent = err.message; }
}

async function applyDraftCards(btn) {
  btn.disabled = true;
  try {
    const data = await api('/api/learn/cards/apply', {
      method: 'POST',
      body: JSON.stringify({ cards: _draftCards, subject }),
    });
    btn.parentElement.querySelector('button').remove();
    addMsg('ai', `<span style="color:var(--green)">✓ ${esc(t_('learn.cardsApplied'))}: ${data.added}</span>`);
  } catch (err) { btn.disabled = false; alert(err.message); }
}

function actCloze() {
  hideSelPopup();
  const quote = _popupAnchor && _popupAnchor.quote;
  if (!quote) return;
  const tokens = (quote.match(/[A-Za-z]{3,}|[\u4e00-\u9fa5]{2,}|\d+(?:\.\d+)?/g) || [])
    .sort((a, b) => b.length - a.length);
  const kw = tokens[0];
  if (!kw) { alert(t_('learn.noResult')); return; }
  const stem = quote.replace(kw, '___');
  const msg = t_('learn.clozeConfirm')
    .replace('%s', stem).replace('%a', kw);
  if (!confirm(msg)) return;
  api('/api/bank/import', {
    method: 'POST',
    body: JSON.stringify({ questions: [{ type: 'fill', stem, answer: kw }], subject }),
  }).then(() => addMsg('ai', `<span style="color:var(--green)">✓ ${esc(t_('learn.clozeDone'))}</span>`))
    .catch(err => alert(err.message));
}

// ── 批注渲染：三段锚 → 文本节点定位 ─────────────────────────────────────

function _escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

async function loadAnnotations() {
  const reader = document.getElementById('reader');
  reader.querySelectorAll('mark.hl').forEach(unwrapMark);
  document.querySelectorAll('#pdfViewer mark.hl').forEach(unwrapMark);
  if (!curMaterialId) { _annos = []; return; }
  try {
    _annos = (await api('/api/learn/materials/' + curMaterialId + '/annotations')).items || [];
  } catch (_) { _annos = []; return; }
  // md/html 批注立即应用；PDF 页锚批注在对应页 textLayer 渲染完成时补打（renderPdfPage）
  for (const a of _annos) {
    if (a.anchor && a.anchor.page) continue;
    tryApplyAnnotation(reader, a);
  }
}

function unwrapMark(mark) {
  const parent = mark.parentNode;
  if (!parent) return;
  while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
  parent.removeChild(mark);
  parent.normalize();
}

function tryApplyAnnotation(root, anno) {
  const a = anno.anchor || {};
  const q = normTxt(a.quote);
  if (!q) return false;
  const re = new RegExp(q.split(/\s+/).map(_escapeRe).join('\\s+'));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: n => (n.parentNode.closest && n.parentNode.closest('mark.hl'))
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const parts = [];
  let buf = '', n;
  while ((n = walker.nextNode())) {
    parts.push({ node: n, start: buf.length, end: buf.length + n.nodeValue.length });
    buf += n.nodeValue;
  }
  const m = re.exec(buf);
  if (!m) return false;
  const s = m.index, e = s + m[0].length;
  for (const p of parts) {
    if (p.end <= s || p.start >= e) continue;
    const ns = Math.max(s, p.start) - p.start;
    const ne = Math.min(e, p.end) - p.start;
    const range = document.createRange();
    range.setStart(p.node, ns);
    range.setEnd(p.node, ne);
    const mark = document.createElement('mark');
    mark.className = 'hl' + (anno.kind === 'note' ? ' hl-note' : '');
    mark.dataset.annoId = anno.id;
    if (anno.kind === 'note' && anno.body) mark.title = anno.body;
    try { range.surroundContents(mark); }
    catch (_) {
      const frag = range.extractContents();
      mark.appendChild(frag);
      range.insertNode(mark);
    }
  }
  return true;
}

// ── P1 AI 整章生成（草稿回填，确认后才保存）─────────────────────────────

async function aiGenerate() {
  const title = document.getElementById('newTitle').value.trim();
  const outline = document.getElementById('newContent').value.trim();
  if (!title || !outline) { alert(t_('learn.aiGenHint')); return; }
  const box = document.getElementById('newContent');
  box.value = t_('learn.aiGenWorking');
  try {
    const data = await api('/api/learn/generate', {
      method: 'POST', body: JSON.stringify({ title, outline, subject }),
    });
    box.value = data.draft || '';
  } catch (err) { alert(err.message); box.value = outline; }
}

// ── P2 今日回看：旧批注浮出（最久未看优先）───────────────────────────────

async function loadTodayReview() {
  const box = document.getElementById('todayReview');
  try {
    const data = await api('/api/learn/review-today?subject=' +
      encodeURIComponent(subject) + '&k=5');
    const items = data.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="col-head">${esc(t_('learn.todayReview'))}</div>` +
        `<div class="muted">${esc(t_('learn.noReview'))}</div>`;
      return;
    }
    box.innerHTML = `<div class="col-head">${esc(t_('learn.todayReview'))}</div>` +
      items.map(r => {
        const q = normTxt((r.anchor && r.anchor.quote) || r.body || '').slice(0, 64);
        return `<div class="sr-item" onclick="openMaterial(${r.material_id},'${esc(r.material_fmt)}')">` +
          `<span class="tr-kind">${r.kind === 'note' ? '📝' : '✏️'}</span>` +
          `<div class="sr-snippet">${esc(q)}…</div></div>`;
      }).join('');
  } catch (_) { box.innerHTML = ''; }
}

// ── 启动 ─────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initI18n().then(initSubjects).then(loadMaterials).then(loadTodayReview).catch(() => {});
  document.getElementById('pdfViewer').addEventListener('mousedown', pdfMouseDown);
  document.getElementById('pdfViewer').addEventListener('mousemove', pdfMouseMove);
  document.getElementById('pdfViewer').addEventListener('mouseup', pdfMouseUp);
  const fi = document.getElementById('fileInput');
  fi.addEventListener('change', () => { handleFile(fi.files[0]); fi.value = ''; });
  document.getElementById('searchBox').addEventListener('input', onSearchInput);
  document.getElementById('searchBox').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); runSearch(); }
  });
  document.getElementById('askInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ask(); }
  });
  document.getElementById('reader').addEventListener('click', e => {
    const mark = e.target.closest('mark.hl');
    if (!mark || !mark.dataset.annoId) return;
    if (!confirm(t_('learn.hlDelete'))) return;
    api('/api/learn/annotations/' + mark.dataset.annoId, { method: 'DELETE' })
      .then(loadAnnotations).catch(err => alert(err.message));
  });
});
