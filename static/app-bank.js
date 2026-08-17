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
let _practiceMode = 'practice';
// AI 评分重评用：记录当前正在练习的题目
let _activePractice = null;

function startWrongDrill() {
  _drillQueue = (_bankItems || []).filter(x => x.status === 'wrong');
  if (!_drillQueue.length) { toast(t('bank.drillEmpty'), 'warn'); return; }
  _drillIdx = 0;
  openDrillNext();
}

const _TYPE_BADGE = { single: '单选', multiple: '多选', fill: '填空', subjective: '主观题', composite: '大小题' };

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
  _practiceMode = 'drill';
  openModal('bankModal');
  renderPractice(item, 'dq');
}

function openBankPractice(qid) {
  const item = (_bankItems || []).find(x => x.id === qid);
  if (!item) return;
  _practiceMode = 'practice';
  openModal('bankModal');
  renderPractice(item, 'q');
}

// 题型作答区渲染（递归支持大小题嵌套）
function buildQuestionBody(item, prefix, depth = 0) {
  const t = item.type || 'single';
  if (t === 'single' || t === 'multiple') {
    const inputType = t === 'single' ? 'radio' : 'checkbox';
    return `<div class="bank-choices">` + (item.choices || []).map((c, i) => `
      <label class="bank-choice" id="${prefix}_c${i}">
        <input type="${inputType}" name="${prefix}_ans" value="${i}">
        <span class="bank-choice-key">${String.fromCharCode(65 + i)}</span>
        <span class="bank-choice-text">${escapeHtml(c)}</span>
      </label>`).join('') + `</div>`;
  }
  if (t === 'fill') {
    const blanks = Array.isArray(item.answer) ? item.answer.length : 1;
    let h = '';
    for (let i = 0; i < blanks; i++) h += `<input type="text" class="bank-fill" id="${prefix}_f${i}" placeholder="第 ${i+1} 空"> `;
    return `<div class="bank-fill-wrap">${h}</div>`;
  }
  if (t === 'subjective') {
    return `<textarea class="bank-subj" id="${prefix}_s" rows="4" placeholder="在此作答…"></textarea>
      <p class="text-xs text-muted mt-8">主观题提交后标记「待评阅」，可对照参考答案自评。</p>`;
  }
  if (t === 'composite') {
    return (item.parts || []).map((p, i) => {
      const lbl = depth === 0 ? `${i+1}.` : `(${i+1})`;
      return `<div class="bank-part">
        <div class="bank-part-title">${lbl} ${escapeHtml(p.stem || '')}</div>
        ${buildQuestionBody(p, `${prefix}_p${i}`, depth + 1)}
      </div>`;
    }).join('');
  }
  return '';
}

function collectAnswer(item, prefix) {
  const t = item.type || 'single';
  if (t === 'single') {
    const sel = document.querySelector(`input[name="${prefix}_ans"]:checked`);
    return sel ? parseInt(sel.value, 10) : null;
  }
  if (t === 'multiple') {
    return Array.from(document.querySelectorAll(`input[name="${prefix}_ans"]:checked`)).map(r => parseInt(r.value, 10));
  }
  if (t === 'fill') {
    if (Array.isArray(item.answer)) {
      return item.answer.map((_, i) => { const el = document.getElementById(`${prefix}_f${i}`); return el ? el.value : ''; });
    }
    const el = document.getElementById(`${prefix}_f0`); return el ? el.value : '';
  }
  if (t === 'subjective') { const el = document.getElementById(`${prefix}_s`); return el ? el.value : ''; }
  if (t === 'composite') { return (item.parts || []).map((p, i) => collectAnswer(p, `${prefix}_p${i}`)); }
  return null;
}

