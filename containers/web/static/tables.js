/* 点 JSON 按钮: 展开/收起对应的详情行 (data-json-toggle="行id")。
   手风琴: 展开一条时先收起同表其它已展开的详情行(展开别条=自动收缩上一条)。 */
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-json-toggle]");
  if (!btn) return;
  const row = document.getElementById(btn.getAttribute("data-json-toggle"));
  if (!row) return;
  const willOpen = row.hidden;
  const table = row.closest("table");
  if (table) table.querySelectorAll("tr.detail-row").forEach((r) => (r.hidden = true));
  row.hidden = !willOpen;   // 再点同一条=收起
});

/* 行标记(全站): 点数据行加高亮标记, 再点取消, 点别行移到别行(同表只标一行)。
   纯视觉, 方便宽表横滑时对着看; 点按钮/链接/表单控件不触发, 详情行不参与 */
document.addEventListener("click", (e) => {
  if (e.target.closest("button, a, input, select, label, form")) return;  // 交互元素不误触
  const tr = e.target.closest("tr");
  if (!tr || !tr.parentNode.closest("table")) return;
  if (tr.querySelector("th")) return;              // 表头行不标记
  if (tr.classList.contains("detail-row")) return; // 详情展开行不标记
  if (tr.querySelector("td.empty")) return;        // 空态行不标记
  const table = tr.closest("table");
  const wasMarked = tr.classList.contains("row-marked");
  table.querySelectorAll("tr.row-marked").forEach((r) => r.classList.remove("row-marked"));
  if (!wasMarked) tr.classList.add("row-marked");  // 再点同一行=取消
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
      // 状态中文名 (显示用; 提交值仍英文, 与 _macros.status_label 一致)
      const ZH = { CANDIDATE: "候选", DEMO: "模拟", LIVE: "实盘", ARCHIVED: "淘汰归档" };
      badge.textContent = ZH[data.status] || data.status;
      badge.className = "badge " + ({ LIVE: "ok", DEMO: "warn" }[data.status] || "");
      const magic = row.querySelector(".cell-magic");
      if (magic && data.magic_number) magic.textContent = data.magic_number;
      sel.innerHTML = '<option value="">状态 →</option>' +
        ["CANDIDATE", "DEMO", "LIVE", "ARCHIVED"]
          .filter((s) => s !== data.status)
          .map((s) => `<option value="${s}">${ZH[s] || s}</option>`).join("");
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

/* 表格排序 (全站自动, 无需标记): 所有 table 点表头即可排序(再点反向); 个别列不想排标 data-nosort。
   数字列(含 %, +, — 空值)按数值排, 其余按文本; 空值(—)固定沉底 */
function initTableSort(table) {
  if (table.dataset.sortInit) return;  // 防重复绑定(支持 AJAX 换表后重入)
  // 有配对详情行的表(如 Workers 主行+隐藏详情)排序会把两者拆散, 默认跳过;
  // 标了 data-detail-sort 的表(回测结果排名)要排序: 排序只动主行, 结束后按 toggle id 把详情行粘回其后
  const detailSort = table.hasAttribute("data-detail-sort");
  if (table.querySelector(".detail-row") && !detailSort) return;
  const headers = [...table.querySelectorAll("tr:first-child th")];
  if (headers.length < 2) return;  // 单列表(如无表头的小结构)不处理
  table.dataset.sortInit = "1";
  headers.forEach((th, col) => {
      if (th.hasAttribute("data-nosort")) return;
      th.classList.add("sortable");
      th.addEventListener("click", (e) => {
        if (e.target.closest(".col-resizer")) return; // 拖列宽不触发排序
        const asc = th.dataset.dir !== "asc";
        headers.forEach((h) => { delete h.dataset.dir; h.classList.remove("sort-asc", "sort-desc"); });
        th.dataset.dir = asc ? "asc" : "desc";
        th.classList.add(asc ? "sort-asc" : "sort-desc");

        // 数据行 = 除表头外的行; 跳过空态行(单格 colspan); 排除嵌套子表(展开明细里的 subtable)的行
        const rows = [...table.querySelectorAll("tr")].slice(1)
          .filter((r) => r.closest("table") === table && !r.querySelector("td[colspan]"));
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
        // 详情行跟随: 排序只重排了主行(rows 已排除 colspan 详情行), 把每主行的详情行粘回其后
        if (detailSort) rows.forEach((r) => {
          const btn = r.querySelector("[data-json-toggle]");
          const det = btn && document.getElementById(btn.getAttribute("data-json-toggle"));
          if (det) r.after(det);
        });
        if (table.id) { table.dataset.page = "1"; applyTableFilters(table); }  // 排序后回到第1页
      });
    });
}
window.initTableSort = initTableSort;
document.addEventListener("DOMContentLoaded", () =>
  document.querySelectorAll("table").forEach(initTableSort));

/* 表格过滤+分页 (组合生效, 全部即时):
   - 文本搜索:  <input  data-table-filter="表id">        整行模糊匹配
   - 列下拉筛选: <select data-col-filter="表id:列序号">   选项自动取该列去重值
   - 每页条数:  <select data-table-limit="表id">
   - 翻页按钮:  <button data-table-page="表id:prev|next">
   - 页码显示:  <span   data-table-pageinfo="表id">      "x / y 页"
   - 计数显示:  <span   data-table-count="表id">         "本页 x / 匹配 y / 共 z" */
function applyTableFilters(table) {
  const tid = table.id;
  // 排序/筛选/翻页时把展开的详情行全部收起(干别的就自动收缩, 避免翻页后残留悬空)
  table.querySelectorAll("tr.detail-row").forEach((r) => (r.hidden = true));
  const q = (document.querySelector(`input[data-table-filter="${tid}"]`)?.value || "")
    .trim().toLowerCase();
  const colFilters = [...document.querySelectorAll(`select[data-col-filter^="${tid}:"]`)]
    .map((s) => [parseInt(s.getAttribute("data-col-filter").split(":")[1], 10), s.value])
    .filter(([, v]) => v !== "");
  const limit = parseInt(
    document.querySelector(`select[data-table-limit="${tid}"]`)?.value || "0", 10);

  const rows = [...table.querySelectorAll("tr")].slice(1)
    .filter((r) => r.closest("table") === table && !r.querySelector("td[colspan]")); // 空态/嵌套子表行不动
  const matched = rows.filter((r) => {
    if (q !== "" && !r.textContent.toLowerCase().includes(q)) return false;
    for (const [col, v] of colFilters) {
      if ((r.children[col]?.textContent || "").trim() !== v) return false;
    }
    return true;
  });

  // 分页窗口: 当前页存在 table.dataset.page, 筛选变化时由调用方重置为 1
  const pages = limit ? Math.max(1, Math.ceil(matched.length / limit)) : 1;
  const page = Math.min(Math.max(1, parseInt(table.dataset.page || "1", 10)), pages);
  table.dataset.page = page;
  const start = limit ? (page - 1) * limit : 0;
  const visible = new Set(matched.slice(start, limit ? start + limit : matched.length));
  rows.forEach((r) => { r.hidden = !visible.has(r); });
  // 动态序号: 行内有 td.rownum 的表, 按当前筛选+排序顺序编号 (跨页连续, 第2页从 N+1 起)
  matched.forEach((r, i) => {
    const c = r.querySelector("td.rownum");
    if (c) c.textContent = i + 1;
  });

  const counter = document.querySelector(`span[data-table-count="${tid}"]`);
  if (counter) {
    counter.textContent = `本页 ${visible.size} / 匹配 ${matched.length}`
      + (matched.length !== rows.length ? ` / 共 ${rows.length}` : "");
  }
  const info = document.querySelector(`span[data-table-pageinfo="${tid}"]`);
  if (info) info.textContent = `${page} / ${pages} 页`;
  document.querySelectorAll(`button[data-table-page^="${tid}:"]`).forEach((b) => {
    const dir = b.getAttribute("data-table-page").split(":")[1];
    b.disabled = dir === "prev" ? page <= 1 : page >= pages;
  });
}

/* 翻页 */
document.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-table-page]");
  if (!btn) return;
  const [tid, dir] = btn.getAttribute("data-table-page").split(":");
  const table = document.getElementById(tid);
  if (!table) return;
  table.dataset.page = String(parseInt(table.dataset.page || "1", 10) + (dir === "prev" ? -1 : 1));
  applyTableFilters(table);
});

