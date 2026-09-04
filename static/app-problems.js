// 错题：列表 / 详情 / 提示 / 语音 / 批量 / 打印 / 解题方法 / 照片
// ── 错题（真分页 + 搜索 + 排序 + 表格视图 + 保存筛选）──
let problemPage = 1;
let problemPages = 1;
// _searchTimer 复用 app-core.js:150 的全局声明（顶层重复 let 会使本文件整体 SyntaxError）

function problemViewMode() {
  return localStorage.getItem('problemView') === 'table' ? 'table' : 'cards';
}

function toggleProblemView() {
  localStorage.setItem('problemView', problemViewMode() === 'table' ? 'cards' : 'table');
  loadProblems(problemPage);
}

function onSearchInput() {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => loadProblems(1), 250);
  renderSavedFilters();
}

async function loadProblems(page = 1) {
  problemPage = page;
  const listEl = document.getElementById('problemsList');
  const btn = document.getElementById('viewToggleBtn');
  if (btn) btn.textContent = problemViewMode() === 'table' ? t('prob.cardsView') : t('prob.tableView');
  listEl.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  const q = document.getElementById('searchInput').value.trim();
  const sort = document.getElementById('sortSelect').value;
  const params = new URLSearchParams({ page, limit: 20, q, sort });
  const prereqId = getHashParam('prereq');
  if (prereqId) params.set('prereq', prereqId);
  const filterHint = document.getElementById('prereqFilterHint');
  if (filterHint) filterHint.style.display = prereqId ? 'flex' : 'none';
  renderSavedFilters();
  try {
    const data = await api(`/api/problems?${params.toString()}`);
    problemPages = data.pages || 1;
    const items = data.items || data;
    if (!items.length) {
      listEl.innerHTML = `<div class="empty">
        <p>${t('detail.emptyList')}</p>
        <p class="text-sm text-muted" style="margin-top:6px">${t('prob.emptyGuide')}</p>
        <button class="btn btn-primary btn-sm" style="margin-top:10px" onclick="switchPage('bank')">${t('prob.goBank')}</button>
      </div>`;
    } else if (problemViewMode() === 'table') {
      listEl.innerHTML = _problemTable(items);
    } else {
      listEl.innerHTML = items.map(p => `
        <div class="list-item" style="display:flex;gap:10px;align-items:flex-start">
          <input type="checkbox" style="margin-top:3px;accent-color:var(--accent)" onclick="event.stopPropagation();toggleBatch(${p.id},this.checked)" aria-label="${t('detail.pickAria')}">
          <div style="flex:1" onclick="viewProblem(${p.id})">
            <div class="list-item-header">
              <span class="list-item-title">${p.starred ? '⭐ ' : ''}${escapeHtml(p.title)}${miniTrendDots(p.recent_results)}</span>
              ${masteryTag(p.mastery)}
          </div>
          <div class="list-item-meta">${escapeHtml(p.course)} · ${escapeHtml(p.topic)} · ${escapeHtml(errLabel(p.error_type))} · ${escapeHtml(p.created_at)}</div>
          </div>
        </div>`).join('');
    }
    renderPager();
  } catch(e) { toast(e.message, 'error'); }
}

// 思源式表格视图：列排序（点击表头切换），行点击进详情
let _tableSort = { key: 'mastery', asc: true };
function _problemTable(items) {
  const key = _tableSort.key;
  const sorted = [...items].sort((a, b) => {
    const va = a[key] ?? '', vb = b[key] ?? '';
    const cmp = typeof va === 'number' && typeof vb === 'number' ? va - vb : String(va).localeCompare(String(vb));
    return _tableSort.asc ? cmp : -cmp;
  });
  const arrow = k => _tableSort.key === k ? (_tableSort.asc ? ' ↑' : ' ↓') : '';
  return `<div style="overflow-x:auto">
    <table class="text-sm" style="width:100%;border-collapse:collapse">
      <thead><tr style="text-align:left;border-bottom:2px solid var(--border,#ddd)">
        <th style="padding:6px 8px;cursor:pointer" onclick="tableSortBy('title')">${t('prob.colTitle')}${arrow('title')}</th>
        <th style="padding:6px 8px;cursor:pointer" onclick="tableSortBy('mastery')">${t('prob.colMastery')}${arrow('mastery')}</th>
        <th style="padding:6px 8px;cursor:pointer" onclick="tableSortBy('error_type')">${t('prob.colError')}${arrow('error_type')}</th>
        <th style="padding:6px 8px;cursor:pointer" onclick="tableSortBy('topic')">${t('prob.colTopic')}${arrow('topic')}</th>
        <th style="padding:6px 8px;cursor:pointer" onclick="tableSortBy('created_at')">${t('prob.colTime')}${arrow('created_at')}</th>
      </tr></thead>
      <tbody>
      ${sorted.map(p => `
        <tr onclick="viewProblem(${p.id})" style="cursor:pointer;border-bottom:1px solid var(--border,#eee)">
          <td style="padding:6px 8px">${p.starred ? '⭐ ' : ''}${escapeHtml(p.title)}</td>
          <td style="padding:6px 8px">${masteryTag(p.mastery)}</td>
          <td style="padding:6px 8px">${escapeHtml(errLabel(p.error_type))}</td>
          <td style="padding:6px 8px">${escapeHtml(p.topic)}</td>
          <td style="padding:6px 8px" class="text-muted">${escapeHtml(p.created_at)}</td>
        </tr>`).join('')}
      </tbody>
    </table>
  </div>`;
}

