const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { problems: [], reviews: [], oralSession: null };

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}

function toast(message) {
  const el = $("#toast"); el.textContent = message; el.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove("show"), 2600);
}

function dots(value) {
  return `<span class="mastery-dots">${[1,2,3,4,5].map(n => `<i class="${n <= value ? "on" : ""}"></i>`).join("")}</span>`;
}

function switchPage(page) {
  $$(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.page === page));
  $$(".page").forEach(el => el.classList.toggle("active", el.id === `page-${page}`));
  const titles = {dashboard:"学习概览", problems:"错题工作台", reviews:"今日复习", oral:"AI 口试", settings:"AI 设置"};
  $("#pageTitle").textContent = titles[page];
  if (page === "dashboard") loadDashboard();
  if (page === "problems") loadProblems();
  if (page === "reviews") loadReviews();
  if (page === "settings") loadSettings();
  window.scrollTo({top: 0, behavior: "smooth"});
}

function openModal(id) { $(id).classList.remove("hidden"); }
function closeModal(id) { $(id).classList.add("hidden"); }

async function loadDashboard() {
  try {
    const data = await api("/api/dashboard");
    $("#statTotal").textContent = data.stats.total || 0;
    $("#statDue").textContent = data.due || 0;
    $("#statMastery").textContent = Number(data.stats.avg_mastery || 0).toFixed(1);
    $("#statMastered").textContent = data.stats.mastered || 0;
    $("#dueBadge").textContent = data.due;
    $("#dueBadge").classList.toggle("hidden", !data.due);
    $("#topicList").innerHTML = data.topics.length ? data.topics.map(t => `
      <div class="topic-item"><span class="topic-name">${escapeHtml(t.topic)}</span><span class="topic-score">${t.mastery} / 5 · ${t.count}题</span>
      <div class="topic-bar"><i style="width:${Number(t.mastery) * 20}%"></i></div></div>`).join("") : "尚无数据，先记录一道错题。";
    $("#recentList").innerHTML = data.recent.length ? data.recent.map(p => `
      <div class="recent-item" data-problem="${p.id}"><span class="recent-icon">${escapeHtml((p.course || "物")[0])}</span><div><strong>${escapeHtml(p.title)}</strong><small>${escapeHtml(p.course || "未分类")} · ${escapeHtml(p.topic || "未标记知识点")}</small></div>${dots(p.mastery)}</div>`).join("") : "还没有错题记录。";
    $$('[data-problem]', $("#recentList")).forEach(el => el.onclick = () => showProblem(el.dataset.problem));
  } catch (e) { toast(e.message); }
}

async function loadProblems() {
  try {
    state.problems = await api("/api/problems");
    const courses = [...new Set(state.problems.map(p => p.course).filter(Boolean))].sort();
    const selected = $("#courseFilter").value;
    $("#courseFilter").innerHTML = '<option value="">全部课程</option>' + courses.map(c => `<option ${c === selected ? "selected" : ""}>${escapeHtml(c)}</option>`).join("");
    renderProblems();
  } catch (e) { toast(e.message); }
}

function renderProblems() {
  const query = $("#problemSearch").value.trim().toLowerCase();
  const course = $("#courseFilter").value;
  const list = state.problems.filter(p => (!course || p.course === course) && (!query || `${p.title} ${p.course} ${p.topic} ${p.content}`.toLowerCase().includes(query)));
  $("#problemGrid").innerHTML = list.length ? list.map(p => `
    <article class="problem-card" data-id="${p.id}">
      <div class="problem-meta"><span class="tag">${escapeHtml(p.error_type)}</span><span>${escapeHtml(p.course || "未分类")}</span></div>
      <h3>${escapeHtml(p.title)}</h3><p>${escapeHtml(p.content)}</p>
      <div class="problem-foot"><span>${escapeHtml(p.topic || "未标记知识点")}</span>${dots(p.mastery)}</div>
    </article>`).join("") : '<div class="empty-state">没有符合条件的题目。</div>';
  $$(".problem-card").forEach(el => el.onclick = () => showProblem(el.dataset.id));
}

