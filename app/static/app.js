/* 作业队列前端：提交/轮询/取消/重试，刷新后从服务器恢复进度（原生 JS，无外部依赖）。 */

const $ = (id) => document.getElementById(id);

const STATUS_META = {
  queued:      { label: "排队中",   cls: "st-queued" },
  running:     { label: "执行中",   cls: "st-running" },
  succeeded:   { label: "已完成",   cls: "st-succeeded" },
  failed:      { label: "失败",     cls: "st-failed" },
  cancelled:   { label: "已取消",   cls: "st-cancelled" },
  interrupted: { label: "已中断",   cls: "st-interrupted" },
  timeout:     { label: "超时中止", cls: "st-timeout" },
};
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted", "timeout"]);

let selectedJobId = Number(localStorage.getItem("lineage.selectedJobId")) || null;
let lastDetailSig = null;   // 详情渲染签名：未变化不重绘，保留滚动与展开状态
let lastListSig = null;
let lastAutoSubmit = 0;     // 空队列自动提交示例的节流时间戳

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function api(path, options) {
  const resp = await fetch(path, options);
  return resp.json();
}

/* ---------- 溯源结果渲染（沿用逐列展示） ---------- */

function sensitiveClass(kind) {
  if (kind.includes("value") && kind.includes("condition")) return "tag-both";
  if (kind.includes("value")) return "tag-value";
  if (kind.includes("condition")) return "tag-cond";
  return "tag-clean";
}

function sensitiveLabel(kind) {
  if (kind.includes("value") && kind.includes("condition")) return "值+条件敏感";
  if (kind.includes("value")) return "值依赖敏感";
  if (kind.includes("condition")) return "条件依赖敏感";
  return "无敏感来源";
}

function hopText(hop) {
  const parts = [];
  parts.push(hop.block || "");
  if (hop.clause) parts.push(hop.clause);
  if (hop.ref) parts.push(hop.ref);
  parts.push("→ " + hop.name);
  if (hop.branch) parts.push(`(分支${hop.branch})`);
  return parts.filter(Boolean).join(" ");
}

function renderPaths(paths) {
  if (!paths.length) return '<span class="empty-note">（直接来源，无中间列）</span>';
  return paths.map((path) => {
    const chain = path.map((hop) =>
      `<span class="hop kind-${esc(hop.kind)}">${esc(hopText(hop))}</span>`
    ).join('<span class="arrow">→</span>');
    return `<div class="path-chain">${chain}</div>`;
  }).join("");
}

function renderDepGroup(title, cls, sources, emptyText) {
  const body = sources.length
    ? sources.map((dep) => `
      <div class="dep-item ${dep.sensitive ? "sensitive" : ""} ${cls === "condition" ? "cond" : ""}">
        <div class="dep-base">
          ${esc(dep.table)}.${esc(dep.column)}
          ${dep.sensitive ? '<span class="s-flag">🔒 敏感源字段</span>' : ""}
        </div>
        ${renderPaths(dep.paths)}
      </div>`).join("")
    : `<div class="empty-note">${esc(emptyText)}</div>`;
  return `<div class="dep-group">
    <div class="dep-title ${sources.some((d) => d.sensitive) ? cls : "clean"}">${esc(title)}</div>
    ${body}
  </div>`;
}

function resultHtml(result) {
  const html = [];
  html.push('<div class="section-title">源表与敏感字段</div>');
  html.push('<div class="meta-tables">');
  for (const t of result.base_tables) {
    const cols = t.columns.map((c) =>
      `<span class="meta-col ${c.sensitive ? "sensitive" : ""}">${esc(c.name)}${c.sensitive ? " *" : ""}</span>`
    ).join("");
    html.push(`<div class="meta-table"><b>${esc(t.name)}</b><div>${cols}</div></div>`);
  }
  html.push("</div>");

  if (result.ctes.length) {
    html.push('<div class="section-title">CTE 列敏感性概览</div>');
    for (const cte of result.ctes) {
      const cols = cte.columns.map((c) => {
        const cls = c.sensitive_kind.includes("value") && c.sensitive_kind.includes("condition")
          ? "s-both" : c.sensitive_kind.includes("value")
          ? "s-value" : c.sensitive_kind.includes("condition")
          ? "s-condition" : "";
        return `<span class="cte-col ${cls}">${esc(c.name)}${c.sensitive ? " 🔒" : ""}</span>`;
      }).join("");
      html.push(`<div class="cte-block"><div class="cte-name">WITH ${esc(cte.name)}</div>
        <div class="cte-cols">${cols}</div></div>`);
    }
  }

  html.push('<div class="section-title">最终输出列逐列溯源</div>');
  for (const col of result.outputs) {
    const kind = col.sensitive_kind;
    html.push(`
    <div class="out-card">
      <div class="out-head">
        <span class="out-pos">${col.position}</span>
        <span class="out-name">${esc(col.name)}</span>
        <span class="tag ${sensitiveClass(kind)}">${esc(sensitiveLabel(kind))}</span>
        ${col.from_star ? '<span class="tag tag-clean">星号展开</span>' : ""}
        <span class="out-expr">${esc(col.expr_text || "")}</span>
      </div>
      <div class="out-body">
        ${renderDepGroup("值依赖（输出表达式 / CASE 条件实际引用）", "value",
          col.value_sources, "常量表达式，无源字段值依赖")}
        ${renderDepGroup("条件依赖（本块及沿途 WHERE / JOIN ON）", "condition",
          col.condition_sources, "无 WHERE / ON 条件依赖")}
      </div>
    </div>`);
  }
  return html.join("");
}