function tableSortBy(key) {
  if (_tableSort.key === key) _tableSort.asc = !_tableSort.asc;
  else _tableSort = { key, asc: true };
  loadProblems(problemPage);
}

// ── 保存的筛选器（Dataview 式自定义视图，存本机）──
// C7：走 store.getJSON——脏 JSON 曾致 renderSavedFilters 在每次 loadProblems 同步抛错（列表页白屏）
function _savedFilters() {
  const v = store.getJSON('savedFilters', []);
  return Array.isArray(v) ? v : [];
}

function saveCurrentFilter() {
  const q = document.getElementById('searchInput').value.trim();
  if (!q) { toast(t('prob.filterNeedQuery'), 'error'); return; }
  const name = prompt(t('prob.filterNamePh'));
  if (!name) return;
  const list = _savedFilters();
  list.push({ name, q, sort: document.getElementById('sortSelect').value });
  store.set('savedFilters', JSON.stringify(list.slice(-20)));
  renderSavedFilters();
  toast(t('prob.filterSaved'));
}

function applySavedFilter(i) {
  const f = _savedFilters()[i];
  if (!f) return;
  document.getElementById('searchInput').value = f.q;
  document.getElementById('sortSelect').value = f.sort || 'time';
  loadProblems(1);
}

function deleteSavedFilter(i) {
  const list = _savedFilters();
  list.splice(i, 1);
  store.set('savedFilters', JSON.stringify(list));
  renderSavedFilters();
}

function renderSavedFilters() {
  const wrap = document.getElementById('savedFilterChips');
  if (!wrap) return;
  const list = _savedFilters();
  wrap.innerHTML = list.map((f, i) => `
    <span class="tag tag-gray" style="display:inline-flex;align-items:center;gap:4px;cursor:pointer" onclick="applySavedFilter(${i})">
      ${escapeHtml(f.name)}
      <span onclick="event.stopPropagation();deleteSavedFilter(${i})" style="cursor:pointer" title="delete">×</span>
    </span>`).join('');
  const row = document.getElementById('savedFiltersRow');
  if (row) row.style.display = list.length ? 'flex' : 'none';
}

function renderPager() {
  const pager = document.getElementById('pager');
  if (problemPages <= 1) { pager.innerHTML = ''; return; }
  pager.innerHTML = `
    <button class="btn btn-secondary btn-sm" ${problemPage <= 1 ? 'disabled' : ''} onclick="loadProblems(${problemPage - 1})">${t('pager.prev')}</button>
    <span class="text-sm text-muted">${problemPage} / ${problemPages}</span>
    <button class="btn btn-secondary btn-sm" ${problemPage >= problemPages ? 'disabled' : ''} onclick="loadProblems(${problemPage + 1})">${t('pager.next')}</button>
  `;
}

// ── 未链接提及（Obsidian 式）：错题文本出现但未绑定的概念，一键确认绑定 ──
let _unlinkedDismissed = new Set(JSON.parse(localStorage.getItem('unlinkedDismissed') || '[]'));

async function loadUnlinked() {
  const card = document.getElementById('unlinkedCard');
  const list = document.getElementById('unlinkedList');
  if (!card || !list) return;
  try {
    const r = await api('/api/graph/unlinked');
    const items = (r.items || []).filter(it => !_unlinkedDismissed.has(it.problem_id + ':' + it.concept_id));
    if (!items.length) { card.classList.add('hidden'); return; }
    card.classList.remove('hidden');
    list.innerHTML = items.slice(0, 10).map(it => `
      <div class="flex-between" style="padding:6px 0;border-bottom:1px solid var(--border,#eee)">
        <span class="text-sm" style="flex:1">${escapeHtml(it.problem_title)}
          → <strong>${escapeHtml(it.concept_name)}</strong>
          ${it.matched !== it.concept_name ? `<span class="tag tag-gray">${escapeHtml(it.matched)}</span>` : ''}
        </span>
        <span class="flex gap-8">
          <button class="btn btn-primary btn-sm" onclick="bindMention(${it.problem_id}, ${it.concept_id})">${t('unlinked.bind')}</button>
          <button class="btn btn-secondary btn-sm" onclick="dismissMention(${it.problem_id}, ${it.concept_id})">×</button>
        </span>
      </div>`).join('');
  } catch(e) { /* 静默：建议卡片非关键路径 */ }
}

async function bindMention(pid, cid) {
  try {
    await api('/api/graph/bind', { method: 'POST', body: { problem_id: pid, concept_id: cid } });
    toast(t('unlinked.bound'));
    loadUnlinked();
  } catch(e) { toast(e.message, 'error'); }
}

function dismissMention(pid, cid) {
  _unlinkedDismissed.add(pid + ':' + cid);
  localStorage.setItem('unlinkedDismissed', JSON.stringify([..._unlinkedDismissed].slice(-200)));
  loadUnlinked();
}