async function showProblem(id) {
  try {
    const p = await api(`/api/problems/${id}`);
    $("#problemDetail").innerHTML = `<div class="detail-content" data-id="${p.id}">
      <span class="kicker">${escapeHtml(p.course || "PHYSICS")} · ${escapeHtml(p.topic || "未标记")}</span>
      <h2>${escapeHtml(p.title)}</h2><div class="detail-meta">错误类型：${escapeHtml(p.error_type)}　掌握度：${p.mastery}/5</div>
      <div class="detail-block"><h4>题目</h4><p>${escapeHtml(p.content)}</p></div>
      <div class="detail-block"><h4>我的尝试</h4><p>${escapeHtml(p.my_attempt || "还没有记录。建议先写下你的模型、方程或具体卡点。")}</p></div>
      <div><h4>逐级提示</h4><div class="hint-actions">${[1,2,3].map(n => `<button data-hint="${n}">${n} 级提示</button>`).join("")}</div><div id="hintOutput">${p.hints.map(h => `<div class="hint-box"><strong>${h.level} 级</strong><br>${escapeHtml(h.content)}</div>`).join("")}</div></div>
      <div style="display:flex;justify-content:flex-end;margin-top:20px"><button class="danger-btn" id="deleteProblem">删除这道题</button></div>
    </div>`;
    $$('[data-hint]').forEach(btn => btn.onclick = () => requestHint(p.id, btn.dataset.hint, btn));
    $("#deleteProblem").onclick = () => deleteProblem(p.id);
    openModal("#detailModal");
  } catch (e) { toast(e.message); }
}

async function requestHint(id, level, button) {
  button.disabled = true; button.textContent = "思考中…";
  try {
    const data = await api(`/api/problems/${id}/hint`, {method:"POST", body:JSON.stringify({level:Number(level)})});
    $("#hintOutput").insertAdjacentHTML("beforeend", `<div class="hint-box"><strong>${level} 级</strong><br>${escapeHtml(data.content)}</div>`);
  } catch (e) { toast(e.message); }
  finally { button.disabled = false; button.textContent = `${level} 级提示`; }
}

async function deleteProblem(id) {
  if (!confirm("确定删除这道题及其复习记录吗？此操作无法撤销。")) return;
  try { await api(`/api/problems/${id}`, {method:"DELETE"}); closeModal("#detailModal"); toast("已删除"); loadProblems(); loadDashboard(); }
  catch (e) { toast(e.message); }
}

async function loadReviews() {
  try {
    state.reviews = await api("/api/reviews");
    const due = state.reviews.filter(r => r.due_date <= new Date().toISOString().slice(0,10));
    $("#reviewList").innerHTML = due.length ? due.map(r => `
      <article class="review-card"><div><span class="kicker">${escapeHtml(r.course || "PHYSICS")} · ${escapeHtml(r.topic || "未标记")}</span><h3>${escapeHtml(r.title)}</h3><p>${escapeHtml(r.content)}</p></div>
      <div class="rating"><span>回忆质量：</span><button data-review="${r.id}" data-rating="1">忘记</button><button data-review="${r.id}" data-rating="2">模糊</button><button data-review="${r.id}" data-rating="3">基本正确</button><button data-review="${r.id}" data-rating="4">完全掌握</button></div></article>`).join("") : '<div class="panel empty-state">今天没有到期任务。可以去错题工作台添加新的学习循环。</div>';
    $$('[data-review]').forEach(btn => btn.onclick = () => completeReview(btn.dataset.review, btn.dataset.rating));
  } catch (e) { toast(e.message); }
}

async function completeReview(id, rating) {
  try { const data = await api(`/api/reviews/${id}/complete`, {method:"POST", body:JSON.stringify({rating:Number(rating)})}); toast(`完成！下次复习：${data.next_due}`); loadReviews(); loadDashboard(); }
  catch (e) { toast(e.message); }
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    $("#apiBase").value = data.api_base || ""; $("#apiKey").value = data.api_key || ""; $("#model").value = data.model || ""; $("#temperature").value = data.temperature || "0.3";
    $("#apiKey").placeholder = data.has_api_key ? "已保存；留空表示不修改" : "sk-…";
  } catch (e) { toast(e.message); }
}

