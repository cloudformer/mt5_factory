/* 点 JSON 按钮: 展开/收起对应的详情行 (data-json-toggle="行id") */
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-json-toggle]");
  if (!btn) return;
  const row = document.getElementById(btn.getAttribute("data-json-toggle"));
  if (row) row.hidden = !row.hidden;
});

/* 状态下拉框 AJAX 原地更新: 改状态不刷新页面, 只更新当前行 */
document.addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-status-select]");
  if (!sel || !sel.value) return;
  const form = sel.form;
  // 必须先取表单数据再禁用: disabled 的控件不会进 FormData, 否则服务端收不到 status
  const body = new FormData(form);
  sel.disabled = true;
  try {
    const resp = await fetch(form.action, {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
      body,
    });
    // 先按文本读: 服务器出错时返回的是 HTML 错误页而非 JSON, 直接解析会得到
    // 无意义的 "unexpected token '<'"。这里把真实内容摘出来提示。
    const raw = await resp.text();
    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      const hint = raw.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim().slice(0, 200);
      throw new Error(`HTTP ${resp.status} (服务器返回的不是JSON): ${hint || "空响应"}`);
    }
    if (!resp.ok) throw new Error(data.error || "HTTP " + resp.status);

    const row = form.closest("tr");
    const badge = row.querySelector(".cell-status .badge");
    if (badge) {
      // 行内有状态列(策略页/回测页): 原地更新
      badge.textContent = data.status;
      badge.className = "badge " + ({ LIVE: "ok", DEMO: "warn" }[data.status] || "");
      const magic = row.querySelector(".cell-magic");
      if (magic && data.magic_number) magic.textContent = data.magic_number;
      sel.innerHTML = '<option value="">状态 →</option>' +
        ["CANDIDATE", "DEMO", "LIVE", "ARCHIVED"]
          .filter((s) => s !== data.status)
          .map((s) => `<option value="${s}">${s}</option>`).join("");
      row.style.transition = "background .8s";
      row.style.background = "#ecfdf3";           // 成功闪一下绿色
      setTimeout(() => (row.style.background = ""), 800);
    } else {
      // 无状态列的页面(Demo/Live 页按状态过滤): 改了状态就不属于本页, 淡出移除
      row.style.transition = "opacity .5s";
      row.style.opacity = "0";
      setTimeout(() => row.remove(), 500);
    }
  } catch (err) {
    alert("状态修改失败: " + err.message);
    sel.value = "";
  } finally {
    sel.disabled = false;
  }
});

/* 表格排序: 表加 data-sortable 后点表头按该列排序(再点反向); 不排序的列标 data-nosort。
   数字列(含 %, +, — 空值)按数值排, 其余按文本; 空值(—)固定沉底 */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("table[data-sortable]").forEach((table) => {
    const headers = [...table.querySelectorAll("tr:first-child th")];
    headers.forEach((th, col) => {
      if (th.hasAttribute("data-nosort")) return;
      th.classList.add("sortable");
      th.addEventListener("click", (e) => {
        if (e.target.closest(".col-resizer")) return; // 拖列宽不触发排序
        const asc = th.dataset.dir !== "asc";
        headers.forEach((h) => { delete h.dataset.dir; h.classList.remove("sort-asc", "sort-desc"); });
        th.dataset.dir = asc ? "asc" : "desc";
        th.classList.add(asc ? "sort-asc" : "sort-desc");

        // 数据行 = 除表头外的行; 跳过空态行(单格 colspan)
        const rows = [...table.querySelectorAll("tr")].slice(1)
          .filter((r) => !r.querySelector("td[colspan]"));
        const cell = (r) => (r.children[col]?.textContent || "").trim();
        const toNum = (t) => parseFloat(t.replace(/[%,+\s]/g, ""));
        const filled = rows.map(cell).filter((v) => v && v !== "—");
        const numeric = filled.length > 0 && filled.every((v) => !isNaN(toNum(v)));

        rows.sort((a, b) => {
          const va = cell(a), vb = cell(b);
          const ea = !va || va === "—", eb = !vb || vb === "—";
          if (ea || eb) return ea - eb;               // 空值沉底
          const c = numeric ? toNum(va) - toNum(vb) : va.localeCompare(vb, "zh");
          return asc ? c : -c;
        }).forEach((r) => r.parentNode.appendChild(r));
        if (table.id) applyTableFilters(table);       // 排序后重算"前N条"与筛选
      });
    });
  });
});

