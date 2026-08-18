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

// OCR 全文直送导入向导（拍照整卷 → AI 提取，免手动复制）
function ocrToWizard() {
  if (!_ocrResultText) { toast(t('ocr.needRun'), 'error'); return; }
  _matText = _ocrResultText;
  _matFile = null;
  _matUploadPath = null;
  const nameEl = document.getElementById('matFileName');
  if (nameEl) nameEl.textContent = t('mat.fromOcr');
  const card = document.getElementById('matDraft');
  const wizard = document.getElementById('page-rag').querySelector('.card');
  if (wizard) wizard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  toast(t('mat.ocrSent'));
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

// ── 资料导入向导（教材/试卷 → 图谱/题库/试卷，草稿-确认）──
let _matText = '';
let _matDraft = null;

let _matFile = null;        // 已选文件（未上传）
let _matUploadPath = null;  // 上传后的工作区路径
let _matUploadKey = '';     // 路径对应的文件指纹（name+size）

function onMatFilePicked() {
  const f = document.getElementById('matFile').files[0];
  const nameEl = document.getElementById('matFileName');
  if (!f) { _matFile = null; _matText = ''; nameEl.textContent = ''; return; }
  _matFile = f;
  _matText = '';
  nameEl.textContent = f.name + '（' + Math.round(f.size / 1024) + ' KB）';
}

async function matEnsureUploaded() {
  if (!_matFile) return null;
  const key = _matFile.name + ':' + _matFile.size;
  if (_matUploadPath && _matUploadKey === key) return _matUploadPath;
  // 上传原始文件到 uploads/（大小上限由服务端 100MB 把关）
  const r = await fetch('/api/material/upload?name=' + encodeURIComponent(_matFile.name), {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream', 'X-Requested-With': 'LearnOS' },
    body: _matFile,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'upload failed');
  _matUploadPath = data.path;
  _matUploadKey = key;
  return data.path;
}

async function loadMatDocs() {
  const sel = document.getElementById('matDocId');
  if (!sel) return;
  try {
    const r = await api('/api/rag/docs');
    sel.innerHTML = '<option value="">' + t('mat.pickDoc') + '</option>' +
      r.items.map(d => `<option value="${d.id}">${escapeHtml(d.source_path)}</option>`).join('');
  } catch(e) { /* 静默降级 */ }
}

// 分段续跑：每次最多 8 批，SSE 逐批进度，前端累积合并草稿
const MAT_BATCHES_PER_RUN = 8;
let _matSource = null;   // 当前分析来源（text/doc_id/path），续跑时复用
let _matTargets = [];
let _matToBatch = 0;
let _matBatchesTotal = 0;

async function matAnalyze() {
  const targets = [];
  if (document.getElementById('matTconcepts').checked) targets.push('concepts');
  if (document.getElementById('matTquestions').checked) targets.push('questions');
  if (document.getElementById('matTpaper').checked) targets.push('paper');
  if (!targets.length) { toast(t('mat.needTarget'), 'error'); return; }
  let source = {};
  if (_matFile) {
    try { source.path = await matEnsureUploaded(); }
    catch(e) { toast(e.message, 'error'); return; }
  } else if (_matText) {
    source.text = _matText;
  } else {
    const docId = document.getElementById('matDocId').value;
    if (docId) source.doc_id = parseInt(docId, 10);
    else { toast(t('mat.needSource'), 'error'); return; }
  }
  // 首轮成本确认（按 32k 上下文粗估）
  if (source.text && _matText.length > 120000) {
    const est = Math.ceil(_matText.length / 14000) * targets.length;
    if (est > 15 && !confirm(t('mat.confirmCalls').replace('{n}', est))) return;
  }
  _matSource = source;
  _matTargets = targets;
  _matDraft = null;
  _matToBatch = 0;
  _matBatchesTotal = 0;
  document.getElementById('matDraft').innerHTML = '';
  await matRunAnalysis(0);
}

async function matContinue() {
  if (!_matSource || _matToBatch >= _matBatchesTotal) return;
  await matRunAnalysis(_matToBatch);
}

async function matRunAnalysis(fromBatch) {
  const status = document.getElementById('matStatus');
  const btn = document.getElementById('matAnalyzeBtn');
  btn.disabled = true;
  status.textContent = t('mat.analyzing');
  const body = { ..._matSource, targets: _matTargets, from_batch: fromBatch, max_batches: MAT_BATCHES_PER_RUN };
  try {
    const r = await fetch('/api/material/analyze' + withSubject(''), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'LearnOS', 'Accept': 'text/event-stream' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || t('msg.requestFail'));
    }
    const ctype = r.headers.get('Content-Type') || '';
    let result;
    if (ctype.includes('text/event-stream')) {
      result = await matConsumeSSE(r, status);
    } else {
      result = await r.json();
      status.textContent = '';
    }
    if (!result || result.error) throw new Error((result && result.error) || 'aborted');
    _matDraft = _matDraft ? matMergeDrafts(_matDraft, result) : result;
    _matToBatch = result.to_batch;
    _matBatchesTotal = result.batches_total;
    renderMatDraft(_matDraft, { toBatch: _matToBatch, total: _matBatchesTotal });
    const more = _matToBatch < _matBatchesTotal;
    status.textContent = t('mat.analyzed')
      .replace('{b}', _matToBatch + '/' + _matBatchesTotal)
      .replace('{s}', result.source === 'ai' ? 'AI' : t('mat.heuristic')) +
      (result.ai_calls ? t('mat.callsInfo').replace('{n}', result.ai_calls) : '') +
      (more ? '' : ' · ' + t('mat.allCovered'));
  } catch(e) {
    status.textContent = '';
    toast(e.message, 'error');
  }
  btn.disabled = false;
}

async function matConsumeSSE(resp, statusEl) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '', finalResult = null;
  outer: while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split('\n\n');
    buffer = events.pop();
    for (const evt of events) {
      const m = evt.match(/^event: (.+)$/m), dm = evt.match(/^data: (.+)$/m);
      if (!dm) continue;
      const type = m ? m[1] : 'message';
      const payload = JSON.parse(dm[1]);
      if (type === 'start') {
        _matBatchesTotal = payload.batches_total;
        statusEl.textContent = t('mat.progress').replace('{d}', 0).replace('{n}', 0);
      } else if (type === 'progress') {
        statusEl.textContent = t('mat.progress')
          .replace('{d}', payload.done).replace('{n}', payload.total) +
          ' · ' + t('mat.batchOf').replace('{b}', payload.batch);
      } else if (type === 'done') {
        finalResult = payload;
        break outer;
      } else if (type === 'error') {
        throw new Error(payload.error || 'analyze failed');
      }
    }
  }
  return finalResult;
}

