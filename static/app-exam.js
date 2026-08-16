// 真题：新建试卷 / 详情 / 题目管理
// ── B4 考试就绪度 ──
async function createExamPaper() {
  const name = document.getElementById('examName').value.trim();
  if (!name) { toast(t('exam.needName'), 'error'); return; }
  try {
    const r = await api('/api/exam/papers', { method: 'POST', body: {
      name,
      exam_date: document.getElementById('examDate').value.trim(),
      target: parseInt(document.getElementById('examTarget').value, 10) || 80,
    }});
    toast(t('exam.created'));
    document.getElementById('examName').value = '';
    loadExam();
  } catch(e) { toast(e.message, 'error'); }
}

async function loadExam() {
  const el = document.getElementById('examPaperList');
  if (!el) return;
  try {
    const r = await api('/api/exam/papers');
    const ov = document.getElementById('examOverview');
    if (r.overall === null) {
      ov.innerHTML = '<p class="text-sm text-muted mt-8">' + t('exam.noneYet') + '</p>';
      el.innerHTML = '';
      return;
    }
    ov.innerHTML = `<div class="flex-between mb-8">
        <span class="text-sm">${t('exam.globalReady')}</span>
        <span class="flex gap-8 items-center">
          <span class="tag ${r.overall >= 80 ? 'tag-green' : r.overall >= 60 ? 'tag-amber' : 'tag-red'}">${r.overall}%</span>
          ${examBar(r.overall, 160)}
        </span>
      </div>`;
    el.innerHTML = r.papers.map(p => {
      const ready = p.readiness;
      return `<div class="card" style="margin-bottom:12px">
        <div class="flex-between">
          <span class="list-item-title">${escapeHtml(p.paper.name)}</span>
          <span class="flex gap-8 items-center">
            <span class="text-sm text-muted">${t('exam.targetLabel').replace('{n}', p.paper.target)}</span>
            <span class="tag ${ready >= p.paper.target ? 'tag-green' : ready >= p.paper.target * 0.75 ? 'tag-amber' : 'tag-red'}">${t('exam.readyLabel').replace('{n}', ready)}</span>
          </span>
        </div>
        <div class="list-item-meta">${t('exam.meta').replace('{d}', escapeHtml(p.paper.exam_date || t('exam.dateNone'))).replace('{n}', p.question_count).replace('{h}', p.hit_rate)}${p.gap_to_target > 0 ? t('exam.gap').replace('{g}', p.gap_to_target) : ''}</div>
        ${examBar(ready)}
        ${p.gaps.length ? `<p class="text-sm mt-8"><b style="color:var(--warning)">${t('exam.weakTopics')}</b>${p.gaps.map(g => `<span class="tag tag-warn" style="margin:2px">${escapeHtml(g)}</span>`).join('')}</p>` : '<p class="text-sm text-muted mt-8">' + t('exam.allGood') + '</p>'}
        <div class="flex gap-8 mt-12">
          <button class="btn btn-secondary btn-sm" onclick="loadExamDetail(${p.paper.id})">${t('exam.viewAdd')}</button>
          <button class="btn btn-danger btn-sm" onclick="deleteExamPaper(${p.paper.id})">${t('exam.delete')}</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<p class="text-sm text-muted">${escapeHtml(e.message)}</p>`; }
}

async function loadExamDetail(id) {
  try {
    const p = await api(`/api/exam/papers/${id}`);
    let rowsHtml = (p.questions || []).map((q, i) => `
      <tr>
        <td>${escapeHtml(q.qno || i + 1)}</td>
        <td>${escapeHtml(q.topic)}</td>
        <td>${q.weight}</td>
        <td><span class="tag ${(p.readiness >= p.target || true) ? 'tag-gray' : ''}">—</span></td>
      </tr>`).join('');
    const html = `<div class="modal" role="dialog" style="max-width:640px">
      <div class="modal-header">
        <h3>${t('exam.detailTitle').replace('{n}', escapeHtml(p.paper.name)).replace('{r}', p.readiness).replace('{t}', p.paper.target)}</h3>
        <button class="modal-close" onclick="this.closest('.modal-overlay').classList.remove('active')">&times;</button>
      </div>
      <p class="text-sm text-muted">${t('exam.inputHint')}</p>
      <textarea id="examQInput" rows="6" class="form-input" data-i18n-ph="exam.inputPh"></textarea>
      <div class="flex gap-12 mt-12">
        <button class="btn btn-primary btn-sm" onclick="saveExamQuestions(${id})">${t('exam.saveQ')}</button>
        <button class="btn btn-secondary btn-sm" onclick="this.closest('.modal-overlay').classList.remove('active')">${t('common.close')}</button>
      </div>
      ${p.questions && p.questions.length ? `<table class="table" style="margin-top:12px"><thead><tr><th>${t('exam.colNo')}</th><th>${t('exam.colTopic')}</th><th>${t('exam.colWeight')}</th></tr></thead><tbody>${rowsHtml}</tbody></table>` : ''}
    </div>`;
    const ov = document.createElement('div');
    ov.className = 'modal-overlay active';
    ov.innerHTML = `<div style="position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;padding:16px">${html}</div>`;
    ov.addEventListener('click', e => { if (e.target === ov.firstElementChild) ov.remove(); });
    document.body.appendChild(ov);
  } catch(e) { toast(e.message, 'error'); }
}

async function saveExamQuestions(paperId) {
  const text = document.getElementById('examQInput').value.trim();
  if (!text) { toast(t('exam.needText'), 'error'); return; }
  const questions = [];
  for (const line of text.split('\n')) {
    const parts = line.split('|').map(s => s.trim());
    if (!parts[1]) continue;
    questions.push({ qno: parts[0], topic: parts[1], weight: parseFloat(parts[2]) || 1 });
  }
  if (!questions.length) { toast(t('exam.invalidLine'), 'error'); return; }
  try {
    await api(`/api/exam/papers/${paperId}/questions`, { method: 'POST', body: { questions } });
    toast(t('exam.added').replace('{n}', questions.length));
    loadExam();
    const ov = document.querySelector('.modal-overlay');
    if (ov) ov.remove();
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteExamPaper(id) {
  const ok = await confirmDialog(t('exam.deleteConfirm'));
  if (!ok) return;
  try {
    await api(`/api/exam/papers/${id}`, { method: 'DELETE' });
    toast(t('msg.deleted'));
    loadExam();
  } catch(e) { toast(e.message, 'error'); }
}