async function viewProblem(id) {
  try {
    const p = await api(`/api/problems/${id}`);
    document.getElementById('modalTitle').textContent = (p.starred ? '⭐ ' : '') + p.title;
    let html = `
      <div class="flex gap-8 mb-8">
        <span class="tag tag-blue">${escapeHtml(p.course || t('detail.noCourse'))}</span>
        <span class="tag tag-gray">${escapeHtml(p.topic || t('detail.noTopic'))}</span>
        ${masteryTag(p.mastery)}
      </div>`;
    if (Array.isArray(p.tags) && p.tags.length) {
      html += `<div class="flex gap-8 mb-8" style="flex-wrap:wrap">${p.tags.map(tName =>
        `<span class="chip${p.tags_status === 'suggested' ? ' pending' : ''}">${escapeHtml(String(tName))}</span>`).join('')}</div>`;
    }
    // A2 先修告警：绑定概念的先修掌握度低时提示
    if (Array.isArray(p.prereq_warnings) && p.prereq_warnings.length) {
      html += `<div style="border:1px solid var(--warning);background:var(--warning-light,rgba(240,180,60,.12));border-radius:8px;padding:10px 12px;margin-bottom:12px">
        <div style="font-size:13px;font-weight:600;color:var(--warning);margin-bottom:4px">${t('detail.prereqWarn')}</div>
        ${p.prereq_warnings.map(w =>
          `<span class="tag tag-warn" style="cursor:pointer;margin:2px" title="${t('detail.prereqTitle')}" onclick="openPrereqMode(${w.concept_id})">${escapeHtml(w.name)} ${w.mastery}%</span>`).join('')}
        <div class="text-sm text-muted" style="margin-top:4px">${t('detail.prereqAdvice')}</div>
      </div>`;
    }
    html += `<div class="card" style="border-color:var(--border);margin-bottom:12px">
      <div class="card-title">${t('detail.content')}</div>
      <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(p.content)}</p>
      ${(p.media_list || []).map(m => `<img class="photo-full" src="/${escapeHtml(m)}" alt="${t('common.photoAlt')}" onclick="window.open('/${escapeHtml(m)}','_blank')">`).join('')}
    </div>`;
    if (p.my_attempt) {
      html += `<div class="card" style="border-color:var(--border);margin-bottom:12px">
        <div class="card-title">${t('detail.myAttempt')}</div>
        <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(p.my_attempt)}</p>
      </div>`;
    }
    html += `<div class="card-title mt-16">${t('detail.hintsTitle')}</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},1)" id="hint1btn">${t('detail.hint1')}</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},2)" id="hint2btn">${t('detail.hint2')}</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},3)" id="hint3btn">${t('detail.hint3')}</button>
        <button class="btn btn-secondary btn-sm" onclick="getHint(${id},4)" id="hint4btn">${t('detail.hint4')}</button>
      </div>
      <div id="hintsArea"></div>`;
    if (p.hints && p.hints.length) {
      p.hints.forEach(h => {
        html += `<div class="hint-card"><h4>${t('detail.hintLevel').replace('{l}', h.level)}</h4><p>${escapeHtml(h.content)}</p></div>`;
      });
    }
    html += `<div class="card-title mt-16">${t('detail.methodsTitle')}</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="addMethod(${id})">${t('detail.addMethod')}</button>
      </div>
      <div id="methodsArea">${renderMethods(p.methods || [], id)}</div>
      <div class="card-title mt-16">${t('detail.variantsTitle')}</div>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="generateVariants(${id})" id="genVariantsBtn">${t('detail.genVariants')}</button>
        <button class="btn btn-primary btn-sm hidden" onclick="saveVariants(${id})" id="saveVariantsBtn">${t('detail.saveVariants')}</button>
      </div>
      <div id="variantsArea"></div>
      <div id="savedVariants"></div>
      <div class="card-title mt-16">${t('detail.feynmanTitle')}</div>
      <p class="text-sm text-muted" style="margin-bottom:8px">${t('detail.feynmanDesc')}</p>
      <div class="flex gap-12 mb-8">
        <button class="btn btn-secondary btn-sm" onclick="startFeynman(${id})">${t('detail.startFeynman')}</button>
      </div>
      <div id="feynmanReview"></div>`;
    if (p.feynman_self_review) renderFeynmanReview(p.feynman_self_review);
    renderSavedVariants(p.variants);
    html += `<div class="flex gap-12 mt-16">
      <button class="btn btn-secondary btn-sm" onclick="editProblem(${id})">${t('detail.edit')}</button>
      <button class="btn btn-secondary btn-sm" onclick="toggleStar(${id})">${p.starred ? t('detail.starred') : t('detail.star')}</button>
      <button class="btn btn-danger btn-sm" onclick="deleteProblem(${id})">${t('prob.delete')}</button>
    </div>
    <div id="problemHistory" class="mt-16"></div>
    <div id="relatedProblems" class="mt-16"></div>
    <p class="text-sm text-muted mt-12" style="opacity:0.6">${t('detail.shortcut')}</p>`;
    document.getElementById('modalBody').innerHTML = html;
    renderMath(document.getElementById('modalBody'));
    openModal('problemModal');
    // 异步加载历史 + 关联题目
    loadHistory(id);
    loadRelated(id);
    // 详情弹窗内键盘快捷键
    const modal = document.getElementById('problemModal');
    const onKey = (e) => {
      if (!modal.classList.contains('active')) return;
      const tag = (document.activeElement && document.activeElement.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === '1') getHint(id, 1);
      else if (e.key === '2') getHint(id, 2);
      else if (e.key === '3') getHint(id, 3);
      else if (e.key === '4') getHint(id, 4);
      else if (e.key === 's') toggleStar(id);
      else if (e.key === 'e') editProblem(id);
      else if (e.key === 'd') deleteProblem(id);
    };
    // 用 AbortController 绑定监听器生命周期：注册与注销共用同一 signal，
    // 关闭弹窗时 closeModalDirect 会 abort 它。旧写法先覆盖 _onKey 再 addEventListener，
    // 旧句柄丢失 → 监听器只增不减。这里改名前先 abort，双保险。
    if (modal._keyAc) modal._keyAc.abort();
    modal._keyAc = new AbortController();
    document.addEventListener('keydown', onKey, { signal: modal._keyAc.signal });
  } catch(e) { toast(e.message, 'error'); }
}