function tableOf(el, attr) {
  const v = el.getAttribute(attr);
  return document.getElementById(v.includes(":") ? v.split(":")[0] : v);
}

document.addEventListener("input", (e) => {
  const box = e.target.closest("input[data-table-filter]");
  if (!box) return;
  const t = tableOf(box, "data-table-filter");
  if (t) { t.dataset.page = "1"; applyTableFilters(t); }  // 筛选变了回到第1页
});
document.addEventListener("change", (e) => {
  const sel = e.target.closest("select[data-col-filter], select[data-table-limit]");
  if (!sel) return;
  const t = tableOf(sel, sel.hasAttribute("data-col-filter") ? "data-col-filter" : "data-table-limit");
  if (t) { t.dataset.page = "1"; applyTableFilters(t); }
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
  // 初始化: 有序号列或条数限制的表先套用一遍 (填充序号 / 应用默认每页条数)
  document.querySelectorAll("table[id]").forEach((t) => {
    if (t.querySelector("td.rownum")
        || document.querySelector(`select[data-table-limit="${t.id}"]`)) {
      applyTableFilters(t);
    }
  });
});

/* 系统时间戳本地化: 库里存 UTC, <span class="localtime" data-utc="..."> 转成浏览器本地时区。
   只作用于系统时间(心跳/创建等); 交易/bar 时间是券商服务器时间, 不带此类, 保持原样 */