/* 表格过滤三件套 (组合生效, 全部即时):
   - 文本搜索:  <input  data-table-filter="表id">        整行模糊匹配
   - 列下拉筛选: <select data-col-filter="表id:列序号">   选项自动取该列去重值
   - 条数限制:  <select data-table-limit="表id">         只显示过滤后前 N 行
   - 计数显示:  <span   data-table-count="表id">         "显示 x / 共 y" */
function applyTableFilters(table) {
  const tid = table.id;
  const q = (document.querySelector(`input[data-table-filter="${tid}"]`)?.value || "")
    .trim().toLowerCase();
  const colFilters = [...document.querySelectorAll(`select[data-col-filter^="${tid}:"]`)]
    .map((s) => [parseInt(s.getAttribute("data-col-filter").split(":")[1], 10), s.value])
    .filter(([, v]) => v !== "");
  const limit = parseInt(
    document.querySelector(`select[data-table-limit="${tid}"]`)?.value || "0", 10);
  let shown = 0, total = 0;
  [...table.querySelectorAll("tr")].slice(1).forEach((r) => {
    if (r.querySelector("td[colspan]")) return; // 空态行不动
    total++;
    let ok = q === "" || r.textContent.toLowerCase().includes(q);
    if (ok) {
      for (const [col, v] of colFilters) {
        if ((r.children[col]?.textContent || "").trim() !== v) { ok = false; break; }
      }
    }
    if (ok && limit && shown >= limit) ok = false; // 超出条数限制
    if (ok) shown++;
    r.hidden = !ok;
  });
  const counter = document.querySelector(`span[data-table-count="${tid}"]`);
  if (counter) counter.textContent = `显示 ${shown} / 共 ${total}`;
}

function tableOf(el, attr) {
  const v = el.getAttribute(attr);
  return document.getElementById(v.includes(":") ? v.split(":")[0] : v);
}

document.addEventListener("input", (e) => {
  const box = e.target.closest("input[data-table-filter]");
  if (box) { const t = tableOf(box, "data-table-filter"); if (t) applyTableFilters(t); }
});
document.addEventListener("change", (e) => {
  const sel = e.target.closest("select[data-col-filter], select[data-table-limit]");
  if (!sel) return;
  const t = tableOf(sel, sel.hasAttribute("data-col-filter") ? "data-col-filter" : "data-table-limit");
  if (t) applyTableFilters(t);
});

/* 加载时: 列下拉自动收集选项 + 应用默认限制 */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("select[data-col-filter]").forEach((sel) => {
    const table = tableOf(sel, "data-col-filter");
    if (!table) return;
    const col = parseInt(sel.getAttribute("data-col-filter").split(":")[1], 10);
    const values = new Set();
    [...table.querySelectorAll("tr")].slice(1).forEach((r) => {
      if (r.querySelector("td[colspan]")) return;
      const v = (r.children[col]?.textContent || "").trim();
      if (v) values.add(v);
    });
    [...values].sort().forEach((v) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = v;
      sel.appendChild(o);
    });
  });
  document.querySelectorAll("select[data-table-limit]").forEach((sel) => {
    const t = tableOf(sel, "data-table-limit");
    if (t) applyTableFilters(t);
  });
});

/* 表格列宽拖拽: 所有表头列边缘出现拖柄, 按住拖动调整列宽 */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("table").forEach((table) => {
    const headers = table.querySelectorAll("tr:first-child th");
    headers.forEach((th, i) => {
      if (i === headers.length - 1) return; // 最后一列不加
      const grip = document.createElement("div");
      grip.className = "col-resizer";
      th.style.position = "relative";
      th.appendChild(grip);

      grip.addEventListener("mousedown", (e) => {
        e.preventDefault();
        const startX = e.pageX;
        const startW = th.offsetWidth;
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";

        const move = (ev) => {
          th.style.width = Math.max(40, startW + ev.pageX - startX) + "px";
        };
        const up = () => {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
          document.body.style.cursor = "";
          document.body.style.userSelect = "";
        };
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    });
  });
});