async function getHint(id, level) {
  const btn = document.getElementById(`hint${level}btn`);
  btn.disabled = true; btn.textContent = t('msg.loading');
  const signal = trackModalAI('problemModal'); // P2-3：详情弹窗关闭可取消在飞 AI 流
  const area = document.getElementById('hintsArea');
  const levelName = t('hint.levelName').replace('{n}', level);
  const diagnoseHtml = (on) => on ? '<p class="hint-text" style="color:var(--warning)">' + t('hint.diagnose') + '</p>' : '';
  const card = document.createElement('div');
  card.className = 'hint-card';
  card.innerHTML = `<h4>${levelName} <span class="tag tag-green">AI</span> <span class="text-muted text-sm">${t('hint.streaming')}</span></h4><p id="hintStreamText"></p>`;
  area.appendChild(card);
  const streamText = card.querySelector('#hintStreamText');
  // C7 SSE 重连：单次流读取，断流抛错由外层重试
  const streamOnce = async () => {
    const r = await fetch(`/api/problems/${id}/hint`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'LearnOS', 'Accept': 'text/event-stream' },
      body: JSON.stringify({ level, lang: currentLang() }),
      signal, // P2-3：可中断 SSE 流
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || t('msg.requestFail') + ` (${r.status})`);
    }
    const ctype = r.headers.get('Content-Type') || '';
    if (!ctype.includes('text/event-stream')) {
      const data = await r.json();
      const srcTag = data.source === 'ai' ? '<span class="tag tag-green">AI</span>' :
                     data.source === 'fallback' ? '<span class="tag tag-amber">' + t('hint.fallbackTag') + '</span>' : '<span class="tag tag-gray">' + t('hint.cacheTag') + '</span>';
      card.querySelector('.tag-green').textContent = srcTag.replace(/<[^>]+>/g, '').trim();
      if (data.cached) card.querySelector('h4').insertAdjacentHTML('beforeend', ' <span class="tag tag-gray">' + t('hint.cachedTag') + '</span>');
      streamText.textContent = data.content || streamText.textContent;
      if (data.diagnose) card.insertAdjacentHTML('afterbegin', diagnoseHtml(true));
      if (data.sources) card.insertAdjacentHTML('afterbegin', ragSourcesHtml(data.sources));
      renderMath(card);
      return true;
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let done = false;
    // C8：rAF 节流的 KaTeX 渲染——一帧至多一次，done 事件仍强制最终渲染
    let mathScheduled = false;
    function scheduleMathRender(card) {
      if (mathScheduled) return;
      mathScheduled = true;
      requestAnimationFrame(() => { mathScheduled = false; renderMath(card); });
    }
    while (true) {
      let chunk;
      try {
        chunk = await reader.read();
      } catch (e) {
        if (e.name === 'AbortError') throw e; // P2-3：保留取消信号，不在外层误报
        throw new Error('stream');
      }
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop();
      for (const evt of events) {
        const evtMatch = evt.match(/^event: (.+)$/m);
        const dataMatch = evt.match(/^data: (.+)$/m);
        if (!dataMatch) continue;
        const event = evtMatch ? evtMatch[1] : 'message';
        const payload = JSON.parse(dataMatch[1]);
        if (event === 'delta') {
          streamText.textContent += payload.delta || '';
          // C8：KaTeX 全卡重渲按帧节流（每个 delta 都渲是 O(n²)，长回答流式期间明显卡顿）
          scheduleMathRender(card);
        } else if (event === 'sources') {
          if (payload.sources && payload.sources.length) {
            card.insertAdjacentHTML('afterbegin', ragSourcesHtml(payload.sources));
          }
        } else if (event === 'done') {
          streamText.textContent = payload.content || streamText.textContent;
          renderMath(card);
          if (payload.diagnose) card.insertAdjacentHTML('afterbegin', diagnoseHtml(true));
          if (payload.cached) card.querySelector('h4').insertAdjacentHTML('beforeend', ' <span class="tag tag-gray">' + t('hint.cachedTag') + '</span>');
          done = true;
        } else if (event === 'error') {
          if (payload.partial) streamText.textContent = payload.partial;
          if (payload.fallback) {
            streamText.textContent = payload.fallback;
            card.querySelector('.tag-green').textContent = t('hint.fallbackTag');
            card.querySelector('.tag-green').className = 'tag tag-amber';
            toast(t('toast.aiFormat'), 'warn');
          }
          done = true;
        }
      }
    }
    return done;
  };
  try {
    let ok = false;
    for (let attempt = 1; attempt <= 3 && !ok; attempt++) {
      try {
        ok = await streamOnce();
      } catch (e) {
        if (attempt < 3 && streamText.textContent.length) {
          toast(t('toast.reconnect').replace('{n}', attempt), 'warn');
          await new Promise(res => setTimeout(res, 800));
        } else if (attempt >= 3) {
          toast(t('toast.reconnectFail'), 'error');
        } else {
          throw e;
        }
      }
    }
    if (!ok) toast(t('toast.streamLost'), 'error');
    finishHintBtn(btn, levelName);
  } catch(e) {
    if (e.name !== 'AbortError') { toast(e.message, 'error'); } // P2-3：取消不报错
    btn.disabled = false; btn.textContent = levelName;
  }
}

