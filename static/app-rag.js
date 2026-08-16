// RAG 教材库 / 试卷 OCR
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
    const undoLink = document.createElement('a');
    undoLink.href = '#';
    undoLink.style.cssText = 'color:#fff;text-decoration:underline;cursor:pointer;margin-left:8px';
    undoLink.textContent = t('undo');
    undoLink.addEventListener('click', async (e) => {
      e.preventDefault();
      undoLink.remove();
      try {
        await api(`/api/rag/doc/${id}/restore`, { method: 'POST' });
        toast(t('rag.restored'));
        loadRagDocs();
      } catch(err) { toast(err.message, 'error'); }
    });
    const el = document.createElement('div');
    el.className = 'toast error';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.textContent = t('rag.removed');
    el.appendChild(undoLink);
    document.body.appendChild(el);
    setTimeout(() => { el.remove(); }, 5000);
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

