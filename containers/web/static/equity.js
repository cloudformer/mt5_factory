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
    const zoomY = parseFloat(box.dataset.zoom || "0");
    const tMax = Math.max(...all.map((s) => s.equity[s.equity.length - 1][0]));
    const tMinFull = Math.min(...all.map((s) => s.equity[0][0]));
    const tMin = zoomY > 0 ? Math.max(tMinFull, tMax - zoomY * 31557600) : tMinFull;

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

    // 网格: 横轴逐年(>24年隔4, >12隔2), 纵轴 1/2/5×10^n 自动步长
    const Y0 = yr(tMin), Y1 = yr(tMax), span = Y1 - Y0;
    const ystep = span > 24 ? 4 : span > 12 ? 2 : 1;
    let grid = "", labels = "";
    for (let Y = Y0 + 1; Y <= Y1; Y++) {
      if (Y % ystep) continue;
      const ts = Date.UTC(Y, 0, 1) / 1000;
      if (ts <= tMin || ts >= tMax - (tMax - tMin) * 0.02) continue;
      const gx = x(ts).toFixed(1);
      grid += `<line x1="${gx}" y1="${T}" x2="${gx}" y2="${H - B}" stroke="currentColor" opacity="0.08"/>`;
      labels += `<text x="${gx}" y="${H - 6}" text-anchor="middle" font-size="10" fill="currentColor" opacity="0.55">${Y}</text>`;
    }
    const rng = hi - lo || 1;
    const mag = Math.pow(10, Math.floor(Math.log10(rng / 4)));
    const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= rng / 4);
    const anchors = [y(hi), y(init), y(lo)];
    for (let v = Math.ceil(lo / step) * step; v < hi; v += step) {
      const gy = y(v);
      grid += `<line x1="${L}" y1="${gy.toFixed(1)}" x2="${W - R}" y2="${gy.toFixed(1)}" stroke="currentColor" opacity="0.08"/>`;
      if (anchors.every((a) => Math.abs(a - gy) > 12))
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
      `<text x="${L - 6}" y="${y(lo) + 4}" text-anchor="end" font-size="11" fill="currentColor">${fmt(lo)}</text>` +
      `<text x="${L}" y="${H - 6}" font-size="11" fill="currentColor" opacity="0.6">${yr(tMin)}</text>` +
      `<text x="${W - R}" y="${H - 6}" text-anchor="end" font-size="11" fill="currentColor" opacity="0.6">${yr(tMax)}</text>`;

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
    box.dataset.zoom = b.dataset.eqZoom;
    box.querySelectorAll("[data-eq-zoom]").forEach((z) => z.classList.toggle("live", z === b));
    draw(box);
  });
  window.drawEquity = drawAll;
  if (document.readyState !== "loading") drawAll();
  else document.addEventListener("DOMContentLoaded", drawAll);
})();