function finishHintBtn(btn, levelName) {
  btn.textContent = t('hint.viewed'); btn.style.opacity = '0.5';
}

function openProblemModal() { editProblem(null); }

async function editProblem(id) {
  const modal = document.getElementById('editModal');
  const titleEl = document.getElementById('editModalTitle');
  if (id) {
    titleEl.textContent = t('edit.title');
    try {
      const p = await api(`/api/problems/${id}`);
      document.getElementById('editId').value = p.id;
      document.getElementById('editTitle').value = p.title || '';
      document.getElementById('editCourse').value = p.course || '';
      document.getElementById('editTopic').value = p.topic || '';
      document.getElementById('editContent').value = p.content || '';
      document.getElementById('editAttempt').value = p.my_attempt || '';
      document.getElementById('editErrorType').value = p.error_type || t('common.pendingDiag');
      document.getElementById('editMastery').value = p.mastery || 1;
      document.getElementById('editStarred').checked = p.starred === 1;
      currentTags = Array.isArray(p.tags) ? p.tags.map(tName => ({ text: String(tName), pending: p.tags_status === 'suggested' })) : [];
      renderTags();
      document.getElementById('editTagInput').value = '';
      renderEditPhotos(Array.isArray(p.media_list) ? p.media_list : []);
    } catch(e) { toast(e.message, 'error'); return; }
  } else {
    titleEl.textContent = t('edit.newTitle');
    document.getElementById('editId').value = '';
    ['editTitle','editCourse','editTopic','editContent','editAttempt'].forEach(i => document.getElementById(i).value = '');
    document.getElementById('editErrorType').value = t('common.pendingDiag');
    document.getElementById('editMastery').value = 1;
    currentTags = [];
    renderTags();
    document.getElementById('editTagInput').value = '';
    renderEditPhotos([]);
    // 草稿回填（P1-c2）：本学科存在未保存草稿时自动填入并轻提示；仅新建分支生效，编辑既有题目不受影响
    const d = loadDraft();
    if (d) {
      if (d.title) document.getElementById('editTitle').value = d.title;
      if (d.course) document.getElementById('editCourse').value = d.course;
      if (d.topic) document.getElementById('editTopic').value = d.topic;
      if (d.content) document.getElementById('editContent').value = d.content;
      if (d.my_attempt) document.getElementById('editAttempt').value = d.my_attempt;
      if (d.error_type) document.getElementById('editErrorType').value = d.error_type;
      if (d.mastery) document.getElementById('editMastery').value = d.mastery;
      if (d.starred) document.getElementById('editStarred').checked = true;
      if (Array.isArray(d.tags) && d.tags.length) {
        currentTags = d.tags.map(tg => ({ text: String(tg), pending: false }));
        renderTags();
      }
      if (d.media_path) { renderEditPhotos(String(d.media_path).split(',').filter(Boolean)); }
      trackEvent('draft.restore'); // P2-5：草稿恢复事件
      toast(t('draft.restored'), 'info');
    }
  }
  document.getElementById('dupHint').textContent = '';
  openModal('editModal');
  // 未保存守卫基线：所有字段填充完成后记录快照，之后任何值变化视为 dirty
  _editModalLast = editSnapshot();
}

