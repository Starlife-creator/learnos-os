// 概览：仪表盘 / 图表 / 通知 / 学习者档案 / 考试冲刺
// ── 概览 ──
async function loadDashboard() {
  const el = document.getElementById('topicsList');
  const recentEl = document.getElementById('recentList');
  const skeleton = (n) => Array(n).fill(0).map(() => '<div class="skeleton"></div>').join('');
  el.innerHTML = skeleton(4);
  if (recentEl) recentEl.innerHTML = skeleton(3);
  try {
    const d = await api('/api/dashboard');
    document.getElementById('statTotal').textContent = d.stats.total || 0;
    document.getElementById('statDue').textContent = d.due || 0;
    document.getElementById('statMastered').textContent = d.stats.mastered || 0;
    const avg = d.stats.avg_mastery || 0;
    document.getElementById('statAvg').textContent = avg.toFixed(1);
    const ring = document.getElementById('statAvgRing');
    if (ring) ring.style.setProperty('--p', Math.round(Math.min(avg, 5) / 5 * 100) + '%');
    updateDueBadge(d.due || 0);
    maybeNotify(d.due || 0);
    renderTodayActions(d);

    if (d.topics && d.topics.length) {
      el.innerHTML = d.topics.map(t => `
        <div class="flex-between mb-8">
          <span class="text-sm">${escapeHtml(t.topic)}</span>
          <span class="flex gap-8 items-center">
            <span class="tag tag-gray">${t('dash.topicCount').replace('{n}', t.count)}</span>
            ${masteryBar(t.mastery)}
          </span>
        </div>`).join('');
    } else {
      el.innerHTML = '<div class="empty"><p>' + t('msg.noData') + '</p></div>';
    }

    const recentEl = document.getElementById('recentList');
    if (d.recent && d.recent.length) {
      recentEl.innerHTML = d.recent.map(p => `
        <div class="list-item" onclick="viewProblem(${p.id})">
          <div class="list-item-header">
            <span class="list-item-title">${escapeHtml(p.title)}</span>
            ${masteryTag(p.mastery)}
          </div>
          <div class="list-item-meta">${escapeHtml(p.course)} · ${escapeHtml(p.topic)} · ${escapeHtml(errLabel(p.error_type))}</div>
        </div>`).join('');
    } else {
      recentEl.innerHTML = '<div class="empty"><p>' + t('msg.noRecent') + '</p></div>';
    }
    drawTrend(d);
    drawAnalytics(d);
    drawErrorDist(d.error_distribution || []);
    drawErrorTrend(d.error_trend || []);
    loadProfile(d);

    // 课程级统计
    const courseEl = document.getElementById('courseStats');
    if (d.course_stats && d.course_stats.length) {
      courseEl.innerHTML = d.course_stats.map(c => `
        <div class="flex-between mb-8">
          <span class="text-sm" style="min-width:80px">${escapeHtml(c.course)}</span>
          <span class="flex gap-8 items-center" style="flex:1">
            <span style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden">
              <span style="display:block;height:100%;width:${(c.avg_mastery/5*100).toFixed(0)}%;background:${c.avg_mastery>=4?'var(--success)':c.avg_mastery>=3?'var(--accent)':'var(--warning)'};border-radius:3px"></span>
            </span>
            <span class="tag tag-gray">${t('dash.courseCount').replace('{m}', c.avg_mastery).replace('{n}', c.count).replace('{d}', c.due>0 ? t('dash.courseDue').replace('{n}', c.due) : '')}</span>
          </span>
        </div>`).join('');
    } else {
      courseEl.innerHTML = '<div class="empty"><p>' + t('msg.noCourse') + '</p></div>';
    }

    // 最近复习活动
    const actEl = document.getElementById('recentActivity');
    if (d.recent_activity && d.recent_activity.length) {
      const labels = {1:'label.rev1',2:'label.rev2',3:'label.rev3',4:'label.rev4'};
      actEl.innerHTML = d.recent_activity.map(a => `
        <div class="list-item" onclick="viewProblem(${a.problem_id})" style="padding:8px 12px">
          <span class="text-sm">📝 ${escapeHtml(a.title)}</span>
          <span class="tag ${a.result==='4'?'tag-green':a.result==='3'?'tag-blue':'tag-amber'}">${t('label.rev'+a.result)||'?'}</span>
          <span class="text-muted text-sm" style="float:right">${(a.created_at||'').slice(0,16)}</span>
        </div>`).join('');
    } else { actEl.innerHTML = '<div class="empty"><p>' + t('msg.noActivity') + '</p></div>'; }
  } catch(e) { toast(e.message, 'error'); }
}