function renderResultBlock(item, res, prefix, depth = 0) {
  const t = item.type || 'single';
  if (t === 'single' || t === 'multiple') {
    const correct = res.answer;
    const corrSet = Array.isArray(correct) ? correct : [correct];
    (item.choices || []).forEach((c, i) => {
      const el = document.getElementById(`${prefix}_c${i}`);
      if (!el) return;
      if (corrSet.includes(i)) el.classList.add('bank-correct');
      const input = el.querySelector('input');
      if (input && input.checked && !corrSet.includes(i)) el.classList.add('bank-incorrect');
    });
    const verdict = res.correct === null ? '📝 待评阅' : (res.correct ? '✅ 正确' : '❌ 错误');
    return `<div class="card ${res.correct === false ? 'bank-bad-card' : 'bank-ok-card'}">
      <p class="text-sm" style="font-weight:600">${verdict}</p>
      ${res.explain ? `<p class="text-sm mt-8">解析：${escapeHtml(res.explain)}</p>` : ''}
    </div>`;
  }
  if (t === 'fill') {
    const verdict = res.correct === null ? '📝 待评阅' : (res.correct ? '✅ 正确' : '❌ 错误');
    const ans = Array.isArray(res.answer) ? res.answer.join(' / ') : res.answer;
    return `<div class="card ${res.correct === false ? 'bank-bad-card' : 'bank-ok-card'}">
      <p class="text-sm" style="font-weight:600">${verdict}</p>
      <p class="text-sm mt-8">参考答案：${escapeHtml(ans)}</p>
      ${res.explain ? `<p class="text-sm mt-8">解析：${escapeHtml(res.explain)}</p>` : ''}
    </div>`;
  }
  if (t === 'subjective') {
    return `<div class="card bank-review-card">
      <p class="text-sm" style="font-weight:600">📝 待评阅（请对照参考答案自评）</p>
      <p class="text-sm mt-8">参考答案：${escapeHtml(res.answer)}</p>
      ${res.explain ? `<p class="text-sm mt-8">解析：${escapeHtml(res.explain)}</p>` : ''}
    </div>`;
  }
  if (t === 'composite') {
    let html = `<div class="card ${res.correct === false ? 'bank-bad-card' : 'bank-ok-card'}">
      <p class="text-sm" style="font-weight:600">${res.correct === null ? '含待评阅' : (res.correct ? '✅ 全部正确' : '❌ 有错误')}</p>
    </div>`;
    (item.parts || []).forEach((p, i) => {
      const pr = (res.parts && res.parts[i]) || {};
      const lbl = depth === 0 ? `${i+1}.` : `(${i+1})`;
      html += `<div class="bank-part-result"><b>${lbl}</b> ${renderResultBlock(p, pr, `${prefix}_p${i}`, depth + 1)}</div>`;
    });
    return html;
  }
  return '';
}

function renderPractice(item, prefix) {
  const badge = _TYPE_BADGE[item.type || 'single'] || '单选';
  const progress = _practiceMode === 'drill'
    ? `<p class="text-sm text-muted mb-8">${t('bank.drillProgress').replace('{n}', _drillIdx + 1).replace('{m}', _drillQueue.length)} · ${escapeHtml(item.concept || '')}</p>`
    : `<p class="text-sm text-muted mb-8">${escapeHtml(item.unit || '')} · ${escapeHtml(item.chapter || '')} · <b>${escapeHtml(item.concept || '')}</b></p>`;
  document.getElementById('bankModalBody').innerHTML = `
    ${progress}
    <p class="text-sm mb-4"><span class="tag tag-gray">${badge}</span></p>
    <p class="text-sm mb-12" style="white-space:pre-wrap;line-height:1.6">${escapeHtml(item.stem || '')}</p>
    ${buildQuestionBody(item, prefix)}
    <div class="flex gap-12 mt-12">
      <button class="btn btn-primary" id="bankSubmit" onclick="submitPractice('${item.id}', '${prefix}')">${t('bank.submit')}</button>
    </div>
    <div id="bankResult" class="mt-12"></div>`;
}

