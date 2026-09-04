// ── 概念闪卡（主动回忆）前端 ────────────────────────────
// 卡片 = 正面 cue / 背面 answer，复用 FSRS 调度做间隔复习。
// 与题目复习分离：/api/cards 系列端点，独立卡池统计。

const _cardKindLabels = { qa: () => t('cards.qa'), cloze: () => t('cards.cloze'), note: () => t('cards.note') };

// 概念下拉（含级别>=1 的章/概念）
async function populateCardConcepts() {
  const sel = document.getElementById('cardConcept');
  sel.innerHTML = `<option value="">${t('cards.noConcept')}</option>`;
  try {
    const g = await api('/api/graph/concepts');
    const nodes = (g.nodes || []).filter(n => n.level >= 1);
    nodes.sort((a, b) => a.name.localeCompare(b.name, 'zh'));
    for (const n of nodes) {
      const opt = document.createElement('option');
      opt.value = n.id;
      opt.textContent = n.name;
      sel.appendChild(opt);
    }
  } catch (e) { /* 下拉加载失败不阻塞，可手动填 */ }
}

async function loadCards() {
  let data;
  try { data = await api('/api/cards'); }
  catch (e) { toast(t('cards.loadFail'), 'error'); return; }
  renderCardsStats(data.stats || {});
  renderCardsList(data.items || []);
}

function renderCardsStats(s) {
  const el = document.getElementById('cardsStats');
  if (!el) return;
  el.textContent = `${t('cards.total')} ${s.total||0} · ${t('cards.due')} ${s.due||0} · ${t('cards.learned')} ${s.learned||0}`;
}

function renderCardsList(items) {
  const wrap = document.getElementById('cardsList');
  if (!items.length) {
    wrap.innerHTML = `<div class="card"><p class="hint-text">${t('cards.empty')}</p></div>`;
    return;
  }
  const kindBadge = k => `<span class="tag tag-gray">${(_cardKindLabels[k] ? _cardKindLabels[k]() : k)}</span>`;
  const dueBadge = it => (it.due_date && it.due_date <= new Date().toISOString().slice(0,10))
    ? `<span class="tag tag-blue">${t('cards.dueToday')}</span>` : '';
  // B6 P2-1：熟悉度 badge（4 档词表）
  const famBadge = it => {
    if (!it.familiarity) return '';
    const key = 'fsrs.fam' + it.familiarity[0].toUpperCase() + it.familiarity.slice(1);
    return `<span class="tag ${_famTagClass(it.familiarity)}" title="${escapeHtml(t(key))}（R=${(it.retrievability||0).toFixed(2)}）">${escapeHtml(t(key))}</span>`;
  };
  wrap.innerHTML = items.map(it => `
    <div class="card mb-8">
      <div class="flex-between" style="align-items:flex-start;gap:10px">
        <div style="flex:1">
          <div class="flex gap-8" style="flex-wrap:wrap;align-items:center;margin-bottom:6px">
            ${it.concept_name ? `<span class="tag">🧠 ${escapeHtml(it.concept_name)}</span>` : ''}
            ${kindBadge(it.kind)}
            ${famBadge(it)}
            <span class="text-muted text-sm">${t('cards.reps')} ${it.repetition||0}</span>
            ${dueBadge(it)}
          </div>
          <div class="card-title" style="margin:0">${escapeHtml(it.cue)}</div>
          <p class="text-sm text-muted" style="white-space:pre-wrap">${escapeHtml(it.answer)}</p>
          <div class="text-muted text-sm">${t('cards.nextDue')} ${escapeHtml(it.due_date || '—')}</div>
        </div>
        <button class="btn btn-danger btn-sm" onclick="deleteCard(${it.id})" data-i18n="cards.delete">删除</button>
      </div>
    </div>`).join('');
}

async function deleteCard(id) {
  try { await api(`/api/cards/${id}/delete`, { method: 'POST' }); }
  catch (e) { toast(e.message, 'error'); }
  loadCards();
}