function matMergeDrafts(a, b) {
  // 跨段累积：概念按名合并，题目按题干去重，试卷题目顺序拼接
  const out = { ...b, draft: {} };
  const da = a.draft || {}, db = b.draft || {};
  if (da.concepts || db.concepts) {
    const chapters = [], seenCh = new Set();
    for (const ch of [...(da.concepts?.chapters || []), ...(db.concepts?.chapters || [])]) {
      if (ch.name && !seenCh.has(ch.name)) { seenCh.add(ch.name); chapters.push(ch); }
    }
    const concepts = [], seenCp = new Map();
    for (const c of [...(da.concepts?.concepts || []), ...(db.concepts?.concepts || [])]) {
      if (!seenCp.has(c.name)) {
        seenCp.set(c.name, { ...c, related: [...(c.related || [])] });
        concepts.push(seenCp.get(c.name));
      } else {
        const merged = seenCp.get(c.name);
        for (const r of (c.related || [])) if (!merged.related.includes(r)) merged.related.push(r);
      }
    }
    out.draft.concepts = { chapters, concepts };
  }
  if (da.questions || db.questions) {
    const seen = new Set();
    out.draft.questions = [...(da.questions || []), ...(db.questions || [])]
      .filter(q => { const k = `${q.type || ''}|${q.stem || ''}`; if (seen.has(k)) return false; seen.add(k); return true; });
  }
  if ('paper' in da || 'paper' in db) {
    if (da.paper && db.paper) {
      out.draft.paper = { name: da.paper.name || db.paper.name,
        questions: [...da.paper.questions, ...db.paper.questions] };
    } else out.draft.paper = db.paper || da.paper;
  }
  out.warnings = [...(a.warnings || []), ...(b.warnings || [])];
  return out;
}