// ── C7 相似题查重（编辑弹窗输入时防抖）──
let _dupTimer = null;
function checkDuplicates() {
  clearTimeout(_dupTimer);
  const el = document.getElementById('dupHint');
  if (!el) return;
  el.textContent = '';
  _dupTimer = setTimeout(async () => {
    const content = document.getElementById('editContent').value.trim();
    if (content.length < 20) return;
    try {
      const topic = document.getElementById('editTopic').value.trim();
      const exclude = document.getElementById('editId').value;
      const q = new URLSearchParams({ content, topic, exclude });
      const r = await api('/api/problems/duplicates?' + q.toString());
      if (!r.duplicates || !r.duplicates.length) return;
      const links = r.duplicates.map(d =>
        `<a href="#" onclick="event.preventDefault();viewProblem(${d.id});return false;">#${d.id}（${(d.similarity*100).toFixed(0)}%）</a>`).join('、');
      el.innerHTML = t('dup.found').replace('{n}', r.duplicates.length).replace('{l}', links);
    } catch(e) { /* 静默 */ }
  }, 800);
}

// ── C7 语音输入（webkitSpeechRecognition，Chrome/Edge）──
function startVoiceInput(targetId, btnId = 'voiceBtn') {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = document.getElementById(btnId);
  const ta = document.getElementById(targetId);
  if (!SR) { toast(t('toast.noVoice'), 'warn'); return; }
  if (btn.dataset.rec === '1') {
    btn.dataset.rec = '0';
    btn.textContent = t('voice.start');
    if (_rec) { _rec.stop(); _rec = null; }
    return;
  }
  const rec = new SR();
  _rec = rec;
  rec.lang = 'zh-CN';
  rec.interimResults = true;
  btn.dataset.rec = '1';
  btn.textContent = t('voice.recording');
  rec.onresult = (ev) => {
    let text = '';
    for (let i = 0; i < ev.results.length; i++) text += ev.results[i][0].transcript;
    ta.value = ta.value.replace(/\s*$/, '') + (ta.value.trim() ? '\n' : '') + text;
  };
  rec.onend = () => {
    _rec = null;
    btn.dataset.rec = '0';
    btn.textContent = t('voice.start');
  };
  rec.onerror = (e) => {
    if (e.error !== 'aborted') toast(t('voice.fail') + ': ' + e.error, 'error');
    btn.dataset.rec = '0';
    btn.textContent = t('voice.start');
  };
  rec.start();
}
let _rec = null;

async function saveProblem() {
  const btn = document.getElementById('saveProblemBtn');
  await withButtonLock(btn, async () => {
  const id = document.getElementById('editId').value;
  const body = {
    title: document.getElementById('editTitle').value,
    course: document.getElementById('editCourse').value,
    topic: document.getElementById('editTopic').value,
    content: document.getElementById('editContent').value,
    my_attempt: document.getElementById('editAttempt').value,
    error_type: document.getElementById('editErrorType').value,
    mastery: parseInt(document.getElementById('editMastery').value, 10),
    starred: document.getElementById('editStarred').checked ? 1 : 0,
    tags: currentTags.map(tName => tName.text).filter(Boolean),
    media_path: document.getElementById('editMediaPath').value,
  };
  if (!body.title.trim() || !body.content.trim()) { toast(t('toast.titleRequired'), 'error'); return; }
  try {
    if (id) {
      await api(`/api/problems/${id}`, { method: 'PUT', body });
    } else {
      await api('/api/problems', { method: 'POST', body });
    }
    toast(id ? t('msg.updated') : t('msg.created'));
    _editModalLast = null; // 保存成功：清除 dirty 基线，让 closeModal 直通
    clearDraft();          // 草稿已固化，清除陈旧草稿
    closeModal('editModal');
    loadProblems(problemPage);
  } catch(e) { trackEvent('save.fail'); toast(e.message, 'error'); }
  });
}

async function deleteProblem(id) {
  const ok = await confirmDialog(t('confirm.deleteProblem'));
  if (!ok) return;
  let cancelled = false;
  const toastEl = document.createElement('div');
  toastEl.className = 'toast error';
  toastEl.setAttribute('role', 'status');
  toastEl.setAttribute('aria-live', 'polite');
  const undoLink = document.createElement('a');
  undoLink.href = '#';
  undoLink.style.cssText = 'color:#fff;text-decoration:underline;cursor:pointer';
  undoLink.textContent = t('undo');
  undoLink.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    cancelled = true;
    toastEl.remove();
    toast(t('msg.deleteCancelled'), 'success');
  });
  toastEl.appendChild(document.createTextNode(t('msg.deleted') + ' · '));
  toastEl.appendChild(undoLink);
  document.body.appendChild(toastEl);
  // 10 秒倒计时后真正删除
  await new Promise(r => setTimeout(r, 10000));
  if (cancelled) { toastEl.remove(); return; }
  try {
    await api(`/api/problems/${id}`, { method: 'DELETE' });
    toastEl.remove();
    toast(t('msg.deleted'));
    closeModal('problemModal');
    loadProblems(problemPage);
  } catch(e) { toastEl.remove(); toast(e.message, 'error'); }
}

async function toggleStar(id) {
  try {
    await api('/api/problems/batch', { method: 'POST', body: { ids: [id], action: 'star' } });
    toast(t('msg.starToggled'));
    closeModal('problemModal'); viewProblem(id);
  } catch(e) { toast(e.message, 'error'); }
}