// ── 编辑 / 生成 ──
async function openCardModal(conceptId) {
  await populateCardConcepts();
  const sel = document.getElementById('cardConcept');
  sel.value = conceptId ? String(conceptId) : '';
  document.getElementById('cardCue').value = '';
  document.getElementById('cardAnswer').value = '';
  document.getElementById('cardKind').selectedIndex = 0;
  document.getElementById('cardDrafts').innerHTML = '';
  document.getElementById('cardGenStatus').textContent = '';
  openModal('cardModal');
}

function generateCardDrafts() { openCardModal(); }

// 学习路径 → 一键为该概念出卡
async function makeCardForPath(conceptId) {
  await openCardModal(conceptId);
  doGenerateCardDrafts();
}

// ── 学习路径（按先修链，Phase 3）──
async function loadLearningPath(containerId) {
  const el = document.getElementById(containerId || 'learningPathContent');
  if (!el) return;
  let d;
  try { d = await api('/api/learn/path'); }
  catch (e) { el.innerHTML = `<span class="text-muted">—</span>`; return; }
  let html = '';
  if (d.now) {
    const isPrereq = d.now.reason === 'prerequisite';
    html += `<div style="border:1px solid var(--accent);border-radius:8px;padding:10px 12px;margin-bottom:10px">
      <div class="text-muted" style="font-size:12px">${isPrereq ? t('cards.pathNowPrereq').replace('{c}', escapeHtml(d.now.for||'')) : t('cards.pathNow')}</div>
      <div class="flex gap-8" style="align-items:center;flex-wrap:wrap">
        <b>🧠 ${escapeHtml(d.now.name)}</b>
        <span class="tag ${d.now.mastery < 40 ? 'tag-red' : 'tag-yellow'}">${t('cards.mastery')} ${d.now.mastery}%</span>
        ${d.now.chapter ? `<span class="text-muted">${escapeHtml(d.now.chapter)}</span>` : ''}
        <button class="btn btn-primary btn-sm" onclick="makeCardForPath(${d.now.concept_id})" data-i18n="cards.pathMakeCard">➕ 为此做闪卡</button>
      </div>
    </div>`;
  } else {
    html += `<p>${t('cards.pathAllGood')}</p>`;
  }
  if ((d.ready_weak || []).length) {
    html += `<div class="flex-between" style="margin:6px 0">
      <div class="text-muted">${t('cards.pathWeak').replace('{n}', d.ready_weak.length)}</div>
      <button class="btn btn-primary btn-sm" onclick="batchMakeCards()" data-i18n="cards.batchMake">⚡ 批量出卡</button>
    </div>`;
    html += d.ready_weak.slice(0, 8).map(w => `
      <div class="flex gap-8" style="align-items:center;flex-wrap:wrap;padding:3px 0">
        <span>${escapeHtml(w.name)}</span>
        <span class="tag tag-yellow">${t('cards.mastery')} ${w.mastery}%</span>
        <button class="btn btn-link btn-sm" onclick="makeCardForPath(${w.concept_id})">${t('cards.pathMakeCard')}</button>
      </div>`).join('');
  }
  if ((d.blocked || []).length) {
    html += `<div class="text-muted" style="margin:6px 0">${t('cards.pathBlocked').replace('{n}', d.blocked.length)}</div>`;
    html += d.blocked.slice(0, 5).map(b => `
      <div style="padding:3px 0">${escapeHtml(b.name)} → ${t('cards.pathNeed')} <b>${escapeHtml((b.missing||[]).join('、')||'')}</b></div>`).join('');
  }
  el.innerHTML = html || `<span class="text-muted">—</span>`;
}

async function doGenerateCardDrafts() {
  const conceptId = document.getElementById('cardConcept').value;
  if (!conceptId) { toast(t('cards.noConcept'), 'warn'); return; }
  const statusEl = document.getElementById('cardGenStatus');
  statusEl.textContent = t('cards.generating');
  let drafts;
  try {
    const r = await api('/api/cards/generate', { method: 'POST', body: { concept_id: +conceptId, use_ai: true } });
    drafts = r.drafts || [];
  } catch (e) { statusEl.textContent = ''; toast(e.message || t('cards.genFail'), 'error'); return; }
  statusEl.textContent = '';
  renderCardDrafts(drafts);
}