async function submitPractice(qid, prefix) {
  const item = (_bankItems || []).find(x => x.id === qid)
            || (_drillQueue || []).find(x => x.id === qid);
  if (!item) { toast(t('bank.needAnswer'), 'error'); return; }
  _activePractice = { qid, prefix };
  const qtype = item.type || 'single';
  const answer = collectAnswer(item, prefix);
  if (qtype !== 'composite' && (answer === null || answer === '' || (Array.isArray(answer) && !answer.length))) {
    toast(t('bank.needAnswer'), 'error'); return;
  }
  const btn = document.getElementById('bankSubmit');
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/bank/attempt', { method: 'POST', body: { qid, answer } });
    const nextBtn = _practiceMode === 'drill'
      ? `<button class="btn btn-primary btn-sm" onclick="_drillIdx++;openDrillNext()">${t('bank.drillNext')}</button>` : '';
    const hasSubj = (qtype === 'subjective') || (qtype === 'composite' && (item.parts || []).some(p => p.type === 'subjective'));
    const scoreBtn = hasSubj
      ? `<button class="btn btn-secondary btn-sm" id="bankAiScore" onclick="scorePractice('${qid}', '${prefix}')">🤖 AI 评分</button>` : '';
    document.getElementById('bankResult').innerHTML = renderResultBlock(item, res, prefix)
      + (res.correct === false ? `<p class="text-sm text-muted mt-8">${t('bank.addedToProblems')}</p>` : '')
      + `<div class="flex gap-8 mt-12">
           ${scoreBtn}
           ${nextBtn}
           <button class="btn btn-sm" onclick="closeModal('bankModal');loadBankUnits();loadBank()">${t('bank.close')}</button>
         </div>`;
    loadBankUnits();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// AI 评分：主观题/含主观 composite 提交后调用
async function scorePractice(qid, prefix) {
  const item = (_bankItems || []).find(x => x.id === qid)
            || (_drillQueue || []).find(x => x.id === qid);
  if (!item) { toast(t('bank.needAnswer'), 'error'); return; }
  _activePractice = { qid, prefix };
  const answer = collectAnswer(item, prefix);
  const btn = document.getElementById('bankAiScore');
  if (btn) { btn.disabled = true; btn.textContent = '🤖 AI 评分中…'; }
  try {
    const r = await api('/api/bank/score', { method: 'POST', body: { qid, answer } });
    if (!r.ai_available && !r.history) {
      toast('未配置 AI，请对照参考答案自评。', 'ok');
      return;
    }
    const total = r.score === null ? '—' : r.score + ' 分';
    const partsHtml = (r.parts || []).map((p, i) => {
      const lbl = `${i+1}.`;
      const pv = p.score === null ? '待评阅' : p.score + ' 分';
      const pc = p.comment ? `<span class="text-sm">${escapeHtml(p.comment)}</span>` : '';
      return `<div class="text-sm mt-4"><b>${lbl}</b> 得分 <b>${pv}</b> ${pc}</div>`;
    }).join('');
    // 历史评分记录（最近 5 条）
    const hist = (r.history || []).slice(0, 5);
    const histHtml = hist.length
      ? `<div class="mt-8"><p class="text-sm text-muted" style="font-weight:600">📚 历史评分</p>
          ${hist.map(h => `<div class="text-sm mt-4" style="display:flex;gap:8px;align-items:baseline">
            <b>${h.score === null ? '待评阅' : h.score + ' 分'}</b>
            <span class="text-muted" style="font-size:12px">${escapeHtml(String(h.created_at || '').slice(5, 16))}</span>
            ${h.comment ? `<span class="text-muted" style="font-size:12px">${escapeHtml(h.comment.slice(0, 40))}</span>` : ''}
          </div>`).join('')}
        </div>` : '';
    const aiNote = !r.ai_available
      ? `<p class="text-sm mt-8 text-muted">🤖 未配置 AI，本次未评分（已记录提交）。</p>` : '';
    document.getElementById('bankResult').innerHTML = `<div class="card bank-review-card">
      <p class="text-sm" style="font-weight:600">🤖 AI 评分：<b>${total}</b></p>
      ${r.comment ? `<p class="text-sm mt-8">${escapeHtml(r.comment)}</p>` : ''}
      ${r.against ? `<p class="text-sm mt-8" style="color:var(--text-muted)">命中要点：${escapeHtml(r.against)}</p>` : ''}
      ${partsHtml}
      ${aiNote}
      ${histHtml}
      <p class="text-sm mt-8 text-muted">✅ 评分完成</p>
      <div class="flex gap-8 mt-12">
        <button class="btn btn-secondary btn-sm" onclick="scoreAgain()">🤖 重新评分</button>
        <button class="btn btn-sm" onclick="closeModal('bankModal');loadBankUnits();loadBank()">${t('bank.close')}</button>
      </div>
    </div>`;
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🤖 AI 评分'; }
  }
}

function scoreAgain() {
  // 结果区按钮已被覆盖，从 _activePractice 重新评分
  const cur = _activePractice || {};
  if (!cur.qid) { location.reload(); return; }
  const btn = document.getElementById('bankAiScore');
  if (btn) { btn.disabled = false; btn.textContent = '🤖 AI 评分'; }
  scorePractice(cur.qid, cur.prefix || '');
}

// ── 题库导入 ──
const BANK_TEMPLATE = [
  {
    "id": "custom-demo-1",
    "type": "single",
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
    "type": "multiple",
    "stem": "以下哪些量是矢量？（多选）",
    "choices": ["位移", "速率", "加速度", "路程"],
    "answer": [0, 2],
    "unit": "力学",
    "chapter": "运动学",
    "concept": "矢量与标量",
    "difficulty": 2,
    "explain": "位移与加速度是矢量；速率与路程是标量。"
  },
  {
    "type": "fill",
    "stem": "自由落体第 1 s 内的位移约为 ___ m（g≈10 m/s²），第 2 s 内的位移约为 ___ m。",
    "answer": ["5", "15"],
    "unit": "力学",
    "chapter": "自由落体",
    "concept": "自由落体",
    "difficulty": 2,
    "explain": "第1s: ½·10·1²=5m；前2s: ½·10·4=20m，第2s内=20-5=15m。"
  },
  {
    "type": "subjective",
    "stem": "请简述牛顿第二定律的物理意义，并说明质量与惯性的关系。",
    "answer": "牛顿第二定律 F=ma 表明物体加速度与合外力成正比、与质量成反比，方向同合外力。质量是惯性大小的量度，质量越大惯性越大，运动状态越难改变。",
    "unit": "力学",
    "chapter": "牛顿定律",
    "concept": "牛顿第二定律",
    "difficulty": 3,
    "explain": "要点：比例关系、方向、质量=惯性量度。"
  },
  {
    "type": "composite",
    "stem": "解答下列小题：",
    "unit": "力学",
    "chapter": "运动学综合",
    "concept": "运动学综合",
    "difficulty": 3,
    "parts": [
      {
        "type": "single",
        "stem": "（1）物体做匀速直线运动的依据是？",
        "choices": ["合力为零", "合力恒定", "加速度为零或合力为零", "速度为零"],
        "answer": 2,
        "explain": "匀速直线运动要求加速度为零，即合力为零。"
      },
      {
        "type": "fill",
        "stem": "（2）若 v-t 图线为水平直线，则加速度为 ___ 。",
        "answer": "0",
        "explain": "水平 v-t 线斜率（加速度）为零。"
      }
    ]
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

// ── AI 按题型出题：生成后填入导入框，自动 AI 审题供复查 ──
async function generateBankQuestion() {
  const typeEl = document.getElementById('bankGenType');
  const topicEl = document.getElementById('bankGenTopic');
  const type = typeEl ? typeEl.value : 'single';
  const topic = topicEl ? (topicEl.value || '').trim() : '';
  const genBtn = document.querySelector('.bank-gen-row .btn');
  if (genBtn) genBtn.disabled = true;
  try {
    const res = await api('/api/bank/generate', { method: 'POST', body: { type, topic, subject: '' } });
    const q = res.question || {};
    openBankImport();
    const ta = document.getElementById('bankImportText');
    if (ta) ta.value = JSON.stringify([q], null, 2);
    toast('已生成题目，请复查后导入', 'ok');
    await autoReview(q);
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    if (genBtn) genBtn.disabled = false;
  }
}

// 对题目对象做 AI 审题，结果展示在导入弹窗内
async function autoReview(q) {
  const box = document.getElementById('bankReviewResult');
  if (!box) return;
  box.innerHTML = '<p class="text-sm text-muted">🤖 正在审题…</p>';
  try {
    const r = await api('/api/bank/review', { method: 'POST', body: { question: q, subject: '' } });
    if (!r.ai_available) {
      box.innerHTML = '<p class="text-sm text-muted">🤖 未配置 AI，跳过自动审题（可对照模板复查）。</p>';
      return;
    }
    const badge = r.verdict === 'pass' ? '✅ 通过' : (r.verdict === 'warn' ? '⚠️ 建议修改' : '❌ 需重出');
    const color = r.verdict === 'pass' ? 'var(--text)' : (r.verdict === 'warn' ? 'var(--warning)' : 'var(--danger)');
    box.innerHTML = `<div class="bank-review-card" style="border-left:3px solid ${color}">
      <p class="text-sm" style="font-weight:600;color:${color}">AI 审题：${badge}</p>
      ${r.comment ? `<p class="text-sm mt-4">${escapeHtml(r.comment)}</p>` : ''}
      ${(r.issues && r.issues.length) ? `<ul class="text-sm mt-4" style="margin:4px 0 0 18px">${r.issues.map(x => `<li>${escapeHtml(x)}</li>`).join('')}</ul>` : ''}
      ${r.revised ? `<p class="text-sm mt-4">💡 修订建议：<pre class="text-xs" style="background:var(--bg);padding:8px;border-radius:6px;white-space:pre-wrap">${escapeHtml(r.revised)}</pre></p>` : ''}
    </div>`;
  } catch (e) {
    box.innerHTML = `<p class="text-sm" style="color:var(--danger)">🤖 审题失败：${escapeHtml(e.message)}</p>`;
  }
}

// 审题按钮：解析导入框 JSON 数组并逐题审题
async function reviewBankQuestions() {
  const text = document.getElementById('bankImportText').value.trim();
  const box = document.getElementById('bankReviewResult');
  if (!text) { if (box) box.innerHTML = '<p class="text-sm text-muted">导入框为空，无法审题。</p>'; return; }
  let arr;
  try { arr = JSON.parse(text); } catch (e) {
    if (box) box.innerHTML = '<p class="text-sm" style="color:var(--danger)">JSON 解析失败，无法审题。</p>';
    return;
  }
  if (!Array.isArray(arr)) arr = [arr];
  if (!arr.length) return;
  for (const q of arr) {
    await autoReview(q);
    if (arr.indexOf(q) < arr.length - 1) box.innerHTML += '<hr class="my-8">';
  }
}