/* ---------- 作业列表 ---------- */

function statusBadge(job) {
  const meta = STATUS_META[job.status] || { label: job.status, cls: "" };
  const cancelling = job.cancel_requested && job.status === "running"
    ? '<span class="badge st-cancelling">取消中…</span>' : "";
  return `<span class="badge ${meta.cls}">${meta.label}</span>${cancelling}`;
}

function jobActions(job) {
  const btns = [];
  if (job.status === "queued" || job.status === "running") {
    btns.push(`<button class="mini danger" data-act="cancel" data-id="${job.id}">取消</button>`);
  }
  if (TERMINAL.has(job.status)) {
    btns.push(`<button class="mini" data-act="retry" data-id="${job.id}">重试</button>`);
  }
  return btns.join("");
}

function renderJobList(jobs, runningId) {
  const sig = JSON.stringify([jobs.map((j) =>
    [j.id, j.status, j.cancel_requested, j.queue_ahead, j.finished_at]), runningId, selectedJobId]);
  if (sig === lastListSig) return;
  lastListSig = sig;

  const queuedCount = jobs.filter((j) => j.status === "queued").length;
  $("queue-summary").textContent =
    (runningId ? `执行中 #${runningId} · ` : "执行名额空闲 · ") + `排队 ${queuedCount} 个`;

  if (!jobs.length) {
    $("job-list").innerHTML = '<div class="empty-note">暂无作业，提交后在此查看队列与状态。</div>';
    return;
  }
  $("job-list").innerHTML = jobs.map((j) => `
    <div class="job-row ${j.id === selectedJobId ? "selected" : ""}" data-id="${j.id}">
      <span class="job-id">#${j.id}</span>
      ${statusBadge(j)}
      <span class="job-sql" title="${esc(j.sql_preview)}">${esc(j.sql_preview)}</span>
      <span class="job-meta">${j.status === "queued" && j.queue_ahead > 0 ? `前方 ${j.queue_ahead} 个 · ` : ""}${fmtTime(j.created_at)}</span>
      <span class="job-actions">${jobActions(j)}</span>
    </div>`).join("");
}

/* ---------- 作业详情 ---------- */

function detailHtml(job) {
  const meta = STATUS_META[job.status] || { label: job.status, cls: "" };
  const html = [];
  html.push(`<div class="detail-head">
    <span class="job-id">作业 #${job.id}</span>
    <span class="badge ${meta.cls}">${meta.label}</span>
    ${job.cancel_requested && job.status === "running" ? '<span class="badge st-cancelling">取消中…</span>' : ""}
    <span class="detail-actions">${jobActions(job)}</span>
  </div>`);
  html.push(`<div class="detail-times">
    提交 ${fmtTime(job.created_at)} · 开始 ${fmtTime(job.started_at)} · 结束 ${fmtTime(job.finished_at)}
  </div>`);

  if (job.status === "queued") {
    html.push(`<div class="note-box">排队中${job.queue_ahead ? `（前方还有 ${job.queue_ahead} 个作业）` : ""}，执行名额空闲后自动开始。</div>`);
  }
  if (job.status === "interrupted") {
    html.push('<div class="note-box warn">执行器重启时该作业仍在执行，已标记为中断。可点击「重试」以原输入生成新作业。</div>');
  }
  if (job.status === "cancelled") {
    html.push('<div class="note-box">作业已取消，实际计算已停止，执行名额已释放。</div>');
  }
  if (job.error) {
    html.push(`<div class="error-box">${esc(job.error)}</div>`);
  }

  html.push(`<details class="io" ${TERMINAL.has(job.status) ? "" : "open"}>
    <summary>提交时固定的输入（源表结构 + SQL）</summary>
    <div class="io-grid">
      <div><div class="io-label">源表结构</div><pre>${esc(job.schema)}</pre></div>
      <div><div class="io-label">SQL</div><pre>${esc(job.sql)}</pre></div>
    </div>
  </details>`);

  if (job.result) {
    html.push(resultHtml(job.result));
  } else if (job.status === "running") {
    html.push('<div class="empty-note">正在计算，完成后自动展示逐列溯源结果…</div>');
  }
  return html.join("");
}

