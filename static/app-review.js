// 复习：列表 / 评分 / Feynman / 变式 / 打印练习卷 / 闪卡
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
    const resp = await api('/api/reviews' + mode);
    const list = resp.items || resp;  // 兼容旧数组形状
    if (!list.length) {
      el.innerHTML = '<div class="empty"><p>' + t('review.noneToday') + '</p></div>';
      return;
    }
    const today = new Date().toISOString().slice(0, 10);
    const dueCount = list.filter(r => r.due_date <= today).length;
    const capNote = resp.capped
      ? `<p class="hint-text" style="color:var(--warning)">${t('review.capNote').replace('{n}', resp.cap).replace('{m}', resp.total)}</p>` : '';
    document.getElementById('reviewProgress').innerHTML = capNote + `
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
  const resp = await api('/api/reviews');
  const list = resp.items || resp;
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
    t('flash.meta').replace('{c}', escapeHtml(r.course || '')).replace('{t}', escapeHtml(r.topic || '')).replace('{e}', escapeHtml(errLabel(r.error_type || '待诊断')));
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

