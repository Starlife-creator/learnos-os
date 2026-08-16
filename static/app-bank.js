// 题库：列表 / 练习 / 导入
// ── 题库 ──────────────────────────────────────────────────
let _bankUnits = [];
let _bankItems = [];

async function loadBankUnits() {
  const sel = document.getElementById('bankUnit');
  if (!sel) return;
  const prev = sel.value;
  try {
    const data = await api('/api/bank/units');
    _bankUnits = data.units || [];
    sel.innerHTML = '<option value="">' + t('bank.allUnits') + '</option>' +
      _bankUnits.map(u => `<option value="${escapeHtml(u.unit)}">${escapeHtml(u.unit)}（${u.count}题，已掌握${u.done}）</option>`).join('');
    sel.value = prev;
  } catch (e) { /* 题库不可用时保持空 */ }
}

async function loadBank() {
  const el = document.getElementById('bankList');
  if (!el) return;
  const unit = document.getElementById('bankUnit').value;
  const status = document.getElementById('bankStatus').value;
  const q = document.getElementById('bankSearch').value.trim();
  el.innerHTML = '<p class="text-sm text-muted">' + t('msg.loading') + '</p>';
  try {
    const data = await api('/api/bank?unit=' + encodeURIComponent(unit) + '&status=' + encodeURIComponent(status) + '&q=' + encodeURIComponent(q));
    const stats = data.stats || {};
    const statEl = document.getElementById('bankStats');
    if (statEl) statEl.textContent = t('bank.statLine')
      .replace('{t}', stats.total).replace('{d}', stats.done)
      .replace('{w}', stats.wrong).replace('{r}', stats.todo);
    _bankItems = data.items || [];
    if (!_bankItems.length) {
      el.innerHTML = '<div class="card"><p class="text-sm text-muted">' + t('bank.empty') + '</p></div>';
      return;
    }
    el.innerHTML = _bankItems.map(item => {
      const cls = item.status === 'done' ? 'tag tag-green' : item.status === 'wrong' ? 'tag tag-amber' : 'tag tag-gray';
      const label = item.status === 'done' ? t('bank.statusDone') : item.status === 'wrong' ? t('bank.statusWrong') : t('bank.statusTodo');
      return `<div class="card mb-8">
        <div class="flex-between mb-8">
          <div class="text-sm text-muted">${escapeHtml(item.unit)} · ${escapeHtml(item.chapter)} · <b>${escapeHtml(item.concept)}</b></div>
          <span class="tag ${cls}">${label}</span>
        </div>
        <p class="text-sm mb-8" style="white-space:pre-wrap;line-height:1.6">${escapeHtml(item.stem)}</p>
        <div class="flex gap-8">
          <button class="btn btn-primary btn-sm" onclick="openBankPractice('${item.id}')">${t('bank.practice')}</button>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div class="card"><p class="text-sm text-muted">' + t('bank.loadFail') + '</p></div>';
  }
}

// ── 错题重练：把状态为 wrong 的题组成顺序重练卷 ──
let _drillQueue = [];
let _drillIdx = 0;

function startWrongDrill() {
  _drillQueue = (_bankItems || []).filter(x => x.status === 'wrong');
  if (!_drillQueue.length) { toast(t('bank.drillEmpty'), 'warn'); return; }
  _drillIdx = 0;
  openDrillNext();
}

function openDrillNext() {
  if (_drillIdx >= _drillQueue.length) {
    document.getElementById('bankModalBody').innerHTML =
      `<div class="text-center" style="padding:20px 0">
        <p style="font-size:28px">🎉</p>
        <p class="text-sm">${t('bank.drillDone')}</p>
      </div>`;
    return;
  }
  const item = _drillQueue[_drillIdx];
  openModal('bankModal');
  document.getElementById('bankModalBody').innerHTML = `
    <p class="text-sm text-muted mb-8">${t('bank.drillProgress').replace('{n}', _drillIdx + 1).replace('{m}', _drillQueue.length)} · ${escapeHtml(item.concept)}</p>
    <p class="text-sm mb-12" style="white-space:pre-wrap;line-height:1.6">${escapeHtml(item.stem)}</p>
    <div class="bank-choices">
      ${item.choices.map((c, i) => `
        <label class="bank-choice" id="bc${i}">
          <input type="radio" name="bankAnswer" value="${i}">
          <span class="bank-choice-key">${String.fromCharCode(65 + i)}</span>
          <span class="bank-choice-text">${escapeHtml(c)}</span>
        </label>`).join('')}
    </div>
    <div class="flex gap-12 mt-12">
      <button class="btn btn-primary" id="bankSubmit" onclick="submitDrillAnswer('${item.id}')">${t('bank.submit')}</button>
    </div>
    <div id="bankResult" class="mt-12"></div>`;
}

async function submitDrillAnswer(qid) {
  const sel = document.querySelector('input[name="bankAnswer"]:checked');
  if (!sel) { toast(t('bank.needAnswer'), 'error'); return; }
  const btn = document.getElementById('bankSubmit');
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/bank/attempt', {
      method: 'POST',
      body: { qid, answer: parseInt(sel.value, 10) },
    });
    document.querySelectorAll('input[name="bankAnswer"]').forEach(r => {
      const i = parseInt(r.value, 10);
      const label = document.getElementById('bc' + i);
      if (label) {
        if (i === res.answer) label.classList.add('bank-correct');
        else if (r.checked) label.classList.add('bank-incorrect');
      }
    });
    document.getElementById('bankResult').innerHTML = `
      <div class="card ${res.correct ? 'bank-ok-card' : 'bank-bad-card'}">
        <p class="text-sm" style="font-weight:600">${res.correct ? t('bank.correct') : t('bank.wrong')}</p>
        <p class="text-sm mt-8">${t('bank.answerIs')} <b>${String.fromCharCode(65 + res.answer)}</b> · ${escapeHtml(res.explain)}</p>
      </div>
      <div class="flex gap-8 mt-12">
        <button class="btn btn-primary btn-sm" onclick="_drillIdx++;openDrillNext()">${t('bank.drillNext')}</button>
      </div>`;
    loadBankUnits();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openBankPractice(qid) {
  const item = (_bankItems || []).find(x => x.id === qid);
  if (!item) return;
  openModal('bankModal');
  document.getElementById('bankModalBody').innerHTML = `
    <p class="text-sm text-muted mb-8">${escapeHtml(item.unit)} · ${escapeHtml(item.chapter)} · <b>${escapeHtml(item.concept)}</b></p>
    <p class="text-sm mb-12" style="white-space:pre-wrap;line-height:1.6">${escapeHtml(item.stem)}</p>
    <div class="bank-choices">
      ${item.choices.map((c, i) => `
        <label class="bank-choice" id="bc${i}">
          <input type="radio" name="bankAnswer" value="${i}">
          <span class="bank-choice-key">${String.fromCharCode(65 + i)}</span>
          <span class="bank-choice-text">${escapeHtml(c)}</span>
        </label>`).join('')}
    </div>
    <div class="flex gap-12 mt-12">
      <button class="btn btn-primary" id="bankSubmit" onclick="submitBankAnswer('${item.id}')">${t('bank.submit')}</button>
    </div>
    <div id="bankResult" class="mt-12"></div>`;
}

async function submitBankAnswer(qid) {
  const sel = document.querySelector('input[name="bankAnswer"]:checked');
  if (!sel) { toast(t('bank.needAnswer'), 'error'); return; }
  const btn = document.getElementById('bankSubmit');
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/bank/attempt', {
      method: 'POST',
      body: { qid, answer: parseInt(sel.value, 10) },
    });
    document.querySelectorAll('input[name="bankAnswer"]').forEach(r => {
      const i = parseInt(r.value, 10);
      const label = document.getElementById('bc' + i);
      if (label) {
        if (i === res.answer) label.classList.add('bank-correct');
        else if (r.checked) label.classList.add('bank-incorrect');
      }
    });
    document.getElementById('bankResult').innerHTML = `
      <div class="card ${res.correct ? 'bank-ok-card' : 'bank-bad-card'}">
        <p class="text-sm" style="font-weight:600">${res.correct ? t('bank.correct') : t('bank.wrong')}</p>
        <p class="text-sm mt-8">${t('bank.answerIs')} <b>${String.fromCharCode(65 + res.answer)}</b> · ${escapeHtml(res.explain)}</p>
        ${!res.correct ? '<p class="text-sm text-muted mt-8">' + t('bank.addedToProblems') + '</p>' : ''}
      </div>
      <div class="flex gap-8 mt-12">
        <button class="btn btn-primary btn-sm" onclick="closeModal('bankModal');loadBankUnits();loadBank()">${t('bank.close')}</button>
      </div>`;
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── 题库导入 ──
const BANK_TEMPLATE = [
  {
    "id": "custom-demo-1",
    "unit": "力学",
    "chapter": "运动学",
    "concept": "匀变速直线运动",
    "difficulty": 2,
    "stem": "一辆汽车以 10 m/s 匀速行驶，突然以 2 m/s² 匀减速，3 s 后的速度是多少？",
    "choices": ["4 m/s", "6 m/s", "8 m/s", "16 m/s"],
    "answer": 0,
    "title": "匀减速示例",
    "explain": "v = v0 - at = 10 - 2×3 = 4 m/s"
  },
  {
    "stem": "光在真空中的速度约为多少？",
    "choices": ["3×10⁸ m/s", "3×10⁶ m/s", "3×10⁴ m/s", "300 m/s"],
    "answer": 0,
    "unit": "光学",
    "chapter": "光的传播",
    "concept": "光速",
    "difficulty": 1,
    "explain": "真空中光速 c ≈ 3×10⁸ m/s。"
  }
];

function openBankImport() {
  document.getElementById('bankImportResult').textContent = '';
  document.getElementById('bankImportText').value = JSON.stringify(BANK_TEMPLATE, null, 2);
  openModal('bankImportModal');
}

function downloadBankTemplate() {
  const blob = new Blob([JSON.stringify(BANK_TEMPLATE, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'bank_questions_template.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function submitBankImport() {
  const text = document.getElementById('bankImportText').value.trim();
  const resultEl = document.getElementById('bankImportResult');
  if (!text) { resultEl.textContent = t('bank.importEmpty'); resultEl.style.color = 'var(--danger)'; return; }
  let parsed;
  try { parsed = JSON.parse(text); } catch (e) {
    resultEl.textContent = t('bank.importBadJson'); resultEl.style.color = 'var(--danger)';
    return;
  }
  try {
    const res = await api('/api/bank/import', { method: 'POST', body: { questions: parsed } });
    resultEl.style.color = 'var(--text-muted)';
    resultEl.textContent = (res.imported ? t('bank.importOk').replace('{n}', res.imported) + ' ' : '')
      + (res.errors && res.errors.length ? t('bank.importWarn').replace('{n}', res.errors.length) : '')
      + (res.errors && res.errors.length ? '（' + res.errors.slice(0, 3).join('；') + '）' : '');
    loadBankUnits(); loadBank();
  } catch (e) {
    resultEl.textContent = e.message; resultEl.style.color = 'var(--danger)';
  }
}