async function refreshDetail() {
  if (!selectedJobId) return;
  const data = await api(`/api/jobs/${selectedJobId}`);
  if (!data.ok) {
    $("job-detail").innerHTML = `<div class="empty-note">${esc(data.error || "作业不存在")}</div>`;
    return;
  }
  const job = data.job;
  const sig = JSON.stringify([job.id, job.status, job.error, job.finished_at,
    job.cancel_requested, job.queue_ahead, !!job.result]);
  if (sig === lastDetailSig) return;
  lastDetailSig = sig;
  $("job-detail").innerHTML = detailHtml(job);
}

/* ---------- 轮询 ---------- */

async function refreshJobs() {
  let data;
  try {
    data = await api("/api/jobs");
  } catch (e) {
    return;  // 网络抖动，下一轮再试
  }
  if (!data.ok) return;

  if (!data.jobs.length) {
    // 全新环境：自动提交一份示例作业，打开页面即可看到完整流程（失败时节流重试）
    if (Date.now() - lastAutoSubmit > 5000) {
      lastAutoSubmit = Date.now();
      await submitJob(true);
    }
    return;
  }
  if (selectedJobId && !data.jobs.some((j) => j.id === selectedJobId)) {
    selectedJobId = null;
  }
  if (!selectedJobId) {
    selectJob(data.jobs[0].id, false);
  }
  renderJobList(data.jobs, data.running_id);
  await refreshDetail();
}

/* ---------- 操作 ---------- */

function selectJob(id, refresh = true) {
  selectedJobId = id;
  lastDetailSig = null;
  localStorage.setItem("lineage.selectedJobId", String(id));
  document.querySelectorAll(".job-row").forEach((row) => {
    row.classList.toggle("selected", Number(row.dataset.id) === id);
  });
  if (refresh) refreshDetail();
}

async function submitJob(silent = false) {
  const errBox = $("error");
  errBox.hidden = true;
  let resp;
  try {
    resp = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ schema: $("schema").value, sql: $("sql").value }),
    });
  } catch (e) {
    if (!silent) {
      errBox.textContent = "提交失败：" + e;
      errBox.hidden = false;
    }
    return;
  }
  if (!resp.ok) {
    errBox.textContent = "提交失败：" + (resp.error || "未知错误");
    errBox.hidden = false;
    return;
  }
  $("form-status").textContent = `已提交，作业号 #${resp.job_id}`;
  selectJob(resp.job_id, false);
  lastListSig = null;
  await refreshJobs();
}

async function cancelJob(id) {
  try {
    const resp = await api(`/api/jobs/${id}/cancel`, { method: "POST" });
    if (!resp.ok) {
      $("form-status").textContent = resp.error || "取消失败";
    }
  } catch (e) {
    $("form-status").textContent = "取消请求失败：" + e;
  }
  lastListSig = null;
  await refreshJobs();
}

async function retryJob(id) {
  let resp;
  try {
    resp = await api(`/api/jobs/${id}/retry`, { method: "POST" });
  } catch (e) {
    $("form-status").textContent = "重试请求失败：" + e;
    return;
  }
  if (!resp.ok) {
    $("form-status").textContent = resp.error || "重试失败";
    return;
  }
  $("form-status").textContent = `已以原输入重试，新作业号 #${resp.job_id}`;
  selectJob(resp.job_id, false);
  lastListSig = null;
  await refreshJobs();
}

/* ---------- 事件 ---------- */

$("submit").addEventListener("click", () => submitJob());
$("reset").addEventListener("click", () => {
  $("schema").value = window.DEMO_SCHEMA;
  $("sql").value = window.DEMO_SQL;
  $("error").hidden = true;
  $("form-status").textContent = "";
});

$("job-list").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (btn) {
    e.stopPropagation();
    const id = Number(btn.dataset.id);
    if (btn.dataset.act === "cancel") cancelJob(id);
    if (btn.dataset.act === "retry") retryJob(id);
    return;
  }
  const row = e.target.closest(".job-row");
  if (row) selectJob(Number(row.dataset.id));
});

$("job-detail").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  if (btn.dataset.act === "cancel") cancelJob(id);
  if (btn.dataset.act === "retry") retryJob(id);
});

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") submitJob();
});

window.addEventListener("DOMContentLoaded", () => {
  refreshJobs();
  setInterval(refreshJobs, 1000);
});