// C1：按薄弱概念里程碑清单批量出卡（逐概念独立生成，失败概念跳过）
async function batchMakeCards() {
  let d;
  try { d = await api('/api/learn/path'); }
  catch (e) { toast(e.message, 'error'); return; }
  const ids = (d.ready_weak || []).map(w => w.concept_id).filter(Boolean);
  if (!ids.length) { toast(t('cards.pathAllGood'), 'ok'); return; }
  await openCardModal();
  const statusEl = document.getElementById('cardGenStatus');
  statusEl.textContent = t('cards.generating');
  let out;
  try {
    out = await api('/api/cards/generate-batch', { method: 'POST', body: { concept_ids: ids, use_ai: true } });
  } catch (e) { statusEl.textContent = ''; toast(e.message || t('cards.genFail'), 'error'); return; }
  statusEl.textContent = '';
  renderBatchDrafts(out.results || []);
  if ((out.failed || []).length) toast(t('cards.batchPartial').replace('{n}', out.failed.length), 'warn');
}

function renderCardDrafts(drafts) {
  const wrap = document.getElementById('cardDrafts');
  if (!drafts.length) { wrap.innerHTML = `<p class="hint-text">${t('cards.genFail')}</p>`; return; }
  wrap.innerHTML = `<div class="card"><div class="card-title" style="font-size:13px">${t('cards.draftPick')}</div>` +
    drafts.map((d, i) => `
      <div class="card mt-8" style="cursor:pointer" onclick="pickCardDraft(${i})">
        <div class="text-sm"><b>${escapeHtml(d.cue)}</b></div>
        <div class="text-muted text-sm" style="white-space:pre-wrap">${escapeHtml(d.answer)}</div>
        <div class="text-muted text-sm">[${escapeHtml(d.kind || 'qa')}] — ${t('cards.tapFill')}</div>
      </div>`).join('') + '</div>';
  window._cardDrafts = drafts;
}

// C1：批量草稿按概念分组渲染（组内点击填入，与单概念草稿共用 pickCardDraft）
function renderBatchDrafts(results) {
  const wrap = document.getElementById('cardDrafts');
  if (!results.length) { wrap.innerHTML = `<p class="hint-text">${t('cards.genFail')}</p>`; return; }
  const flat = [];
  let html = `<div class="card"><div class="card-title" style="font-size:13px">${t('cards.draftPick')}</div>`;
  for (const r of results) {
    html += `<div class="text-muted text-sm" style="margin-top:10px">🧠 ${escapeHtml(r.concept_name)}</div>`;
    for (const d of r.drafts) {
      const i = flat.length;
      flat.push(d);
      html += `
      <div class="card mt-8" style="cursor:pointer" onclick="pickCardDraft(${i})">
        <div class="text-sm"><b>${escapeHtml(d.cue)}</b></div>
        <div class="text-muted text-sm" style="white-space:pre-wrap">${escapeHtml(d.answer)}</div>
        <div class="text-muted text-sm">[${escapeHtml(d.kind || 'qa')}] — ${t('cards.tapFill')}</div>
      </div>`;
    }
  }
  wrap.innerHTML = html + '</div>';
  window._cardDrafts = flat;
}

function pickCardDraft(idx) {
  const d = (window._cardDrafts || [])[idx];
  if (!d) return;
  document.getElementById('cardCue').value = d.cue || '';
  document.getElementById('cardAnswer').value = d.answer || '';
  const k = document.getElementById('cardKind');
  k.value = (d.kind in { qa:1, cloze:1, note:1 }) ? d.kind : 'qa';
  window._cardDrafts = null;
  document.getElementById('cardDrafts').innerHTML = '';
}

async function saveCard() {
  const cue = document.getElementById('cardCue').value.trim();
  const answer = document.getElementById('cardAnswer').value.trim();
  if (!cue) { toast(t('cards.cueRequired'), 'warn'); return; }
  const body = {
    concept_id: +(document.getElementById('cardConcept').value || 0),
    kind: document.getElementById('cardKind').value,
    cue, answer,
    source: 'manual',
  };
  try { await api('/api/cards', { method: 'POST', body }); }
  catch (e) { toast(e.message, 'error'); return; }
  toast(t('cards.saved'), 'ok');
  closeModal('cardModal');
  loadCards();
}

