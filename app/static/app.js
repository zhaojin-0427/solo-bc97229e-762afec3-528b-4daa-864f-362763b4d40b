/* 溯源结果渲染（原生 JS，无外部依赖）。 */

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

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

function renderResult(data) {
  const { result } = data;
  const html = [];

  // 源表概览
  html.push('<div class="section-title">源表与敏感字段</div>');
  html.push('<div class="meta-tables">');
  for (const t of result.base_tables) {
    const cols = t.columns.map((c) =>
      `<span class="meta-col ${c.sensitive ? "sensitive" : ""}">${esc(c.name)}${c.sensitive ? " *" : ""}</span>`
    ).join("");
    html.push(`<div class="meta-table"><b>${esc(t.name)}</b><div>${cols}</div></div>`);
  }
  html.push("</div>");

  // CTE 概览
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

  // 逐输出列
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
  $("result").innerHTML = html.join("");
}

async function runAnalyze() {
  const status = $("status");
  const errBox = $("error");
  errBox.hidden = true;
  status.textContent = "分析中…";
  try {
    const resp = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ schema: $("schema").value, sql: $("sql").value }),
    });
    const data = await resp.json();
    if (!data.ok) {
      errBox.textContent = "语法/溯源错误（已拒绝，未执行任何 SQL）：\n" + data.error;
      errBox.hidden = false;
      $("result").innerHTML = "";
      status.textContent = "失败";
      return;
    }
    renderResult(data);
    const n = data.result.outputs.length;
    const sens = data.result.outputs.filter((o) => o.sensitive).length;
    status.textContent = `完成：${n} 个输出列，其中 ${sens} 个含敏感来源`;
  } catch (e) {
    errBox.textContent = "请求失败：" + e;
    errBox.hidden = false;
    status.textContent = "失败";
  }
}

$("run").addEventListener("click", runAnalyze);
$("reset").addEventListener("click", () => {
  $("schema").value = window.DEMO_SCHEMA;
  $("sql").value = window.DEMO_SQL;
  $("error").hidden = true;
  $("status").textContent = "";
  $("result").innerHTML = "";
});

// 支持 Ctrl/Cmd+Enter 触发
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") runAnalyze();
});

// 首次加载自动分析示例
window.addEventListener("DOMContentLoaded", runAnalyze);