document.addEventListener("DOMContentLoaded", () => {
  const p = (n) => String(n).padStart(2, "0");
  document.querySelectorAll(".localtime[data-utc]").forEach((el) => {
    const raw = el.getAttribute("data-utc");
    const d = new Date(raw);
    if (isNaN(d)) return;
    el.textContent = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
      + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    el.title = "本地时间(存储为 UTC)";
  });
});

/* 表格列宽拖拽 (全站统一, 电子表格式: 拖谁只动谁, 别的列不跳):
   拖动瞬间把所有列宽固定成当前像素 + table-layout:fixed, 之后只改被拖的列和表总宽,
   表变宽超出容器时靠 section 的 overflow-x 横向滚动。所有 table 自动生效, 无需标记 */
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("table").forEach((table) => {
    const ths = [...table.querySelectorAll("tr:first-child th")];
    if (ths.length < 2) return;
    ths.forEach((th) => {
      const grip = document.createElement("div");
      grip.className = "col-resizer";
      th.style.position = "relative";
      th.appendChild(grip);

      grip.addEventListener("mousedown", (e) => {
        e.preventDefault();
        // 冻结当前布局: 各列固定为现有像素宽, 切 fixed 让改动只作用于目标列
        ths.forEach((h) => { h.style.width = h.offsetWidth + "px"; });
        table.style.tableLayout = "fixed";
        table.style.minWidth = "0";                 // 解除 max-content, 改由列宽之和决定
        table.style.width = table.offsetWidth + "px";
        const startX = e.pageX, startW = th.offsetWidth, startTable = table.offsetWidth;

        const move = (ev) => {
          const w = Math.max(40, startW + ev.pageX - startX);
          table.style.width = startTable + (w - startW) + "px";  // 表总宽同步, 别的列不被压
          th.style.width = w + "px";
        };
        const up = () => {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
          document.body.style.cursor = "";
          document.body.style.userSelect = "";
        };
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    });
  });
});

/* ===== 列定制(标准组件): <table id data-colpick> 的工具条出现「列 ▾」下拉 =====
   勾选显隐列; 选择存 localStorage(键 cols:<表id>), 刷新/翻页/排序都保持。
   # 序号列不给关; 明细行(单td colspan)不受影响(nth-child 匹配不到)。 */
function initColPick(table) {
  const tid = table.id;
  if (!tid) return;
  const anchor = document.querySelector(`[data-table-filter="${tid}"]`);
  if (!anchor) return;                       // 需要配套 table_toolbar
  const headers = [...table.querySelectorAll("tr:first-child th")];
  const KEY = "cols:" + tid;
  let hidden;
  try { hidden = new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); }
  catch { hidden = new Set(); }
  const styleEl = document.createElement("style");
  document.head.appendChild(styleEl);
  const apply = () => {
    styleEl.textContent = [...hidden].map((i) =>
      `#${tid} tr > th:nth-child(${i + 1}), #${tid} tr > td:nth-child(${i + 1}){display:none}`
    ).join("\n");
    localStorage.setItem(KEY, JSON.stringify([...hidden]));
  };
  const wrap = document.createElement("span");
  wrap.className = "colpick";
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "small"; btn.textContent = "列 ▾";
  btn.title = "定制显示哪些列(记在本浏览器)";
  const panel = document.createElement("div");
  panel.className = "colpick-panel"; panel.hidden = true;
  headers.forEach((th, i) => {
    const name = th.textContent.trim().replace(/\s+/g, " ");
    if (!name || name === "#") return;       // 序号列常显
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !hidden.has(i);
    cb.addEventListener("change", () => { cb.checked ? hidden.delete(i) : hidden.add(i); apply(); });
    lab.appendChild(cb); lab.appendChild(document.createTextNode(" " + name));
    panel.appendChild(lab);
  });
  btn.addEventListener("click", () => (panel.hidden = !panel.hidden));
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) panel.hidden = true; });
  wrap.appendChild(btn); wrap.appendChild(panel);
  anchor.parentNode.insertBefore(wrap, anchor);
  apply();
}
document.addEventListener("DOMContentLoaded", () =>
  document.querySelectorAll("table[data-colpick]").forEach(initColPick));