// ── 闪卡复习（翻卡）──
let _cardQueue = [], _cardIdx = 0;

async function startCardFlash() {
  let r;
  try { r = await api('/api/cards/due'); }
  catch (e) { toast(t('cards.loadFail'), 'error'); return; }
  const list = r.items || [];
  if (!list.length) { toast(t('cards.noDue'), 'warn'); return; }
  _cardQueue = list; _cardIdx = 0;
  openModal('cardFlashModal');
  renderCardFlash();
}

function renderCardFlash() {
  const r = _cardQueue[_cardIdx];
  if (!r) { cardFlashFinish(); return; }
  document.getElementById('cardFlashCount').textContent =
    `${_cardIdx + 1}/${_cardQueue.length}`;
  // B6 P2-1：熟悉度 badge（4 档词表）紧贴 reps 后面
  const fam = r.familiarity;
  const famTag = fam ? `<span class="tag ${_famTagClass(fam)}" title="${escapeHtml(t('fsrs.fam' + fam[0].toUpperCase() + fam.slice(1)))}（R=${(r.retrievability||0).toFixed(2)}）">${escapeHtml(t('fsrs.fam' + fam[0].toUpperCase() + fam.slice(1)))}</span>` : '';
  document.getElementById('cardFlashMeta').innerHTML =
    `${r.concept_name ? '🧠 ' + escapeHtml(r.concept_name) + ' · ' : ''}${escapeHtml(t('cards.reps'))} ${Number(r.repetition)||0} ${famTag}`;
  document.getElementById('cardFlashCue').textContent = r.cue || t('cards.noContent');
  document.getElementById('cardFlashAnswer').textContent = r.answer || t('cards.noContent');
  // D2：已有评分记录才可撤销（_cardIdx>0 说明本会话刚评过）
  document.getElementById('cardFlashUndoBtn').classList.toggle('hidden', _cardIdx === 0);
  cardFlashFlip(true);
}

// B6 P2-1：熟悉度档位 → CSS 颜色（hazy 红/shaky 琥珀/familiar 蓝/solid 绿）
function _famTagClass(fam) {
  switch (fam) {
    case 'solid': return 'tag-green';
    case 'familiar': return 'tag-blue';
    case 'shaky': return 'tag-amber';
    case 'hazy':
    default: return 'tag-red';
  }
}

function cardFlashFlip(forceBack) {
  const back = document.getElementById('cardFlashBack');
  if (forceBack === true || !back.classList.contains('hidden')) { back.classList.add('hidden'); return; }
  back.classList.remove('hidden');
}

async function cardFlashRate(rating) {
  const r = _cardQueue[_cardIdx];
  if (!r) return;
  try { await api(`/api/cards/${r.id}/review`, { method: 'POST', body: { rating } }); }
  catch (e) { toast(e.message, 'error'); }
  _cardIdx++;
  renderCardFlash();
}

async function cardFlashUndo() {
  // D2：撤销最近一次评分 → 原子恢复快照，队列退回该卡重看
  const r = _cardQueue[_cardIdx - 1];
  if (!r) return;
  try { await api(`/api/cards/${r.id}/undo`, { method: 'POST' }); }
  catch (e) { toast(e.message, 'error'); return; }
  toast(t('cards.undone'), 'ok');
  _cardIdx--;
  renderCardFlash();
}

function cardFlashFinish() {
  const el = document.getElementById('cardFlashBody');
  el.innerHTML = `<div class="text-center" style="padding:30px 0"><h3 style="margin-bottom:12px">${t('cards.doneTitle')}</h3></div>`;
  loadCards();
  setTimeout(() => { el.innerHTML = ''; closeModal('cardFlashModal'); }, 900);
}

document.addEventListener('keydown', e => {
  if (!document.getElementById('cardFlashModal').classList.contains('active')) return;
  const map = { ArrowLeft: 1, ArrowUp: 2, ArrowDown: 3, ArrowRight: 4 };
  if (map[e.key]) { e.preventDefault(); cardFlashRate(map[e.key]); }
});