function renderMatDraft(r, cov) {
  const el = document.getElementById('matDraft');
  const d = r.draft || {};
  const warns = (r.warnings || []).map(w => `<p class="hint-text" style="color:var(--warning)">${escapeHtml(w)}</p>`).join('');
  if (r.truncated) warns += `<p class="hint-text" style="color:var(--warning)">${t('mat.truncated')}</p>`;
  let html = warns;
  const cp = d.concepts;
  if (cp && (cp.chapters?.length || cp.concepts?.length)) {
    const byCh = {};
    (cp.concepts || []).forEach((c, i) => {
      const key = c.chapter || t('mat.noChapter');
      (byCh[key] = byCh[key] || []).push({ ...c, _i: i });
    });
    html += `<div class="card-title">${t('mat.dConcepts')}
      <label class="text-sm" style="font-weight:400"><input type="checkbox" id="matAllCp" checked onchange="matToggleAll('matCp',this.checked)"> ${t('mat.selectAll')}</label></div>`;
    html += Object.entries(byCh).map(([ch, list]) => `
      <div style="padding:4px 0">
        <div class="text-sm" style="font-weight:600">${escapeHtml(ch)} <span class="tag tag-gray">${list.length}</span></div>
        ${list.map(c => `<label class="text-sm" style="display:block;padding:2px 0 2px 16px">
          <input type="checkbox" class="matCp" data-i="${c._i}" checked> ${escapeHtml(c.name)}
          ${c.related?.length ? `<span class="text-muted">↔ ${escapeHtml(c.related.join('、'))}</span>` : ''}
        </label>`).join('')}
      </div>`).join('');
  }
  const qs = d.questions;
  const _MAT_Q_BADGE = { single: '单选', multiple: '多选', fill: '填空', subjective: '主观题', composite: '大小题' };
  if (qs && qs.length) {
    html += `<div class="card-title mt-12">${t('mat.dQuestions')}
      <label class="text-sm" style="font-weight:400"><input type="checkbox" id="matAllQ" checked onchange="matToggleAll('matQ',this.checked)"> ${t('mat.selectAll')}</label></div>`;
    html += qs.map((q, i) => `
      <label style="display:block;padding:6px 0;border-bottom:1px solid var(--border,#eee)">
        <span class="text-sm"><input type="checkbox" class="matQ" data-i="${i}" checked>
        <span class="tag tag-blue" style="margin-right:6px">${escapeHtml(_MAT_Q_BADGE[q.type] || q.type || '单选')}</span>
        ${escapeHtml((q.stem || '').slice(0, 90))}${(q.stem || '').length > 90 ? '…' : ''}</span>
        <span class="tag tag-gray" style="margin-left:6px">${escapeHtml(q.concept || t('mat.qNoConcept'))}</span>
      </label>`).join('');
  }
  if (targets_has(r, 'paper')) {
    const pp = d.paper;
    if (pp && pp.questions?.length) {
      html += `<div class="card-title mt-12">${t('mat.dPaper')} <span class="tag tag-green">${escapeHtml(pp.name)}</span>
        <label class="text-sm" style="font-weight:400"><input type="checkbox" id="matAllP" checked onchange="matToggleAll('matP',this.checked)"> ${t('mat.selectAll')}</label></div>`;
      html += pp.questions.map((q, i) => `
        <label style="display:block;padding:4px 0;border-bottom:1px solid var(--border,#eee)">
          <span class="text-sm"><input type="checkbox" class="matP" data-i="${i}" checked>
          [${escapeHtml(q.qno || '')}] ${escapeHtml((q.content || '').slice(0, 80))}</span>
          <span class="tag tag-gray" style="margin-left:6px">${escapeHtml(q.topic)}</span>
        </label>`).join('');
    } else {
      html += `<p class="text-sm text-muted mt-12">${t('mat.noPaper')}</p>`;
    }
  }
  if (!html.trim()) { el.innerHTML = `<p class="text-sm text-muted">${t('mat.emptyDraft')}</p>`; return; }
  // 覆盖进度 + 断点续跑按钮（先导入已提取部分，或继续分析剩余批次）
  if (cov && cov.total && cov.toBatch < cov.total) {
    html += `<div class="flex gap-8 mt-12" style="align-items:center">
      <span class="text-sm text-muted">${t('mat.coverage').replace('{d}', cov.toBatch).replace('{n}', cov.total)}</span>
      <button class="btn btn-secondary btn-sm" onclick="matContinue()">${t('mat.continue')}</button>
    </div>`;
  }
  html += `<button class="btn btn-primary mt-12" onclick="matApply()">${t('mat.apply')}</button>`;
  el.innerHTML = html;
}

