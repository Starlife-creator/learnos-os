// 口试：开始 / 对话 / 重置
// ── F1 口试 → 复习卡 ──
let _oralDraft = null;

async function draftOralCard() {
  if (!oralSessionId) return;
  try {
    const r = await api(`/api/oral/${oralSessionId}/draft-card`, { method: 'POST', body: {} });
    _oralDraft = r.draft || {};
    const d = _oralDraft;
    const chatEl = document.getElementById('oralChat');
    chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
      <div class="card-title" style="font-size:14px">${t('draft.cardTitle')}</div>
      <div class="text-sm"><b>${t('draft.title')}</b>${escapeHtml(d.title || '')}</div>
      <div class="text-sm" style="white-space:pre-wrap"><b>${t('draft.content')}</b>${escapeHtml(d.content || '')}</div>
      <div class="text-sm">${t('draft.topicErr').replace('{t}', escapeHtml(d.topic || '')).replace('{e}', escapeHtml(errLabel(d.error_type || '待诊断')))}</div>
      <div class="flex gap-8 mt-8">
        <button class="btn btn-primary btn-sm" onclick="saveOralCard()">${t('draft.saveAs')}</button>
        <button class="btn btn-secondary btn-sm" onclick="this.closest('.bubble').remove()">${t('common.discard')}</button>
      </div>
    </div></div>`;
    chatEl.scrollTop = chatEl.scrollHeight;
  } catch(e) { toast(e.message, 'error'); }
}

async function saveOralCard() {
  if (!_oralDraft) return;
  try {
    const d = _oralDraft;
    await api('/api/problems', { method: 'POST', body: {
      title: d.title || t('oral.cardTitle'), content: d.content || '', topic: d.topic || '',
      error_type: d.error_type || 'concept_misunderstood', my_attempt: d.my_attempt || '',
      tags: d.tags || [],
    }});
    _oralDraft = null;
    toast(t('oral.savedToast'));
  } catch(e) { toast(e.message, 'error'); }
}

// ── 口试 ──
let oralSessionId = null;
let oralTurn = 0;

async function startOral() {
  startOralWith(document.getElementById('oralTopic').value.trim());
}

async function startOralWith(topic) {
  _feynmanMode = false;
  if (!topic) { toast(t('oral.needTopic'), 'error'); return; }
  try {
    const r = await api('/api/oral/start', { method: 'POST', body: { topic } });
    oralSessionId = r.session_id;
    oralTurn = 1;
    document.getElementById('oralStartCard').classList.add('hidden');
    document.getElementById('oralChatCard').classList.remove('hidden');
    document.getElementById('oralTopicDisplay').textContent = t('oral.topicDisplay').replace('{t}', topic);
    document.getElementById('oralTurn').textContent = t('oral.roundOf').replace('{n}', 1).replace('{m}', 5);
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
  div.innerHTML = '<div class="bubble">' + t('oral.thinking') +
    ' <button class="btn btn-secondary btn-sm" id="oralCancelBtn" onclick="oralCancel()">' + t('oral.cancel') + '</button></div>';
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
}

let _oralAbort = null;
function oralCancel() {
  if (_oralAbort) _oralAbort.abort();
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
  _oralAbort = new AbortController();
  try {
    const r = await api('/api/oral/respond', { method: 'POST', body: { session_id: oralSessionId, answer }, signal: _oralAbort.signal });
    const t = document.getElementById('oralThinking');
    if (t) t.remove();
    oralTurn++;
    chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">${escapeHtml(r.reply)}</div></div>`;
    renderMath(chatEl);
    chatEl.scrollTop = chatEl.scrollHeight;
    if (r.finished) {
      document.getElementById('oralTurn').textContent = _feynmanMode ? t('oral.feynDone') : t('oral.ended');
      document.getElementById('oralAnswer').placeholder = t('oral.restartHint');
      if (_feynmanMode) {
        chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
          <button class="btn btn-primary btn-sm" onclick="showFeynmanSelfReview()">${t('oral.selfReviewBtn')}</button>
          <span class="text-sm text-muted">${t('oral.gapHint')}</span>
        </div></div>`;
      } else {
        chatEl.innerHTML += `<div class="chat-msg assistant"><div class="bubble">
          <button class="btn btn-primary btn-sm" onclick="draftOralCard()">${t('oral.draftBtn')}</button>
          <span class="text-sm text-muted">${t('oral.draftHint')}</span>
        </div></div>`;
      }
      chatEl.scrollTop = chatEl.scrollHeight;
    } else {
      document.getElementById('oralTurn').textContent = _feynmanMode ? t('feyn.stepOf').replace('{n}', oralTurn + 1).replace('{m}', 3) : t('oral.roundOf').replace('{n}', oralTurn).replace('{m}', 5);
      document.getElementById('oralAnswer').disabled = false;
      document.getElementById('oralAnswer').focus();
    }
  } catch(e) {
    const box = document.getElementById('oralThinking');
    if (box) box.remove();
    if (e && e.name === 'AbortError') {
      // 用户主动取消：不视为错误，仅提示（局部变量不得遮蔽全局 i18n 函数 t）
      toast(t('oral.cancelled'), 'info');
    } else {
      toast(e.message, 'error');
    }
    document.getElementById('oralAnswer').disabled = false;
  } finally {
    _oralAbort = null;
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

