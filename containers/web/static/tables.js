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
  sel.disabled = true;
  try {
    const resp = await fetch(form.action, {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
      body: new FormData(form),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "HTTP " + resp.status);

    const row = form.closest("tr");
    const badge = row.querySelector(".cell-status .badge");
    badge.textContent = data.status;
    badge.className = "badge " + ({ ACTIVE: "ok", DEMO: "warn" }[data.status] || "");
    if (data.magic_number) row.querySelector(".cell-magic").textContent = data.magic_number;
    sel.innerHTML = '<option value="">状态 →</option>' +
      ["CANDIDATE", "DEMO", "ACTIVE", "ARCHIVED"]
        .filter((s) => s !== data.status)
        .map((s) => `<option value="${s}">${s}</option>`).join("");
    row.style.transition = "background .8s";
    row.style.background = "#ecfdf3";           // 成功闪一下绿色
    setTimeout(() => (row.style.background = ""), 800);
  } catch (err) {
    alert("状态修改失败: " + err.message);
    sel.value = "";
  } finally {
    sel.disabled = false;
  }
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