// ── C6 复习提醒：角标 + 浏览器通知 ──
function updateDueBadge(n) {
  const b = document.getElementById('dueBadge');
  if (!b) return;
  b.textContent = n > 99 ? '99+' : n;
  b.classList.toggle('hidden', !(n > 0));
}

function notificationsEnabled() { return localStorage.getItem('notifyEnabled') === '1'; }

function maybeNotify(due) {
  if (!due || !notificationsEnabled() || !('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  const now = Date.now();
  const last = parseInt(localStorage.getItem('notifyLastAt') || '0', 10);
  if (now - last < 10 * 60 * 1000) return;  // 10 分钟节流
  try {
    new Notification(t('notify.title'), { body: t('notify.dueBody').replace('{n}', due), tag: 'due-review' });
    localStorage.setItem('notifyLastAt', String(now));
  } catch(e) { /* 通知失败不影响 */ }
}

async function requestNotifyPermission() {
  if (!('Notification' in window)) { toast(t('notify.unsupported'), 'error'); return; }
  if (Notification.permission === 'granted') { toast(t('notify.granted')); return; }
  const res = await Notification.requestPermission();
  toast(res === 'granted' ? t('notify.on') : t('notify.off'), res === 'granted' ? 'success' : 'error');
}

// C6 轮询：每 5 分钟轻量刷新待复习角标 + 通知
setInterval(async () => {
  try {
    const d = await api('/api/dashboard');
    updateDueBadge(d.due || 0);
    maybeNotify(d.due || 0);
    const { page } = parseHash();
    if (page === 'dashboard' && !document.hidden) loadDashboard();
  } catch(e) { /* 静默失败 */ }
}, 5 * 60 * 1000);

function drawTrend(data) {
  const svg = document.getElementById('trendSvg');
  const hint = document.getElementById('trendHint');
  const log = (data && data.points) || [];
  const summary = (data && data.summary) || {};
  if (!log.length) { svg.innerHTML = ''; hint.textContent = t('trend.empty'); return; }
  const W = 300, H = 120, pad = 10;
  const max = 5, min = 0;
  const n = log.length;
  const x = i => pad + (W - 2 * pad) * (n === 1 ? 0.5 : i / (n - 1));
  const y = v => H - pad - (H - 2 * pad) * ((v - min) / (max - min));
  const pts = log.map((d, i) => `${x(i).toFixed(1)},${y(d.avg_mastery).toFixed(1)}`).join(' ');
  const last = log[log.length - 1];
  svg.innerHTML = `
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="var(--border)" stroke-width="1"/>
    <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2"/>
    <circle cx="${x(n - 1).toFixed(1)}" cy="${y(last.avg_mastery).toFixed(1)}" r="3" fill="var(--accent)"/>
  `;
  const acc = summary.week_accuracy ? t('trend.weekAcc').replace('{p}', summary.week_accuracy) : '';
  hint.textContent = t('trend.summary').replace('{n}', n).replace('{acc}', acc).replace('{m}', last.avg_mastery);
}

// ── D4 复习分析（压力图 + 卡组健康度）──
function drawAnalytics(data) {
  const chart = document.getElementById('dueChart');
  const hint = document.getElementById('dueChartHint');
  if (!chart) return;
  const series = (data && data.due_7d) || [];
  if (!series.length) { chart.innerHTML = ''; hint.textContent = '—'; return; }
  const max = Math.max(1, ...series.map(s => s.due));
  const todayLabel = new Date().toISOString().slice(5, 10);
  chart.innerHTML = series.map(s => {
    const h = Math.max(3, Math.round(s.due / max * 82));
    const isToday = s.date.slice(5) === todayLabel;
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px">
      <span class="text-sm" style="font-size:11px">${s.due || ''}</span>
      <div style="width:100%;max-width:34px;height:${h}px;background:${isToday ? 'var(--warning)' : 'var(--accent)'};border-radius:3px;opacity:${s.due ? 1 : 0.25}"></div>
      <span class="text-sm" style="font-size:10px;color:var(--text-2)">${isToday ? t('label.today') : s.date.slice(5)}</span>
    </div>`;
  }).join('');
  const total7 = series.reduce((n, s) => n + s.due, 0);
  hint.textContent = total7 ? t('dash.due7').replace('{n}', total7) : t('dash.due7None');
  const dh = document.getElementById('deckHealth');
  const h = (data && data.deck_health) || {};
  if (dh) dh.innerHTML = h.total ?
    t('dash.deckHealth').replace('{a}', h.newborn).replace('{b}', h.learning).replace('{c}', h.mature).replace('{t}', h.total).replace('{r}', h.avg_repetition).replace('{m}', h.avg_mastery) :
    t('msg.noData');
  drawPressure((data && data.pressure) || {});
  drawForgetPredict((data && data.forget_predict) || {});
  drawTodayTasks((data && data.tasks) || []);
  drawStubborn((data && data.stubborn) || []);
  drawGamification((data && data.gamification) || {});
  drawTelemetry((data && data.telemetry) || {});
  drawWeekly((data && data.weekly) || {});
  if (data && data.forgetting !== undefined) drawForgetCurve(data.forgetting);
}

// ── D4 遗忘曲线（SVG 纯手绘，零依赖）──
function drawForgetCurve(f) {
  const svg = document.getElementById('forgetSvg');
  const stats = document.getElementById('forgetStats');
  if (!svg || !f) return;
  const W = 340, H = 150, padL = 34, padB = 22, padT = 10, padR = 10;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const NS = 'http://www.w3.org/2000/svg';
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const mk = (tag, attrs) => { const el = document.createElementNS(NS, tag); for (const k in attrs) el.setAttribute(k, attrs[k]); return el; };
  const xOf = t => padL + (t / 30) * plotW;
  const yOf = r => padT + (1 - r) * plotH;
  // 网格 + 轴
  for (const g of [0.5, 0.7, 0.9]) {
    const l = mk('line', { x1: padL, y1: yOf(g), x2: W - padR, y2: yOf(g), stroke: '#e5eaf2', 'stroke-width': 1 });
    svg.appendChild(l);
  }
  svg.appendChild(mk('line', { x1: padL, y1: padT, x2: padL, y2: H - padB, stroke: '#c3cddb', 'stroke-width': 1.2 }));
  svg.appendChild(mk('line', { x1: padL, y1: H - padB, x2: W - padR, y2: H - padB, stroke: '#c3cddb', 'stroke-width': 1.2 }));
  // 预测曲线（平均稳定度）
  if (f.curve && f.curve.length > 1) {
    const d = f.curve.map((p, i) => `${i ? 'L' : 'M'}${xOf(p.t).toFixed(1)},${yOf(p.r).toFixed(1)}`).join(' ');
    const path = mk('path', { d, fill: 'none', stroke: '#3b82f6', 'stroke-width': 2 });
    svg.appendChild(path);
  }
  // 实测桶点（蓝点）
  (f.buckets || []).forEach((b, i) => {
    if (!b.count) return;
    const t = [1.5, 5.5, 11, 22, 45, 80][i] || 20;
    const c = mk('circle', { cx: xOf(t), cy: yOf(b.avg_r), r: 4, fill: '#22c55e', stroke: '#fff', 'stroke-width': 1.5 });
    svg.appendChild(c);
  });
  // 目标保持率虚线
  const target = (parseFloat(localStorage.getItem('fsrsRetention') || '0.9') || 0.9);
  const tl = mk('line', { x1: padL, y1: yOf(target), x2: W - padR, y2: yOf(target), stroke: '#f59e0b', 'stroke-width': 1, 'stroke-dasharray': '4 3' });
  svg.appendChild(tl);
  // 坐标点标
  for (const t of [0, 10, 20, 30]) {
    const lbl = mk('text', { x: xOf(t), y: H - 6, 'text-anchor': 'middle', 'font-size': '9px', fill: '#94a3b8' }, `${t}d`);
    svg.appendChild(lbl);
  }
  if (stats) {
    const total = f.buckets.reduce((a, b) => a + b.count, 0);
    stats.innerHTML = total
      ? t('fsrs.stats').replace('{n}', total).replace('{s}', f.avg_stability || 0) + '<span class="text-muted">' + t('fsrs.legend') + '</span>'
      : '<span class="text-muted">' + t('msg.noFsrs') + '</span>';
  }
}

// ── D6 游戏化 ──
function drawGamification(g) {
  const el = document.getElementById('gameCard');
  if (!el) return;
  if (!g || g.total_reviews === undefined) { el.innerHTML = '<div class="text-muted text-sm">' + t('msg.noData') + '</div>'; return; }
  const unlocked = (g.badges || []).filter(b => b.unlocked);
  el.innerHTML = `<div class="flex-between mb-8">
    <div><span class="text-sm">${t('stat.xpTotal')}</span><div class="text-xl">${g.total_xp}</div></div>
    <div><span class="text-sm">${t('stat.xpToday')}</span><div class="text-xl">${g.today_xp}</div></div>
    <div><span class="text-sm">${t('stat.streak')}</span><div class="text-xl">🔥 ${g.streak}</div></div>
    <div><span class="text-sm">${t('stat.totalRev')}</span><div class="text-xl">${g.total_reviews}</div></div>
  </div>
  <div class="flex wrap gap-8">${(g.badges || []).map(b =>
    `<span class="tag ${b.unlocked ? 'tag-green' : 'tag-gray'}" title="${escapeHtml(b.label)}">${b.unlocked ? '🏅' : '🔒'} ${escapeHtml(b.id.replace('_',' '))}</span>`
  ).join('') || '<span class="text-muted text-sm">' + t('stat.badgeFirst') + '</span>'}</div>`;
}

// ── C6 AI 遥测 ──
function drawTelemetry(t) {
  const el = document.getElementById('telemetryCard');
  if (!el) return;
  if (!t || t.calls === undefined) { el.innerHTML = '<div class="text-muted text-sm">' + t('msg.noData') + '</div>'; return; }
  const rate = t.fail_rate > 0.3 ? 'tag-red' : t.fail_rate > 0.1 ? 'tag-warn' : 'tag-green';
  el.innerHTML = `<div class="flex-between mb-8">
    <span class="text-sm">${t('stat.calls7')}</span><b>${t.calls}</b>
    <span class="text-sm">${t('stat.failRate')}</span><span class="tag ${rate}">${(t.fail_rate * 100).toFixed(0)}%</span>
    <span class="text-sm">${t('stat.avgLatency')}</span><b>${t.avg_latency_ms}ms</b>
    <span class="text-sm">${t('stat.tokens')}</span><b>${t.tokens}</b>
  </div>
  ${t.slow_routes && t.slow_routes.length ? `<p class="hint-text">${t('stat.slowRoutes')}：${t.slow_routes.map(escapeHtml).join('、')}</p>` : ''}`;
}

// ── D5 周报 ──
function drawWeekly(w) {
  const el = document.getElementById('weeklyCard');
  if (!el) return;
  if (!w || w.week_start === undefined) { el.innerHTML = '<div class="text-muted text-sm">' + t('msg.noData') + '</div>'; return; }
  const delta = (w.review_delta || 0);
  const deltaStr = delta > 0 ? `+${delta}` : String(delta);
  el.innerHTML = `<div class="flex-between mb-8">
    <span class="text-sm">${t('stat.weekRange').replace('{s}', escapeHtml(w.week_start))}</span>
  </div>
  <div class="flex-between mb-8">
    <span class="text-sm">${t('stat.newProblem')}</span><b>${w.new_problems}（${t('stat.lastWeek').replace('{n}', w.prev_problems)}）</b>
    <span class="text-sm">${t('stat.reviewCount')}</span><b>${w.week_reviews}（${deltaStr}）</b>
    <span class="text-sm">${t('stat.goodRate')}</span><b>${(w.good_rate * 100).toFixed(0)}%</b>
  </div>
  <p class="hint-text">${t('report.tip').replace('{t}', t(w.tip_key || 'report.tipWeekNone'))}</p>`;
}

// ── P0 顽固错题 ──
function drawStubborn(list) {
  const el = document.getElementById('stubbornList');
  if (!el) return;
  if (!list || !list.length) { el.innerHTML = t('msg.noStubborn'); return; }
  el.innerHTML = list.map(p => {
    const rate = p.total_reviews ? Math.round(p.miss_count / p.total_reviews * 100) : 0;
    return `<div class="flex-between mb-8">
      <a href="#" onclick="event.preventDefault();viewProblem(${p.id});return false;">${escapeHtml(p.title)}</a>
      <span class="text-muted">${t('forgetting.missed').replace('{n}', p.miss_count).replace('{p}', rate).replace('{m}', p.mastery)}</span>
    </div>`;
  }).join('') + '<p class="hint-text mt-8">' + t('stubborn.hint') + '</p>';
}

// ── P0 复习压力指数（PI）──
function drawPressure(p) {
  const el = document.getElementById('pressureCard');
  if (!el) return;
  const color = p.level === t('pressure.high') ? 'var(--danger,#ef4444)' : p.level === t('pressure.mid') ? 'var(--warning)' : 'var(--success)';
  el.innerHTML = p.total == null ? '—' :
    `<div class="flex-between"><span class="text-sm">${t('pressure.score').replace('{s}', p.score)} <b style="color:${color}">${p.level}</b></span>
     <span class="text-sm text-muted">${t('pressure.detail').replace('{o}', p.overdue).replace('{t}', p.today).replace('{tm}', p.tomorrow).replace('{e}', p.est_minutes)}</span></div>
     <div class="error-bar-track mt-8"><div class="error-bar-fill" style="width:${Math.min(100, p.score)}%;background:${color}"></div></div>
     <p class="hint-text mt-8">${p.overdue > 0 ? t('pressure.overdue').replace('{n}', p.overdue) : t('pressure.noOverdue')}</p>`;
}

// ── P0 遗忘预测（FSRS R 值）──
function drawForgetPredict(f) {
  const el = document.getElementById('forgetCard');
  if (!el) return;
  if (!f.count) { el.innerHTML = t('msg.noRisk'); return; }
  const pct = (r) => (r * 100).toFixed(0) + '%';
  el.innerHTML =
    `<div class="text-sm">${t('forget.overview').replace('{n}', f.count).replace('{r}', pct(f.avg_r)).replace('{h}', f.high_risk).replace('{m}', f.medium_risk)}</div>
     ${f.top && f.top.length ? `<div class="mt-8 text-sm">${t('forget.top')}：${f.top.map(t => `<a href="#" onclick="event.preventDefault();viewProblem(${t.problem_id});return false;">${escapeHtml(t.title)}（R=${pct(t.r)}）</a>`).join('、')}</div>` : ''}
     <p class="hint-text mt-8">${t('forget.rehint')}</p>`;
}

// ── P0 今日任务清单 ──
function drawTodayTasks(tasks) {
  const el = document.getElementById('taskCard');
  if (!el) return;
  if (!tasks || !tasks.length) { el.innerHTML = t('msg.noTask'); return; }
  const icons = { review: '📚', error_focus: '🎯', exam: '🏃', done: '✅' };
  el.innerHTML = tasks.map(t =>
    `<div class="flex-between mb-8"><span class="text-sm">${icons[t.kind] || ''} ${escapeHtml(t.label)}</span>
     ${t.kind === 'review' && t.count ? `<a class="btn btn-secondary btn-sm" href="#review">${t('task.goReviewBtn')}</a>` : ''}</div>`
  ).join('');
}

// ── C6 错因分布（横向条形）──
function drawErrorDist(list) {
  const el = document.getElementById('errorDist');
  if (!el) return;
  if (!list || !list.length) { el.innerHTML = '<div class="empty"><p>' + t('msg.noData') + '</p></div>'; return; }
  const total = list.reduce((n, e) => n + e.count, 0);
  const colors = {1:'#ef4444',2:'#f97316',3:'#f59e0b',4:'#3b82f6',5:'#22c55e'};
  el.innerHTML = list.map(e => {
    const pct = Math.round(e.count / total * 100);
    return `<div class="error-bar-row">
      <span class="text-sm" style="min-width:88px">${escapeHtml(e.label)}</span>
      <span class="error-bar-track"><span class="error-bar-fill" style="width:${pct}%;background:${colors[Math.round(e.avg_mastery)] || '#94a3b8'}"></span></span>
      <span class="text-sm text-muted" style="min-width:52px;text-align:right">${t('errDist.item').replace('{n}', e.count).replace('{p}', pct)}</span>
    </div>`;
  }).join('');
  el.insertAdjacentHTML('beforeend', '<p class="hint-text mt-8">' + t('errDist.hint') + '</p>');
}

// ── C5 学习者档案 ──
// ── C7 错因趋势（近 30 天 vs 历史）──
function drawErrorTrend(list) {
  const el = document.getElementById('errorTrend');
  if (!el) return;
  if (!list || !list.length) { el.innerHTML = '<div class="empty"><p>' + t('msg.noData') + '</p></div>'; return; }
  el.innerHTML = list.map(t => {
    const up = t.delta > 0, down = t.delta < 0;
    const arrow = up ? '↗' : down ? '↘' : '→';
    const cls = up ? 'tag-red' : down ? 'tag-green' : 'tag-gray';
    return `<div class="flex-between mb-8">
      <span class="text-sm" style="min-width:88px">${escapeHtml(t.label)}</span>
      <span class="text-sm text-muted" style="flex:1">${t('errTrend.range').replace('{n}', t.recent_count).replace('{p}', t.recent_pct).replace('{h}', t.total_pct)}</span>
      <span class="tag ${cls}">${arrow} ${up ? '+' : ''}${t.delta}%</span>
    </div>`;
  }).join('');
}

// ── C7 考试冲刺卡（倒计时 + 每日计划）──
function renderSprint(goal, stats) {
  const el = document.getElementById('sprintCard');
  if (!el) return;
  if (!goal || !goal.exam_date) { el.innerHTML = t('sprint.noGoal'); return; }
  const days = Math.ceil((new Date(goal.exam_date) - new Date()) / 86400000);
  const target = goal.exam_target_score ? t('sprint.target').replace('{s}', goal.exam_target_score) : '';
  const total = stats ? (stats.total || 0) : 0;
  const mastered = stats ? (stats.mastered || 0) : 0;
  const remaining = Math.max(0, total - mastered);
  let plan = '';
  if (days > 0 && remaining > 0) {
    const perDay = Math.ceil(remaining / days);
    plan = t('sprint.plan').replace('{d}', days).replace('{r}', remaining).replace('{p}', perDay);
  } else if (days <= 0) {
    plan = `<span class="tag tag-red">${t('sprint.passed')}</span>`;
  } else {
    plan = t('sprint.done');
  }
  el.innerHTML = `<div class="text-sm">${plan}</div>
    <div class="text-sm text-muted mt-8">${escapeHtml(goal.exam_date || '')} ${escapeHtml(target)}</div>`;
}

async function loadProfile(dash) {
  try {
    const p = await api('/api/profile');
    const el = document.getElementById('profileBox');
    if (!el) return;
    const topicLine = (p.topics || []).slice(0, 3).map(t =>
      `${escapeHtml(t.topic)} ${t.avg_mastery}/5`).join('、');
    const errLine = (p.errors || []).slice(0, 4).map(e =>
      `${escapeHtml(errLabel(e.error_type))}×${e.count}`).join('、');
    const pace = p.pace || {};
    const goal = p.goal || {};
    let goalText = t('profile.noGoal');
    if (goal.exam_date) {
      const days = Math.ceil((new Date(goal.exam_date) - new Date()) / 86400000);
      goalText = t('profile.goalText').replace('{d}', goal.exam_date).replace('{n}', days) + (goal.exam_target_score ? '，' + t('sprint.target').replace('{s}', goal.exam_target_score) : '');
    }
    el.innerHTML =
      t('profile.topicLine').replace('{v}', topicLine || t('profile.noneTopics')) + '<br>' +
      t('profile.errLine').replace('{v}', errLine || t('profile.noneErrors')) + '<br>' +
      t('profile.paceLine').replace('{r}', pace.week_reviews).replace('{n}', pace.week_new_problems).replace('{h}', pace.peak_hour) + '<br>' +
      t('profile.goalLine').replace('{v}', escapeHtml(goalText));
    document.getElementById('profExamDate').value = goal.exam_date || '';
    document.getElementById('profExamScore').value = goal.exam_target_score || '';
    renderSprint(goal, dash ? dash.stats : null);
  } catch(e) { /* 可选 */ }
}

async function saveProfile() {
  try {
    await api('/api/profile', {
      method: 'PUT',
      body: {
        exam_date: document.getElementById('profExamDate').value,
        exam_target_score: document.getElementById('profExamScore').value,
      },
    });
    toast(t('profile.saved'));
    loadProfile();
  } catch(e) { toast(e.message, 'error'); }
}

// ── 今日行动向导：按优先级渲染行动按钮 ──
function renderTodayActions(d) {
  const card = document.getElementById('todayActions');
  const list = document.getElementById('todayActionsList');
  if (!card || !list) return;
  const items = [];
  if ((d.due || 0) > 0) {
    items.push({
      label: t('today.reviewDue').replace('{n}', d.due),
      page: 'review',
      primary: true,
    });
  }
  const focus = (d.tasks || []).find(x => x.kind === 'error_focus');
  if (focus) {
    items.push({ label: focus.label, page: 'problems' });
  }
  if ((d.stats && d.stats.total) || 0 > 0) {
    items.push({
      label: t('today.wrongbook').replace('{n}', d.stats.total || 0),
      page: 'problems',
    });
  }
  items.push({ label: t('today.bank'), page: 'bank' });
  if (!items.length) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';
  list.innerHTML = items.map(it => `
    <button class="btn ${it.primary ? 'btn-primary' : 'btn-secondary'}"
            onclick="switchPage('${it.page}')">${escapeHtml(it.label)}</button>
  `).join('');
}