async function loadHistory(id) {
  try {
    const history = await api(`/api/problems/${id}/history`);
    const el = document.getElementById('problemHistory');
    if (!history.length) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="card-title">${t('history.title')}</div>` +
      history.map(h => {
        const labels = {1:'label.flash1',2:'label.flash2',3:'label.flash3',4:'label.flash4'};
        const cls = h.result === '4' ? 'tag-green' : h.result === '3' ? 'tag-blue' : h.result === '2' ? 'tag-amber' : 'tag-red';
        return `<span class="tag ${cls}" style="margin:1px 4px" title="${t('history.intervalTitle').replace('{d}', h.due_date).replace('{i}', h.interval_days)}">${t(labels[h.result]||'')||h.result}</span>`;
      }).join(' ');
  } catch(e) {}
}

async function loadRelated(id) {
  try {
    const related = await api(`/api/problems/${id}/related`);
    const el = document.getElementById('relatedProblems');
    if (!related.length) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="card-title">${t('related.title')}</div>` +
      related.map(r => `<span class="tag tag-gray" style="cursor:pointer;margin:1px 4px" onclick="closeModal('problemModal');viewProblem(${r.id})">${escapeHtml(r.title)}</span>`).join('');
  } catch(e) {}
}

function miniTrendDots(results) {
  if (!results || !results.length) return '';
  const colors = {1:'var(--danger)',2:'var(--warning)',3:'var(--accent)',4:'var(--success)'};
  return '<span style="display:inline-flex;gap:2px;vertical-align:middle;margin-left:6px">' +
    results.map(r => `<span style="width:6px;height:6px;border-radius:50%;background:${colors[r]||'var(--border)'}"></span>`).join('') +
    '</span>';
}

let _batchSelected = new Set();
function toggleBatch(pid, checked) {
  if (checked) _batchSelected.add(pid); else _batchSelected.delete(pid);
  document.getElementById('batchBar').classList.toggle('hidden', _batchSelected.size === 0);
  document.getElementById('batchCount').textContent = _batchSelected.size;
}
async function batchAction(action) {
  const ids = Array.from(_batchSelected);
  if (!ids.length) return;
  if (action === 'delete') {
    const ok = await confirmDialog(t('confirm.batchDelete').replace('{n}', ids.length));
    if (!ok) return;
  }
  try {
    await api('/api/problems/batch', { method: 'POST', body: { ids, action } });
    toast(t('msg.processedN').replace('{n}', ids.length));
    _batchSelected.clear();
    document.getElementById('batchBar').classList.add('hidden');
    loadProblems(problemPage);
  } catch(e) { toast(e.message, 'error'); }
}

// ── C7 打印错题集（尊重当前搜索/排序，全量拉取）──
async function printProblems() {
  const q = document.getElementById('searchInput').value.trim();
  const sort = document.getElementById('sortSelect').value;
  const params = new URLSearchParams({ page: 1, limit: 10000, q, sort });
  try {
    const data = await api('/api/problems?' + params.toString());
    const items = data.items || data;
    if (!items.length) { toast(t('print.noItems'), 'warn'); return; }
    const area = document.getElementById('printArea');
    const sorted = [...items].sort((a, b) => (b.mastery || 0) - (a.mastery || 0));
    area.innerHTML = `<h2>${t('print.bookTitle').replace('{n}', items.length).replace('{d}', new Date().toLocaleDateString())}</h2>` +
      sorted.map(p => `<div class="print-item">
        <div class="print-title">${t('print.pTitle').replace('{t}', escapeHtml(p.title || t('print.unnamed'))).replace('{m}', p.mastery)}</div>
        <div class="print-meta">${t('print.meta').replace('{c}', escapeHtml(p.course || '')).replace('{t}', escapeHtml(p.topic || '')).replace('{e}', escapeHtml(errLabel(p.error_type || '待诊断')))}</div>
        <pre>${escapeHtml(p.content || '')}</pre>
        ${p.my_attempt ? `<div class="print-hdr">${t('print.myAttempt')}</div><pre>${escapeHtml(p.my_attempt)}</pre>` : ''}
        ${p.fix_action ? `<div class="print-hdr">${t('print.fixAction')}</div><pre>${escapeHtml(p.fix_action)}</pre>` : ''}
      </div>`).join('');
    window.print();
  } catch(e) { toast(e.message, 'error'); }
}

// ── A8 一题多解 ──
function renderMethods(methods, id) {
  if (!Array.isArray(methods) || !methods.length) {
    return '<p class="text-sm text-muted">' + t('method.none') + '</p>';
  }
  return '<div class="flex column gap-8">' + methods.map((m, i) =>
    `<div style="border:1px solid var(--border);border-radius:8px;padding:10px 12px">
      <div class="flex-between mb-4">
        <b class="text-sm">${t('method.title').replace('{i}', i + 1)}</b>
        <button class="btn btn-secondary btn-sm" onclick="removeMethod(${id},${i})">${t('method.delete')}</button>
      </div>
      <p class="text-mono text-sm" style="white-space:pre-wrap">${escapeHtml(m)}</p>
    </div>`).join('') + '</div>';
}

