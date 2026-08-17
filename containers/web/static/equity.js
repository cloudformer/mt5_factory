/* 资金曲线渲染器(2026-08-17, 唯一源 — 单策略分析页与多策略对比页共用, 改这里两页生效)
   容器 = [data-eq-chart](标记见 _equity_chart.html);
   数据 = 容器内 script[data-eq-data](单策略, 服务端嵌入) 或 容器._eqSeries(多策略, fetch 后赋值);
   series 格式: [{id, name, symbol, equity: [[出场时间, 累计净点, 单笔净点], ...]}, ...]
   换算口径: 钱 = 初始资金 + 累计净点 × 手数(1点=1USD/标准手; 事实只有净点, 钱是显示换算)
   缩放: data-eq-zoom 按钮(全部/10/5/2/1年, 从最新往回), 纯前端裁窗重画, y 自适应可见段 */
(function () {
  const COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#9333ea",
                  "#0891b2", "#db2777", "#65a30d", "#475569", "#b45309"];

  function seriesOf(box) {
    if (box._eqSeries) return box._eqSeries;
    const tag = box.querySelector("script[data-eq-data]");
    if (!tag) return [];
    try { return [{ equity: JSON.parse(tag.textContent) }]; } catch (e) { return []; }
  }

  function draw(box) {
    const svg = box.querySelector("[data-eq-svg]");
    if (!svg) return;
    const endEl = box.querySelector("[data-eq-end]");
    const legEl = box.querySelector("[data-eq-legend]");
    const all = seriesOf(box).filter((s) => (s.equity || []).length > 1);
    if (!all.length) {
      svg.innerHTML = "";
      if (endEl) endEl.textContent = "";
      if (legEl) legEl.textContent = "";
      return;
    }
    const init = parseFloat(box.querySelector("[data-eq-init]")?.value) || 10000;
    const lots = parseFloat(box.querySelector("[data-eq-lots]")?.value) || 0.01;
    const tMaxFull = Math.max(...all.map((s) => s.equity[s.equity.length - 1][0]));
    const tMinFull = Math.min(...all.map((s) => s.equity[0][0]));
    let tMin, tMax;
    if (box._eqWin) {                              // 拖拽/滚轮的自定义窗口
      tMin = Math.max(tMinFull, box._eqWin[0]);
      tMax = Math.min(tMaxFull, box._eqWin[1]);
      if (tMax - tMin < 86400) { tMin = tMinFull; tMax = tMaxFull; }
    } else {                                       // 预设档: 最近 N 年
      const zoomY = parseFloat(box.dataset.zoom || "0");
      tMax = tMaxFull;
      tMin = zoomY > 0 ? Math.max(tMinFull, tMax - zoomY * 31557600) : tMinFull;
    }
    box._eqFull = [tMinFull, tMaxFull];            // 交互层(平移/缩放)读这两个
    box._eqView = [tMin, tMax];

    // 裁窗 + 换算: 窗前最后一个余额平移到左缘(线从左边进场, 不算交易点)
    let lo = init, hi = init, total = 0;
    const S = [];
    for (const s of all) {
      let prev = null; const pts = [];
      for (const [t, c, p] of s.equity) {
        const v = init + c * lots;
        if (t < tMin) { prev = v; continue; }
        pts.push([t, v, (p || 0) * lots, true]);
      }
      if (prev !== null) pts.unshift([tMin, prev, 0, false]);
      if (pts.length < 2) continue;
      for (const q of pts) { if (q[1] < lo) lo = q[1]; if (q[1] > hi) hi = q[1]; }
      total += pts.length;
      S.push(Object.assign({ pts: pts }, s));
    }
    if (!S.length) { svg.innerHTML = ""; if (endEl) endEl.textContent = "窗口内无交易"; return; }

    const W = 860, H = 260, L = 66, R = 8, T = 10, B = 22;
    const x = (t) => L + (t - tMin) / (tMax - tMin || 1) * (W - L - R);
    const y = (v) => T + (hi - v) / (hi - lo || 1) * (H - T - B);
    const fmt = (v) => Math.round(v).toLocaleString();
    const yr = (ts) => new Date(ts * 1000).getUTCFullYear();
    const day = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);

    // 时间刻度(2026-08-17 重做, 端点不再单独标 — 之前 2026 会和端点标签叠罗汉):
    // 跨度≥3年按年(步长 1/2/4/5/10 自动, ≤8个标签); <3年按月(1/2/3/6月, 放大后才有细刻度)
    let grid = "", labels = "";
    const spanY = (tMax - tMin) / 31557600;
    const ticks = [];
    if (spanY >= 3) {
      const st = [1, 2, 4, 5, 10, 20].find((s) => s >= spanY / 8) || 20;
      for (let Y = Math.ceil(yr(tMin) / st) * st; Y <= yr(tMax); Y += st) {
        ticks.push([Date.UTC(Y, 0, 1) / 1000, String(Y)]);
      }
    } else {
      const d0 = new Date(tMin * 1000);
      const months = Math.max(1, Math.round(spanY * 12));
      const st = [1, 2, 3, 6].find((s) => s >= months / 8) || 6;
      let Y = d0.getUTCFullYear(), M = d0.getUTCMonth() + 1;   // 下一个整月起
      for (let i = 0; i < 40; i++) {
        if (M > 11) { Y += 1; M -= 12; }
        const ts = Date.UTC(Y, M, 1) / 1000;
        if (ts > tMax) break;
        ticks.push([ts, M === 0 ? String(Y) : `${Y}-${String(M + 1).padStart(2, "0")}`]);
        M += st;
      }
    }
    for (const [ts, lbl] of ticks) {
      const gx = x(ts);
      if (gx < L + 16 || gx > W - R - 16) continue;   // 贴边不标, 防溢出/互叠
      grid += `<line x1="${gx.toFixed(1)}" y1="${T}" x2="${gx.toFixed(1)}" y2="${H - B}" stroke="currentColor" opacity="0.08"/>`;
      labels += `<text x="${gx.toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="10" fill="currentColor" opacity="0.55">${lbl}</text>`;
    }
    const rng = hi - lo || 1;
    const mag = Math.pow(10, Math.floor(Math.log10(rng / 4)));
    const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= rng / 4);
    const anchors = [y(hi), y(init), y(lo)];
    for (let v = Math.ceil(lo / step) * step; v < hi; v += step) {
      const gy = y(v);
      grid += `<line x1="${L}" y1="${gy.toFixed(1)}" x2="${W - R}" y2="${gy.toFixed(1)}" stroke="currentColor" opacity="0.08"/>`;
      if (anchors.every((a) => Math.abs(a - gy) > 14))
        labels += `<text x="${L - 6}" y="${(gy + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="currentColor" opacity="0.45">${fmt(v)}</text>`;
    }

    const single = S.length === 1;
    let body = "";
    S.forEach((s, i) => {
      s._col = single ? (s.pts[s.pts.length - 1][1] >= init ? "#16a34a" : "#dc2626")
                      : COLORS[i % COLORS.length];
      body += `<polyline points="${s.pts.map((p) => x(p[0]).toFixed(1) + "," + y(p[1]).toFixed(1)).join(" ")}"` +
              ` fill="none" stroke="${s._col}" stroke-width="1" opacity="${single ? 0.7 : 0.9}"/>`;
    });
    if (total <= 4000) {           // 交易点: 总量可控才画(多策略大样本自动退化为纯线)
      for (const s of S) for (const [t, v, p, real] of s.pts) {
        if (!real) continue;       // 左缘接入点不是交易
        body += `<circle cx="${x(t).toFixed(1)}" cy="${y(v).toFixed(1)}" r="1.8"` +
          ` fill="${p >= 0 ? "#16a34a" : "#dc2626"}" opacity="0.8">` +
          `<title>${single ? "" : "#" + (s.id ?? "") + " · "}${day(t)} · 单笔 ${p >= 0 ? "+" : ""}${p.toFixed(2)} · 余额 ${fmt(v)}</title></circle>`;
      }
    }
    svg.innerHTML = grid +
      `<line x1="${L}" y1="${y(init)}" x2="${W - R}" y2="${y(init)}"` +
      ` stroke="currentColor" stroke-dasharray="4 4" opacity="0.35"/>` +
      body + labels +
      `<text x="${L - 6}" y="${y(hi) + 4}" text-anchor="end" font-size="11" fill="currentColor">${fmt(hi)}</text>` +
      `<text x="${L - 6}" y="${y(init) + 4}" text-anchor="end" font-size="11" fill="currentColor" opacity="0.6">${fmt(init)}</text>` +
      `<text x="${L - 6}" y="${y(lo) + 4}" text-anchor="end" font-size="11" fill="currentColor">${fmt(lo)}</text>`;

    if (endEl) {
      const e0 = S[0].pts[S[0].pts.length - 1][1];
      endEl.textContent = single
        ? `期末 ${fmt(e0)}(${(e0 / init * 100 - 100).toFixed(1)}%) · 峰值 ${fmt(hi)} · 谷值 ${fmt(lo)}`
        : `${S.length} 条曲线 · 同一初始资金/手数下可比`;
    }
    if (legEl) legEl.innerHTML = single ? "" : S.map((s) => {
      const e = s.pts[s.pts.length - 1][1];
      return `<span style="margin-right:14px; white-space:nowrap"><span style="color:${s._col}">●</span>` +
        ` #${s.id ?? ""} ${s.symbol || ""} 期末 <b class="${e >= init ? "pos" : "neg"}">${fmt(e)}</b>` +
        `<span class="muted">(${(e / init * 100 - 100).toFixed(1)}%)</span></span>`;
    }).join("");
  }

  function drawAll() { document.querySelectorAll("[data-eq-chart]").forEach(draw); }
  document.addEventListener("input", (e) => {
    if (e.target.matches("[data-eq-init],[data-eq-lots]")) drawAll();
  });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-eq-zoom]");
    if (!b) return;
    const box = b.closest("[data-eq-chart]");
    if (!box) return;
    box._eqWin = null;                             // 预设档清掉自定义窗口
    box.dataset.zoom = b.dataset.eqZoom;
    box.querySelectorAll("[data-eq-zoom]").forEach((z) => z.classList.toggle("live", z === b));
    draw(box);
  });

  // 拖=平移 · 滚轮=以光标为中心缩放 · 双击=复位 — 数据已全量在内存, 纯前端重画零请求
  const PLOT = 786;                                // 绘图区宽 = 860 - 66(左) - 8(右)
  const vbx = (svg) => 860 / svg.getBoundingClientRect().width;   // CSS像素 → viewBox 换算
  function clearPreset(box) {
    box.querySelectorAll("[data-eq-zoom]").forEach((z) => z.classList.remove("live"));
  }
  let drag = null;
  document.addEventListener("mousedown", (e) => {
    const svg = e.target.closest("[data-eq-svg]");
    if (!svg) return;
    const box = svg.closest("[data-eq-chart]");
    if (!box || !box._eqView) return;
    drag = { box, svg, x0: e.clientX, view: box._eqView.slice() };
    svg.style.cursor = "grabbing";
    e.preventDefault();                            // 防拖动选中文本
  });
  document.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const [t0, t1] = drag.view, [f0, f1] = drag.box._eqFull;
    const w = t1 - t0;
    if (w >= f1 - f0) return;                      // 已是全长, 无处可移
    const dt = -(e.clientX - drag.x0) * vbx(drag.svg) / PLOT * w;
    const n0 = Math.max(f0, Math.min(t0 + dt, f1 - w));   // 两端顶住不出界
    drag.box._eqWin = [n0, n0 + w];
    clearPreset(drag.box);
    draw(drag.box);
  });
  document.addEventListener("mouseup", () => {
    if (drag) drag.svg.style.cursor = "grab";
    drag = null;
  });
  document.addEventListener("dblclick", (e) => {
    const svg = e.target.closest("[data-eq-svg]");
    if (!svg) return;
    const box = svg.closest("[data-eq-chart]");
    box._eqWin = null;
    box.dataset.zoom = "0";
    box.querySelectorAll("[data-eq-zoom]").forEach((z) =>
      z.classList.toggle("live", z.dataset.eqZoom === "0"));
    draw(box);
  });
  document.addEventListener("wheel", (e) => {
    const svg = e.target.closest("[data-eq-svg]");
    if (!svg) return;
    const box = svg.closest("[data-eq-chart]");
    if (!box || !box._eqView) return;
    e.preventDefault();                            // 图上滚轮=缩放, 不滚页面
    const [t0, t1] = box._eqView, [f0, f1] = box._eqFull;
    const rect = svg.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, ((e.clientX - rect.left) * vbx(svg) - 66) / PLOT));
    const ct = t0 + frac * (t1 - t0);              // 光标所指时刻为缩放中心
    const k = e.deltaY > 0 ? 1.25 : 0.8;
    let n0 = ct - (ct - t0) * k, n1 = ct + (t1 - ct) * k;
    if (n1 - n0 < 604800) return;                  // 最小窗 7 天
    n0 = Math.max(f0, n0); n1 = Math.min(f1, n1);
    box._eqWin = (n0 <= f0 && n1 >= f1) ? null : [n0, n1];
    clearPreset(box);
    draw(box);
  }, { passive: false });

  window.drawEquity = drawAll;
  if (document.readyState !== "loading") drawAll();
  else document.addEventListener("DOMContentLoaded", drawAll);
})();
