// 设置：设置页 / 周期报告 / FSRS / 数据导出导入 / 公式面板
// ── 设置页 ──
async function loadSettings() {
  probeLocalModels();
  try {
    const s = await api('/api/settings');
    document.getElementById('setApiBase').value = s.api_base || '';
    document.getElementById('setApiKey').value = '';
    document.getElementById('setApiKey').placeholder = s.has_api_key ? t('set.keyPh') : 'sk-...';
    document.getElementById('setModel').value = s.model || '';
    document.getElementById('setFastModel').value = s.fast_model || '';
    document.getElementById('setHeavyModel').value = s.heavy_model || '';
    document.getElementById('setVisionModel').value = s.vision_model || '';
    document.getElementById('setMasterPassword').value = '';
    document.getElementById('setMasterPassword').placeholder = s.key_source === 'keyfile' ? t('set.masterPhKeyfile') : t('set.masterPh');
    document.getElementById('setTemp').value = s.temperature || '0.3';
    const hintCacheSel = document.getElementById('setHintCache');
    if (hintCacheSel) hintCacheSel.value = s.hint_cache_enabled === false ? '0' : '1';
    const capEl = document.getElementById('setDailyCap');
    if (capEl) capEl.value = s.daily_review_cap || 0;
    const ctxSel = document.getElementById('setContextTokens');
    const ctxCustom = document.getElementById('setContextCustom');
    if (ctxSel) {
      const v = String(s.ai_context_tokens || 32000);
      const preset = Array.from(ctxSel.options).some(o => o.value === v && o.value !== 'custom');
      if (preset) {
        ctxSel.value = v;
        if (ctxCustom) { ctxCustom.style.display = 'none'; ctxCustom.value = ''; }
      } else {
        ctxSel.value = 'custom';
        if (ctxCustom) { ctxCustom.style.display = ''; ctxCustom.value = v; }
      }
    }
    const localSel = document.getElementById('setAllowLocal');
    if (localSel) localSel.value = s.allow_local_ai === false ? '0' : '1';
    const defSel = document.getElementById('setDefaultSubject');
    if (defSel) {
      loadSubjectOptions(defSel, s.default_subject || 'physics');
    }
    const srcLabel = {
      environment: t('set.srcEnv'),
      keyfile: t('set.srcKeyfile'),
      runtime: t('set.srcRuntime'),
      none: t('set.srcNone'),
    };
    const ksEl = document.getElementById('keyStatus');
    ksEl.textContent = s.key_file_locked
      ? t('set.keyFileLocked') + '（' + (srcLabel[s.key_source] || srcLabel.none) + '）'
      : (srcLabel[s.key_source] || srcLabel.none);
    loadPrefs();
    loadSubjectsAdmin();
  } catch(e) { toast(e.message, 'error'); }
}

// ── 学科管理（注册表驱动，网页端增删）──
function onCtxPresetChange() {
  const sel = document.getElementById('setContextTokens');
  const custom = document.getElementById('setContextCustom');
  if (!sel || !custom) return;
  const isCustom = sel.value === 'custom';
  custom.style.display = isCustom ? '' : 'none';
  if (isCustom && !custom.value) custom.value = '';
}
async function loadSubjectsAdmin() {
  const wrap = document.getElementById('subjectAdminList');
  if (!wrap) return;
  try {
    const data = await api('/api/subjects');
    const list = data.subjects || [];
    wrap.innerHTML = list.map(s => `
      <div class="subject-admin-row" style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border,#eee)">
        <span style="flex:1">${escapeHtml(s.title || s.id)}
          <span class="text-muted text-sm">${escapeHtml(s.id)}</span>
        </span>
        ${s.builtin ? `<span class="tag tag-gray">${t('set.subjBuiltin')}</span>`
                    : `<button class="btn btn-secondary btn-sm" onclick="deleteSubject('${escapeHtml(s.id)}')">${t('set.subjDelete')}</button>`}
      </div>`).join('');
  } catch(e) { /* 静默：列表加载失败不阻塞设置页 */ }
}