function addMessage(role, content) {
  const box = $("#chatMessages"); $(".chat-placeholder", box)?.remove();
  box.insertAdjacentHTML("beforeend", `<div class="message ${role}">${escapeHtml(content)}</div>`); box.scrollTop = box.scrollHeight;
}

async function startOral() {
  const topic = $("#oralTopic").value.trim(); if (!topic) return toast("请输入口试主题");
  $("#startOral").disabled = true; $("#startOral").textContent = "准备问题…";
  try { const data = await api("/api/oral/start", {method:"POST", body:JSON.stringify({topic})}); state.oralSession = data.session_id; $("#chatMessages").innerHTML = ""; addMessage("assistant", data.reply); $("#oralAnswer").disabled = false; $("#sendOral").disabled = false; $("#oralStatus").textContent = `主题：${topic}`; }
  catch (e) { toast(e.message); }
  finally { $("#startOral").disabled = false; $("#startOral").textContent = "重新开始五轮口试"; }
}

async function sendOral() {
  const answer = $("#oralAnswer").value.trim(); if (!answer || !state.oralSession) return;
  addMessage("user", answer); $("#oralAnswer").value = ""; $("#sendOral").disabled = true;
  try { const data = await api("/api/oral/respond", {method:"POST", body:JSON.stringify({session_id:state.oralSession, answer})}); addMessage("assistant", data.reply); if (data.finished) { $("#oralAnswer").disabled = true; $("#oralStatus").textContent = "本轮已完成"; } else $("#sendOral").disabled = false; }
  catch (e) { toast(e.message); $("#sendOral").disabled = false; }
}

// Navigation and dialogs
$$(".nav-item").forEach(el => el.onclick = () => switchPage(el.dataset.page));
$$('[data-go]').forEach(el => el.onclick = () => switchPage(el.dataset.go));
[$("#quickAdd"), $("#heroAdd"), $("#problemAdd")].forEach(el => el.onclick = () => openModal("#problemModal"));
$$('[data-close]').forEach(el => el.onclick = () => closeModal(`#${el.dataset.close}`));
$$(".modal-backdrop").forEach(el => el.addEventListener("click", e => { if (e.target === el) el.classList.add("hidden"); }));
$("#problemSearch").oninput = renderProblems; $("#courseFilter").onchange = renderProblems;

$("#problemForm").onsubmit = async e => {
  e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); data.mastery = Number(data.mastery);
  try { await api("/api/problems", {method:"POST", body:JSON.stringify(data)}); e.target.reset(); closeModal("#problemModal"); toast("已保存，并安排明日复习"); loadDashboard(); }
  catch (err) { toast(err.message); }
};

$("#settingsForm").onsubmit = async e => {
  e.preventDefault(); const data = {api_base:$("#apiBase").value, api_key:$("#apiKey").value, model:$("#model").value, temperature:$("#temperature").value};
  try { await api("/api/settings", {method:"PUT", body:JSON.stringify(data)}); toast("AI 设置已保存"); loadSettings(); }
  catch (err) { toast(err.message); }
};

$("#testApi").onclick = async () => {
  $("#testApi").disabled = true; const status = $("#apiStatus"); status.className = "notice"; status.textContent = "正在测试连接…";
  try { const data = await api("/api/settings/test", {method:"POST", body:"{}"}); status.textContent = `连接成功：${data.reply}`; }
  catch (e) { status.className = "notice error"; status.textContent = e.message; }
  finally { $("#testApi").disabled = false; }
};

$("#startOral").onclick = startOral; $("#sendOral").onclick = sendOral;
$("#oralAnswer").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendOral(); } });
document.addEventListener("keydown", e => { if (e.key === "Escape") $$(".modal-backdrop").forEach(m => m.classList.add("hidden")); });

loadDashboard();
