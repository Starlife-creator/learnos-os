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
  wrap.innerHTML = items.map(it => `
    <div class="card mb-8">
      <div class="flex-between" style="align-items:flex-start;gap:10px">
        <div style="flex:1">
          <div class="flex gap-8" style="flex-wrap:wrap;align-items:center;margin-bottom:6px">
            ${it.concept_name ? `<span class="tag">🧠 ${escapeHtml(it.concept_name)}</span>` : ''}
            ${kindBadge(it.kind)}
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
    html += `<div class="text-muted" style="margin:6px 0">${t('cards.pathWeak').replace('{n}', d.ready_weak.length)}</div>`;
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
  document.getElementById('cardFlashMeta').textContent =
    `${r.concept_name ? '🧠 ' + r.concept_name + ' · ' : ''}${t('cards.reps')} ${r.repetition||0}`;
  document.getElementById('cardFlashCue').textContent = r.cue || t('cards.noContent');
  document.getElementById('cardFlashAnswer').textContent = r.answer || t('cards.noContent');
  cardFlashFlip(true);
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