async function addSubject() {
  const idEl = document.getElementById('newSubjectId');
  const titleEl = document.getElementById('newSubjectTitle');
  const id = (idEl.value || '').trim();
  const title = (titleEl.value || '').trim();
  if (!id) { toast(t('set.subjNewIdPh'), 'error'); return; }
  try {
    await api('/api/subjects', { method: 'POST', body: { id, title } });
    toast(t('set.subjAdded').replace('{s}', title || id));
    idEl.value = ''; titleEl.value = '';
    loadSubjectsAdmin();
    loadSubjectOptions(document.getElementById('setDefaultSubject'));
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteSubject(id) {
  if (!confirm(t('set.subjConfirm').replace('{s}', id))) return;
  try {
    await api('/api/subjects/' + encodeURIComponent(id), { method: 'DELETE' });
    toast(t('set.subjDeleted').replace('{s}', id));
    loadSubjectsAdmin();
    loadSubjectOptions(document.getElementById('setDefaultSubject'));
  } catch(e) { toast(e.message, 'error'); }
}

async function unlockKeystore() {
  const pwd = document.getElementById('setMasterPassword').value;
  if (!pwd) { toast(t('set.unlockFail'), 'error'); return; }
  try {
    const r = await api('/api/keystore/unlock', { method: 'POST', body: { master_password: pwd } });
    if (!r.ok) { toast(t('set.unlockFail'), 'error'); return; }
    toast(t('set.unlockOk'), 'success');
    document.getElementById('setMasterPassword').value = '';
    loadSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function clearKeystore() {
  const ok = await confirmDialog(t('set.clearConfirm'));
  if (!ok) return;
  try {
    await api('/api/keystore/clear', { method: 'POST', body: {} });
    toast(t('set.clearOk'), 'success');
    loadSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function showPeriodicReport() {
  try {
    const [w, m] = await Promise.all([
      api('/api/report/weekly'),
      api('/api/report/monthly'),
    ]);
    const html = `
      <div class="flex gap-8 mb-8">
        <button class="btn btn-primary btn-sm" onclick="periodicTab('week')">${t('report.weekTab')}</button>
        <button class="btn btn-secondary btn-sm" onclick="periodicTab('month')">${t('report.monthTab')}</button>
        <button class="btn btn-secondary btn-sm" onclick="printPeriodicReport()" data-i18n="report.print">🖨 打印报告</button>
      </div>
      <div id="periodicWeek">${periodicWeekHtml(w)}</div>
      <div id="periodicMonth" class="hidden">${periodicMonthHtml(m)}</div>`;
    const mb = document.getElementById('modalBody');
    mb.innerHTML = html;
    document.getElementById('modalTitle').textContent = t('card.weeklyMore');
    renderMath(mb);
    openModal('problemModal');
  } catch(e) { toast(e.message || t('report.loadFail'), 'error'); }
}

// 报告打印：新窗口呈现当前可见 tab 的内容 + 打印
function printPeriodicReport() {
  const week = document.getElementById('periodicWeek');
  const month = document.getElementById('periodicMonth');
  const body = (!week.classList.contains('hidden') ? week : month) || week;
  if (!body) { toast(t('report.loadFail'), 'error'); return; }
  const w = window.open('', '_blank');
  if (!w) { toast(t('report.popBlocked'), 'error'); return; }
  w.document.write(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>${document.title} · ${t('card.weeklyMore')}</title>
    <style>body{font-family:system-ui,sans-serif;max-width:720px;margin:24px auto;color:#222;line-height:1.6}
    .hidden{display:none}.error-bar-track{background:#eee;border-radius:4px;height:10px;flex:1}
    .error-bar-fill{height:100%;border-radius:4px}.error-bar-row{display:flex;align-items:center;gap:8px;margin:4px 0}
    @media print{body{margin:0}}</style></head><body>${body.innerHTML}
    <script>window.onload=()=>window.print()<\/script></body></html>`);
  w.document.close();
}

function periodicTab(tab) {
  const w = document.getElementById('periodicWeek');
  const m = document.getElementById('periodicMonth');
  if (!w || !m) return;
  w.classList.toggle('hidden', tab !== 'week');
  m.classList.toggle('hidden', tab !== 'month');
}

function periodicBarHtml(rows, max) {
  const m = max || Math.max(1, ...rows.map(r => r.count));
  return rows.map(r => `
    <div class="error-bar-row">
      <span class="text-sm" style="min-width:130px">${escapeHtml(r.label || r.date)}</span>
      <div class="error-bar-track"><div class="error-bar-fill" style="width:${Math.round(r.count / m * 100)}%;background:var(--accent)"></div></div>
      <b class="text-sm" style="min-width:32px">${r.count}</b>
    </div>`).join('') || '<p class="text-sm text-muted">' + t('msg.noData') + '</p>';
}

function periodicWeekHtml(w) {
  if (!w || w.week_start === undefined) return '<p class="text-sm text-muted">' + t('msg.noData') + '</p>';
  const delta = w.review_delta || 0;
  const deltaStr = delta > 0 ? '+' + delta : String(delta);
  return `
    <p class="text-sm text-muted mb-8">${t('report.weekRange').replace('{s}', escapeHtml(w.week_start))}</p>
    <div class="error-bar-row"><span class="text-sm">${t('report.newProblems')}</span><b>${w.new_problems}</b><span class="text-sm text-muted">${t('report.vsLastWeek').replace('{n}', w.prev_problems)}</span></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.reviews')}</span><b>${w.week_reviews}</b><span class="text-sm text-muted">${t('report.delta').replace('{d}', deltaStr)}</span></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.goodRate')}</span><b>${(w.good_rate * 100).toFixed(0)}%</b></div>
    <p class="hint-text mt-12">${t('report.tip').replace('{t}', t(w.tip_key || 'report.tipWeekNone'))}</p>`;
}

function periodicMonthHtml(m) {
  if (!m || m.start === undefined) return '<p class="text-sm text-muted">' + t('msg.noData') + '</p>';
  const daily = (m.daily || []).map(d => ({ label: d.date, count: d.count }));
  const errs = (m.top_errors || []).map(e => ({ label: e.label, count: e.count }));
  return `
    <p class="text-sm text-muted mb-8">${t('report.monthRange').replace('{s}', escapeHtml(m.start)).replace('{e}', escapeHtml(m.end))}</p>
    <div class="error-bar-row"><span class="text-sm">${t('report.newProblems')}</span><b>${m.month_new}</b><span class="text-sm text-muted">${t('report.vsLastMonth').replace('{n}', m.prev_new)}</span></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.reviews')}</span><b>${m.month_revs}</b><span class="text-sm text-muted">${t('report.vsLastMonth').replace('{n}', m.prev_revs)}</span></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.goodRate')}</span><b>${(m.good_rate * 100).toFixed(0)}%</b></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.activeDays')}</span><b>${m.active_days}</b></div>
    <div class="error-bar-row"><span class="text-sm">${t('report.mastered')}</span><b>${m.mastered}</b><span class="text-sm text-muted">/ ${m.total_problems}</span></div>
    <div class="text-sm text-muted mt-12 mb-8">${t('report.dailyTitle')}</div>
    ${periodicBarHtml(daily.slice(-14))}
    <div class="text-sm text-muted mt-12 mb-8">${t('report.topErrors')}</div>
    ${periodicBarHtml(errs)}
    <p class="hint-text mt-12">${t('report.tip').replace('{t}', t(m.tip_key || 'report.tipMonthNone'))}</p>`;
}

async function loadPrefs() {
  const notifyEl = document.getElementById('prefNotify');
  if (notifyEl) notifyEl.checked = notificationsEnabled();
  try {
    const p = await api('/api/profile');
    const prefs = p.preferences || {};
    const goal = p.goal || {};
    const el = (id, v) => { const e = document.getElementById(id); if (e) e.value = v || (id === 'prefDailyTarget' ? '0' : ''); };
    el('prefDepth', prefs.explain_depth || '2');
    el('prefExamples', prefs.example_count || '1');
    el('prefDailyTarget', goal.daily_review_target === undefined ? '0' : goal.daily_review_target);
  } catch(e) { /* 偏好可选 */ }
}

async function savePrefs() {
  const notifyEl = document.getElementById('prefNotify');
  if (notifyEl) localStorage.setItem('notifyEnabled', notifyEl.checked ? '1' : '0');
  try {
    await api('/api/profile', {
      method: 'PUT',
      body: {
        explain_depth: document.getElementById('prefDepth').value,
        example_count: document.getElementById('prefExamples').value,
        daily_review_target: document.getElementById('prefDailyTarget').value,
      },
    });
    toast(t('msg.prefsSaved'));
    loadPrefs();
  } catch(e) { toast(e.message, 'error'); }
}

// ── P0 FSRS 参数个性化 ──
async function loadFsrsStatus() {
  const el = document.getElementById('fsrsStatus');
  if (!el) return;
  try {
    const s = await api('/api/fsrs/status');
    if (!s.available) {
      el.textContent = t('fsrs.disabled');
      el.style.color = 'var(--warning)';
      return;
    }
    const src = s.params_source === 'trained'
      ? t('fsrs.trained').replace('{d}', escapeHtml(s.trained_at)).replace('{n}', s.sample_count)
      : t('fsrs.default');
    let extra = '';
    if (s.training) extra = ' <span class="tag tag-amber">' + t('fsrs.training') + '</span>';
    else if (s.last_train) extra = ` <span class="tag tag-green">${t('fsrs.trainOk')}</span>`;
    else if (s.train_error) extra = ` <span class="tag tag-red">${t('fsrs.lastFail')}</span>`;
    el.innerHTML = t('fsrs.enabled').replace('{s}', src).replace('{r}', s.desired_retention) + extra;
    const ret = document.getElementById('fsrsRetention');
    if (ret) { ret.value = s.desired_retention; document.getElementById('fsrsRetentionVal').textContent = s.desired_retention; }
    if (s.training) { setTimeout(loadFsrsStatus, 3000); }
  } catch(e) { el.textContent = t('fsrs.loadFail'); }
}

async function saveFsrsRetention() {
  try {
    const r = await api('/api/fsrs/retention', {
      method: 'POST',
      body: { value: parseFloat(document.getElementById('fsrsRetention').value) },
    });
    toast(r.ok ? t('fsrs.retentionSaved') : t('fsrs.retentionRange'), r.ok ? '' : 'error');
    if (r.ok) loadFsrsStatus();
  } catch(e) { toast(e.message, 'error'); }
}

async function trainFsrs() {
  const btn = document.getElementById('trainFsrsBtn');
  btn.disabled = true;
  try {
    const r = await api('/api/fsrs/train', { method: 'POST', body: {} });
    if (r.started) {
      toast(t('fsrs.trainingStart').replace('{n}', r.sample_count));
      setTimeout(loadFsrsStatus, 2000);
    } else {
      toast(t('fsrs.trainFail').replace('{m}', r.error || t('fsrs.unknown')), 'error');
    }
  } catch(e) { toast(e.message, 'error'); }
  btn.disabled = false;
}

// ── 复习提醒（浏览器通知，最小可用）──
function enableReviewNotify() {
  if (!('Notification' in window)) { toast(t('set.notifyUnsupported'), 'warn'); return; }
  Notification.requestPermission().then(p => {
    toast(p === 'granted' ? t('set.notifyOn') : t('set.notifyOff'), p === 'granted' ? '' : 'warn');
  });
}

async function loadOptimalRetention() {
  const el = document.getElementById('optimalRetentionResult');
  if (!el) return;
  el.innerHTML = '<p class="text-sm text-muted">' + t('msg.loading') + '</p>';
  try {
    const r = await api('/api/fsrs/optimal');
    const maxMin = Math.max(...r.points.map(p => p.minutes), 1);
    el.innerHTML = `
      <p class="text-sm"><strong>${t('fsrs.optimalReco').replace('{r}', r.recommended).replace('{c}', r.current)}</strong>
        ${!r.has_data ? `<span class="tag tag-amber">${t('fsrs.optimalThin')}</span>` : ''}
        ${r.assumed_stability ? `<span class="tag tag-gray">${t('fsrs.optimalAssumed')}</span>` : ''}
      </p>
      <p class="text-sm text-muted">${t('fsrs.optimalMeta').replace('{n}', r.n_items).replace('{s}', r.avg_stability)}</p>
      <div style="display:flex;gap:6px;align-items:flex-end;height:80px;margin:8px 0">
        ${r.points.map(p => `
          <div style="flex:1;text-align:center" title="R=${p.retention}">
            <div style="height:${Math.round(p.minutes / maxMin * 70)}px;background:${p.retention === r.recommended ? 'var(--accent)' : 'var(--border,#ccc)'};border-radius:3px 3px 0 0"></div>
            <div class="text-sm text-muted">${p.retention}</div>
          </div>`).join('')}
      </div>
      <table class="text-sm" style="width:100%;border-collapse:collapse">
        <tr style="text-align:left;border-bottom:1px solid var(--border,#ddd)">
          <th style="padding:4px 8px">${t('fsrs.optRet')}</th>
          <th style="padding:4px 8px">${t('fsrs.optInterval')}</th>
          <th style="padding:4px 8px">${t('fsrs.optDaily')}</th>
          <th style="padding:4px 8px">${t('fsrs.optMinutes')}</th>
        </tr>
        ${r.points.map(p => `
          <tr style="${p.retention === r.recommended ? 'font-weight:600;background:var(--hover,#f0f4ff)' : ''}border-bottom:1px solid var(--border,#eee)">
            <td style="padding:4px 8px">${p.retention}${p.retention === r.current ? ' ◀' : ''}</td>
            <td style="padding:4px 8px">${p.interval_days}d</td>
            <td style="padding:4px 8px">${p.daily_reviews}</td>
            <td style="padding:4px 8px">${p.minutes} min</td>
          </tr>`).join('')}
      </table>
      <p class="hint-text mt-8">${t('fsrs.optimalNote')}</p>`;
  } catch(e) { el.innerHTML = `<p class="text-sm text-muted">${escapeHtml(e.message)}</p>`; }
}

async function resetFsrs() {
  try {
    const r = await api('/api/fsrs/reset', { method: 'POST', body: {} });
    toast(r.ok ? t('fsrs.resetOk') : t('fsrs.resetFail'), r.ok ? '' : 'error');
    if (r.ok) loadFsrsStatus();
  } catch(e) { toast(e.message, 'error'); }
}

async function saveSettings() {
  const body = {
    api_base: document.getElementById('setApiBase').value,
    model: document.getElementById('setModel').value,
    temperature: document.getElementById('setTemp').value,
    fast_model: document.getElementById('setFastModel').value,
    heavy_model: document.getElementById('setHeavyModel').value,
    vision_model: document.getElementById('setVisionModel').value,
  };
  const defSel = document.getElementById('setDefaultSubject');
  if (defSel && defSel.value) body.default_subject = defSel.value;
  const hintCacheSel = document.getElementById('setHintCache');
  if (hintCacheSel) body.hint_cache_enabled = hintCacheSel.value;
  const capEl = document.getElementById('setDailyCap');
  if (capEl && capEl.value !== '') body.daily_review_cap = parseInt(capEl.value, 10) || 0;
  const ctxSel = document.getElementById('setContextTokens');
  const ctxCustom = document.getElementById('setContextCustom');
  if (ctxSel) {
    const val = ctxSel.value === 'custom' ? (ctxCustom ? ctxCustom.value : '') : ctxSel.value;
    body.ai_context_tokens = parseInt(val, 10) || 32000;
  }
  const localSel = document.getElementById('setAllowLocal');
  if (localSel) body.allow_local_ai = localSel.value;
  const key = document.getElementById('setApiKey').value;
  if (key) body.api_key = key;
  const master = document.getElementById('setMasterPassword').value;
  if (master) body.master_password = master;
  try {
    await api('/api/settings', { method: 'PUT', body });
    toast(t('set.saved'));
    loadSettings();
  } catch(e) { toast(e.message, 'error'); }
}

async function testSettings() {
  try {
    const r = await api('/api/settings/test', { method: 'POST', body: {} });
    if (r.ok) toast(t('set.connOk').replace('{r}', r.reply));
  } catch(e) { toast(t('set.connFail').replace('{m}', e.message), 'error'); }
}

// ── 数据导入 / 导出 ──
function _downloadFromApi(path, filename) {
  return fetch(path, { headers: { 'X-Requested-With': 'LearnOS' } })
    .then(r => { if (!r.ok) throw new Error(t('export.fail').replace('{s}', r.status)); return r.blob(); })
    .then(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = filename; a.click();
      URL.revokeObjectURL(url);
    });
}

async function exportAnki() {
  try {
    await _downloadFromApi('/api/export?format=anki-csv', `learnos_anki_${new Date().toISOString().slice(0, 10)}.csv`);
    toast(t('export.anki'));
  } catch(e) { toast(e.message, 'error'); }
}

async function exportIcs() {
  try {
    await _downloadFromApi('/api/export?format=ics', `learnos_review_${new Date().toISOString().slice(0, 10)}.ics`);
    toast(t('export.ics'));
  } catch(e) { toast(e.message, 'error'); }
}

async function exportData() {
  try {
    const data = await api('/api/export');
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `learnos_${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast(t('export.data'));
  } catch(e) { toast(e.message, 'error'); }
}

// ── 一键备份 / 还原（全库 JSON）──
async function exportBackup() {
  try {
    await _downloadFromApi('/api/export/backup', `learnos-backup-${new Date().toISOString().slice(0, 10)}.json`);
    toast(t('export.backup'));
  } catch(e) { toast(e.message, 'error'); }
}

async function importBackup(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const ok = await confirmDialog(t('restore.confirm'));
    if (!ok) { input.value = ''; return; }
    const r = await api('/api/import/restore', { method: 'POST', body: { backup: text } });
    const n = Object.values(r.restored || {}).reduce((s, x) => s + x, 0);
    toast(t('restore.done').replace('{n}', n));
    loadDashboard();
  } catch(e) { toast(t('restore.fail').replace('{m}', e.message), 'error'); }
  input.value = '';
}

async function importData(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    const ok = await confirmDialog(t('import.confirm'));
    if (!ok) { input.value = ''; return; }
    const r = await api('/api/import', { method: 'POST', body: data });
    toast(t('import.done').replace('{n}', r.imported).replace('{b}', r.backup.split(/[\\/]/).pop()));
    loadDashboard();
  } catch(e) { toast(t('import.fail').replace('{m}', e.message), 'error'); }
  input.value = '';
}

// ── 公式速查 ──
const _FORMULAS = [
  {key:'formula.kinematics', eqs:['v = v₀ + at','s = v₀t + ½at²','v² − v₀² = 2as','ω = dθ/dt']},
  {key:'formula.dynamics', eqs:['F = ma','F_f ≤ μN','F = −kx (Hooke\x27s law)','p = mv']},
  {key:'formula.workEnergy', eqs:['W = F·s·cosθ','K = ½mv²','W = ΔK','U_g = mgh','U_e = ½kx²']},
  {key:'formula.momentum', eqs:['p_i = p_f','J = Δp = FΔt','perfectly elastic: v₁'+"'"+' = (m₁−m₂)/(m₁+m₂)·v₁']},
  {key:'formula.circular', eqs:['a_c = v²/r = ω²r','F_c = mv²/r','v = ωr','T = 2π/ω']},
  {key:'formula.electro', eqs:['F = kQq/r²','E = F/q','E = kQ/r²','U = Ed (uniform field)']},
  {key:'formula.circuit', eqs:['V = IR','P = IV = I²R','R_s = R₁+R₂+...','1/R_p = 1/R₁+1/R₂+...']},
  {key:'formula.magnet', eqs:['F = qvB·sinθ','F = ILB·sinθ','Φ = BA·cosθ','ε = −dΦ/dt']},
  {key:'formula.thermo', eqs:['PV = nRT','ΔU = Q − W','η = 1 − T_c/T_h','ΔS = Q_rev/T']},
  {key:'formula.waves', eqs:['v = fλ','n = c/v','n₁sinθ₁ = n₂sinθ₂','dsinθ = mλ (double-slit)']},
  {key:'formula.si', eqs:['n 10⁻⁹','μ 10⁻⁶','m 10⁻³','c 10⁻²','k 10³','M 10⁶','G 10⁹']},
];
function toggleFormulaPanel() {
  const p = document.getElementById('formulaPanel');
  const content = document.getElementById('formulaContent');
  if (p.classList.contains('hidden')) {
    content.innerHTML = _FORMULAS.map(c =>
      `<div style="margin-bottom:8px"><strong>${t('formula.' + c.key)}</strong>: ${c.eqs.map(e=>escapeHtml(e)).join(' &nbsp;| ')}</div>`
    ).join('');
    p.classList.remove('hidden');
    renderMath(content);
  } else {
    p.classList.add('hidden');
  }
}