async function addMethod(id) {
  const text = window.prompt(t('method.prompt'), '');
  if (text === null) return;
  const p = await api(`/api/problems/${id}`);
  const methods = [...(p.methods || []), text.trim()].filter(Boolean);
  try {
    await api(`/api/problems/${id}`, { method: 'PUT', body: { methods } });
    document.getElementById('methodsArea').innerHTML = renderMethods(methods, id);
    toast(t('method.saved'));
    renderMath(document.getElementById('methodsArea'));
  } catch(e) { toast(e.message, 'error'); }
}

async function removeMethod(id, idx) {
  const p = await api(`/api/problems/${id}`);
  const methods = (p.methods || []).filter((_, i) => i !== idx);
  await api(`/api/problems/${id}`, { method: 'PUT', body: { methods } });
  document.getElementById('methodsArea').innerHTML = renderMethods(methods, id);
  toast(t('method.deleted'));
}

// ── 设置 ──
async function probeLocalModels() {
  const el = document.getElementById('ollamaStatus');
  if (!el) return;
  // 延后探测：避免 3 秒级的探测请求抢占设置页首屏数据的请求通道
  await new Promise(res => setTimeout(res, 600));
  try {
    const r = await api('/api/models/probe');
    if (r.ollama && r.ollama.available) {
      const list = (r.ollama.models || []).slice(0, 5).join(', ');
      el.innerHTML = t('ollama.available').replace('{l}', escapeHtml(list));
      el.style.color = 'var(--success)';
    } else {
      el.textContent = t('ollama.noLocal');
      el.style.color = 'var(--text-2)';
    }
  } catch(e) {
    el.textContent = t('ollama.probeFail');
  }
}

// ── B1 拍照/截图录题 ──
let _editPhotos = []; // 当前表单已上传图片相对路径

function renderEditPhotos(paths) {
  _editPhotos = (paths || []).filter(Boolean);
  document.getElementById('editMediaPath').value = _editPhotos.join(',');
  const wrap = document.getElementById('photoPreviewWrap');
  const btn = document.getElementById('extractPhotoBtn');
  const delBtn = document.getElementById('clearPhotoBtn');
  if (_editPhotos.length) {
    wrap.classList.remove('hidden');
    wrap.innerHTML = _editPhotos.map(p =>
      `<span class="photo-preview"><img src="/${escapeHtml(p)}" alt="${t('common.photoAlt')}"></span>`).join('');
    btn.classList.remove('hidden');
    delBtn.classList.remove('hidden');
  } else {
    wrap.classList.add('hidden');
    wrap.innerHTML = '';
    btn.classList.add('hidden');
    delBtn.classList.add('hidden');
  }
}

async function uploadPhotoBlob(blob) {
  const b64 = await new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(',')[1] || '');
    fr.onerror = reject;
    fr.readAsDataURL(blob);
  });
  const r = await api('/api/upload/photo', { method: 'POST', body: { data: b64, name: blob.name || 'paste.png' } });
  return r.path;
}

async function attachPhoto(blob) {
  try {
    const path = await uploadPhotoBlob(blob);
    renderEditPhotos([..._editPhotos, path]);
    toast(t('photo.uploaded'), 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function extractPhoto() {
  const path = _editPhotos[_editPhotos.length - 1];
  if (!path) return;
  const btn = document.getElementById('extractPhotoBtn');
  btn.disabled = true;
  btn.textContent = t('ocr.recognizing');
  const signal = trackModalAI('editModal'); // P2-3：弹窗关闭可取消
  try {
    const r = await api('/api/ai/extract-photo', { method: 'POST', signal, body: { media_path: path } });
    if (!r.draft) {
      toast(r.error || t('ocr.noVision'), 'info');
      return;
    }
    const d = r.draft;
    if (d.title) document.getElementById('editTitle').value = d.title;
    if (d.topic) document.getElementById('editTopic').value = d.topic;
    if (d.content) document.getElementById('editContent').value = d.content;
    if (d.answer) {
      const hint = document.getElementById('editContent').value;
      document.getElementById('editContent').value = hint + (hint ? '\n\n' : '') +
        t('ocr.answerBlock').replace('{a}', d.answer) + (d.analysis ? t('ocr.analysisBlock').replace('{a}', d.analysis) : '');
    }
    toast(t('ocr.filled'), 'success');
  } catch(e) {
    if (e.name !== 'AbortError') toast(e.message, 'error'); // P2-3：取消不报错
  }
  finally {
    btn.disabled = false;
    btn.textContent = t('ocr.btnTitle');
  }
}

function clearPhoto() { renderEditPhotos([]); }

// 全局粘贴：编辑弹窗打开时，剪贴板图片 → 上传
document.addEventListener('paste', (e) => {
  if (!document.getElementById('editModal').classList.contains('active')) return;
  const items = (e.clipboardData && e.clipboardData.items) || [];
  for (const it of items) {
    if (it.type && it.type.startsWith('image/')) {
      e.preventDefault();
      attachPhoto(it.getAsFile());
      return;
    }
  }
});
document.getElementById('editPhotoFile').addEventListener('change', (e) => {
  const f = e.target.files && e.target.files[0];
  if (f) attachPhoto(f);
  e.target.value = '';
});