function targets_has(r, name) {
  return !!(r.draft && Object.prototype.hasOwnProperty.call(r.draft, name));
}

function matToggleAll(cls, checked) {
  document.querySelectorAll('.' + cls).forEach(cb => { cb.checked = checked; });
}

async function matApply() {
  if (!_matDraft) return;
  const d = _matDraft.draft || {};
  const payload = {};
  if (d.concepts) {
    const keep = new Set(Array.from(document.querySelectorAll('.matCp:checked')).map(cb => +cb.dataset.i));
    const concepts = (d.concepts.concepts || []).filter((_, i) => keep.has(i));
    const usedCh = new Set(concepts.map(c => c.chapter).filter(Boolean));
    const chapters = (d.concepts.chapters || []).filter(c => usedCh.has(c.name));
    payload.concepts = { chapters, concepts };
  }
  if (d.questions) {
    const keep = new Set(Array.from(document.querySelectorAll('.matQ:checked')).map(cb => +cb.dataset.i));
    payload.questions = d.questions.filter((_, i) => keep.has(i));
  }
  if (d.paper && d.paper.questions) {
    const keep = new Set(Array.from(document.querySelectorAll('.matP:checked')).map(cb => +cb.dataset.i));
    const questions = d.paper.questions.filter((_, i) => keep.has(i));
    if (questions.length) payload.paper = { name: d.paper.name, questions };
  }
  if (!payload.concepts?.concepts?.length && !payload.concepts?.chapters?.length &&
      !payload.questions?.length && !payload.paper) {
    toast(t('mat.needTarget'), 'error'); return;
  }
  try {
    const r = await api('/api/material/apply', { method: 'POST', body: { draft: payload } });
    const s = r.stats || {};
    toast(t('mat.applied')
      .replace('{c}', s.concepts_added || 0)
      .replace('{q}', s.questions_imported || 0)
      .replace('{p}', s.paper ? s.paper.added : 0));
    document.getElementById('matDraft').innerHTML = '';
    document.getElementById('matStatus').textContent = '';
    _matDraft = null;
    if (s.questions_errors?.length) toast(t('mat.qErrors').replace('{n}', s.questions_errors.length), 'warn');
  } catch(e) { toast(e.message, 'error'); }
}

function examBar(pct, width) {
  const w = width || '100%';
  const color = pct >= 80 ? '#22c55e' : pct >= 60 ? '#f59e0b' : '#ef4444';
  return `<div style="height:8px;background:var(--border);border-radius:4px;overflow:hidden;width:${w}">
    <div style="height:100%;width:${Math.min(100, Math.max(0, pct))}%;background:${color};border-radius:4px"></div></div>`;
}

