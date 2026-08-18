/* 资金曲线渲染器(2026-08-17, 唯一源 — 单策略分析页与多策略对比页共用, 改这里两页生效)
   容器 = [data-eq-chart](标记见 _equity_chart.html);
   数据 = 容器内 script[data-eq-data](单策略, 服务端嵌入) 或 容器._eqSeries(多策略, fetch 后赋值);
   series 格式: [{id, name, symbol, equity: [[出场时间, 累计净点, 单笔净点], ...]}, ...]
   换算口径: 钱 = 初始资金 + 累计净点 × 手数(1点=1USD/标准手; 事实只有净点, 钱是显示换算)
   缩放: data-eq-zoom 按钮(全部/10/5/2/1年, 从最新往回), 纯前端裁窗重画, y 自适应可见段 */
(function () {
  const COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#9333ea",
                  "#0891b2", "#db2777", "#65a30d", "#475569", "#b45309"];
  // regime 八格底色(与全站格语义对齐: A/B=长短趋势与波动的两态; 深浅区分波动)
  const CELLS = { AAA: "#15803d", AAB: "#4ade80", ABA: "#d97706", ABB: "#fcd34d",
                  BAA: "#4f46e5", BAB: "#a5b4fc", BBA: "#b91c1c", BBB: "#f87171" };

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
    // raw=全部有效曲线(图例表永远列全, 勾选控制显隐); all=当前显示的(进图)
    const raw = seriesOf(box).filter((s) => (s.equity || []).length > 1);
    box._eqLegendArr = raw;                        // 勾选事件按下标找回同一对象
    const hasMeta = raw.some((s) => s.id != null); // 带身份才有图例表(对比页含单条)
    raw.forEach((s, i) => { s._col = hasMeta ? COLORS[i % COLORS.length] : null; });
    const all = raw.filter((s) => !s._off);
    if (!raw.length) {
      svg.innerHTML = "";
      if (endEl) endEl.textContent = "";
      if (legEl) legEl.textContent = "";
      return;
    }
    // 钳成正数: 负手数=盈亏取反的算术游戏, 不是反向策略的回测(成本不翻/SL·TP互换)
    const init = Math.max(1, parseFloat(box.querySelector("[data-eq-init]")?.value) || 10000);
    const lots = Math.max(0.01, parseFloat(box.querySelector("[data-eq-lots]")?.value) || 0.01);
    // 图例表(从 raw 出, 含隐藏行): 期末/收益=全程口径(不随缩放窗口变, 对比才稳定)
    const fmt0 = (v) => Math.round(v).toLocaleString();
    const cut0 = (t, n) => (t && t.length > n ? t.slice(0, n) + "…" : (t || "—"));
    if (legEl) {
      legEl.innerHTML = !hasMeta ? "" :
        `<div style="margin-top:16px; padding-top:10px; border-top:1px solid var(--border, #ddd); overflow-x:auto">` +
        `<table class="subtable eq-leg" style="width:100%"><tr><th title="勾选=显示该曲线; 取消=隐藏(纵轴按剩余曲线自适应)">显</th><th>ID</th>` +
        `<th>名称</th><th>批次</th><th>品种</th><th>周期</th><th>回测窗</th><th>笔数</th><th>期末(全程)</th><th>收益</th></tr>` +
        raw.map((s, i) => {
          const e = init + (s.equity[s.equity.length - 1][1] || 0) * lots;
          const r = (e / init * 100 - 100);
          const win = (s.from && s.to)
            ? `${String(s.from).slice(0, 10)} ~ ${String(s.to).slice(0, 10)}` : "—";
          return `<tr${s._off ? ' style="opacity:.45"' : ""}>` +
            `<td style="white-space:nowrap"><label style="cursor:pointer">` +
            `<input type="checkbox" data-eq-vis="${i}"${s._off ? "" : " checked"}>` +
            ` <span style="color:${s._col || "#999"}">●</span></label></td>` +
            `<td class="mono">${s.id ?? ""}</td>` +
            `<td class="muted" style="white-space:normal; word-break:break-all">${s.name || "—"}</td>` +
            `<td class="muted" title="${s.basis || ""}">${cut0(s.basis, 26)}</td>` +
            `<td>${s.symbol || "—"}</td><td>${s.timeframe || "—"}</td>` +
            `<td class="mono">${win}</td><td>${s.equity.length}</td>` +
            `<td class="${e >= init ? "pos" : "neg"}">${fmt0(e)}</td>` +
            `<td class="${r >= 0 ? "pos" : "neg"}">${r >= 0 ? "+" : ""}${r.toFixed(1)}%</td></tr>`;
        }).join("") + "</table></div>";
      if (hasMeta) legendResizers(box, legEl);
    }
    if (!all.length) {                             // 全部被勾掉: 图清空但表还在, 随时勾回
      svg.innerHTML = "";
      if (endEl) endEl.textContent = "全部曲线已隐藏 — 勾选图例表恢复";
      return;
    }
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

    // 画布宽 = 容器实际像素宽(1 viewBox 单位 = 1 CSS 像素, 拉满整行且文字不变形);
    // 窗口改变尺寸时由 resize 监听重画
    const W = Math.max(700, Math.round(svg.clientWidth || svg.getBoundingClientRect().width || 860));
    const H = 260, L = 66, R = 8, T = 10, B = 22;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    box._eqG = { W, L, R, PLOT: W - L - R };       // 交互层(框选/平移/滚轮/悬停)读这个
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
      labels += `<text x="${gx.toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="12" font-weight="700" fill="currentColor" opacity="0.75">${lbl}</text>`;
    }
    const rng = hi - lo || 1;
    const mag = Math.pow(10, Math.floor(Math.log10(rng / 4)));
    const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= rng / 4);
    const anchors = [y(hi), y(init), y(lo)];
    for (let v = Math.ceil(lo / step) * step; v < hi; v += step) {
      const gy = y(v);
      grid += `<line x1="${L}" y1="${gy.toFixed(1)}" x2="${W - R}" y2="${gy.toFixed(1)}" stroke="currentColor" opacity="0.08"/>`;
      if (anchors.every((a) => Math.abs(a - gy) > 14))
        labels += `<text x="${L - 6}" y="${(gy + 4).toFixed(1)}" text-anchor="end" font-size="12" font-weight="700" fill="currentColor" opacity="0.65">${fmt(v)}</text>`;
    }

    // regime 底色(开了下拉才有): 时间线连续段铺满绘图区高度, 低透明度垫底
    let band = "";
    if (box._eqBand) {
      for (const [s0, s1, cell] of box._eqBand) {
        if (s1 <= tMin || s0 >= tMax) continue;
        const xa = x(Math.max(s0, tMin)), xb = x(Math.min(s1, tMax));
        band += `<rect x="${xa.toFixed(1)}" y="${T}" width="${(xb - xa).toFixed(1)}"` +
                ` height="${H - T - B}" fill="${CELLS[cell] || "#999"}" opacity="0.10"/>`;
      }
    }
    const single = S.length === 1;
    const light = !!box._eqLight;                  // 交互中轻量模式: 只画线(平滑优先)
    let body = "";
    S.forEach((s, i) => {
      if (!s._col)                                 // 无身份(分析页内嵌)才按涨跌上色
        s._col = s.pts[s.pts.length - 1][1] >= init ? "#16a34a" : "#dc2626";
      // 台阶线(数字准确的画法): 两笔之间余额是常数 → 横到下一笔时刻再竖跳,
      // 斜线插值是错的(余额不会在两笔间线性漂移)
      let ln = "", py = "";
      for (const p of s.pts) {
        const X = x(p[0]).toFixed(1), Y = y(p[1]).toFixed(1);
        ln += ln ? ` ${X},${py} ${X},${Y}` : `${X},${Y}`;
        py = Y;
      }
      if (single && !light) {                      // 单曲线: 淡面积填充(厚度感)
        const base = (H - B).toFixed(1);
        body += `<polygon points="${ln} ${x(s.pts[s.pts.length - 1][0]).toFixed(1)},${base}` +
                ` ${x(s.pts[0][0]).toFixed(1)},${base}" fill="${s._col}" opacity="0.07"/>`;
      }
      body += `<polyline points="${ln}" fill="none" stroke="${s._col}"` +
              ` stroke-width="1.2" opacity="${single ? 0.8 : 0.9}"/>`;
    });
    box._eqS = S;                                  // 悬停读数用(十字线/气泡)
    if (!light && total <= 600) {  // 交易点: 放大到可读密度才浮现(全景=干净的线)
      for (const s of S) for (const [t, v, p, real] of s.pts) {
        if (!real) continue;       // 左缘接入点不是交易
        body += `<circle cx="${x(t).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2"` +
          ` fill="${p >= 0 ? "#16a34a" : "#dc2626"}" opacity="0.85">` +
          `<title>${single ? "" : "#" + (s.id ?? "") + " · "}${day(t)} · 单笔 ${p >= 0 ? "+" : ""}${p.toFixed(2)} · 余额 ${fmt(v)}</title></circle>`;
      }
    }
    svg.innerHTML = band + grid +
      `<line x1="${L}" y1="${y(init)}" x2="${W - R}" y2="${y(init)}"` +
      ` stroke="currentColor" stroke-dasharray="4 4" opacity="0.35"/>` +
      `<line x1="${L}" y1="${y(hi).toFixed(1)}" x2="${W - R}" y2="${y(hi).toFixed(1)}"` +
      ` stroke="#16a34a" stroke-dasharray="4 4" opacity="0.4"/>` +
      `<line x1="${L}" y1="${y(lo).toFixed(1)}" x2="${W - R}" y2="${y(lo).toFixed(1)}"` +
      ` stroke="#dc2626" stroke-dasharray="4 4" opacity="0.4"/>` +
      body + labels +
      `<text x="${L - 6}" y="${y(hi) + 4}" text-anchor="end" font-size="13" font-weight="700" fill="currentColor">${fmt(hi)}</text>` +
      `<text x="${L - 6}" y="${y(init) + 4}" text-anchor="end" font-size="13" font-weight="700" fill="currentColor" opacity="0.6">${fmt(init)}</text>` +
      `<text x="${L - 6}" y="${y(lo) + 4}" text-anchor="end" font-size="13" font-weight="700" fill="currentColor">${fmt(lo)}</text>`;

    if (endEl) {
      const e0 = S[0].pts[S[0].pts.length - 1][1];
      endEl.textContent = single
        ? `期末 ${fmt(e0)}(${(e0 / init * 100 - 100).toFixed(1)}%) · 峰值 ${fmt(hi)} · 谷值 ${fmt(lo)}`
        : `${S.length} 条曲线 · 同一初始资金/手数下可比`;
    }

  }

  // 尺寸监听: 元素隐藏→显示(如分析页切到回测页签)或任何尺寸变化都重画 —
  // 首绘时若在 hidden 页签里量到宽=0 只能按兜底画, 露面那一刻靠它纠正
  let roT;
  const ro = ("ResizeObserver" in window)
    ? new ResizeObserver(() => { clearTimeout(roT); roT = setTimeout(drawAll, 100); })
    : null;
  function drawAll() {
    document.querySelectorAll("[data-eq-chart]").forEach((b) => {
      const svg = b.querySelector("[data-eq-svg]");
      if (ro && svg && !svg._eqRO) { svg._eqRO = true; ro.observe(svg); }
      draw(b);
    });
    fillRegimeSelects();
    fillLotSelects();
  }

  // 图例表列宽手拉: 表头右缘 7px 拖柄; 首拖时把各列当前宽钉住(fixed布局)防跳动;
  // 宽度记在容器上, 重画后原样恢复
  function legendResizers(box, legEl) {
    const table = legEl.querySelector("table");
    if (!table) return;
    const ths = [...table.querySelectorAll("th")];
    if (box._eqColW && box._eqColW.length === ths.length) {
      table.style.tableLayout = "fixed";
      ths.forEach((th, i) => { if (box._eqColW[i]) th.style.width = box._eqColW[i] + "px"; });
    }
    ths.forEach((th, i) => {
      th.style.position = "relative";
      const h = document.createElement("span");
      h.title = "拖动调列宽";
      h.style.cssText = "position:absolute;right:-3px;top:0;bottom:0;width:7px;" +
                        "cursor:col-resize;user-select:none;z-index:2";
      th.appendChild(h);
      h.addEventListener("mousedown", (e) => {
        e.preventDefault(); e.stopPropagation();
        const x0 = e.clientX, w0 = th.getBoundingClientRect().width;
        if (!box._eqColW)
          box._eqColW = ths.map((t) => Math.round(t.getBoundingClientRect().width));
        table.style.tableLayout = "fixed";
        ths.forEach((t, j) => (t.style.width = box._eqColW[j] + "px"));
        const move = (ev) => {
          const w = Math.max(28, Math.round(w0 + ev.clientX - x0));
          th.style.width = w + "px";
          box._eqColW[i] = w;
        };
        const up = () => {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
        };
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    });
  }
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

  // 框选=放大 · Shift+拖=平移 · 滚轮=光标为中心缩放 · 双击=复位 — 全量在内存, 纯前端零请求
  const geo = (box) => box._eqG || { W: 860, L: 66, R: 8, PLOT: 786 };   // 画布几何(随容器宽动态)
  const vbx = (box, svg) => geo(box).W / svg.getBoundingClientRect().width;  // CSS像素→viewBox
  function clearPreset(box) {
    box.querySelectorAll("[data-eq-zoom]").forEach((z) => z.classList.remove("live"));
  }
  // 拖=框选放大(Frank 原始需求"选一段只放大这些"), Shift+拖=平移
  let drag = null;
  document.addEventListener("mousedown", (e) => {
    const svg = e.target.closest("[data-eq-svg]");
    if (!svg) return;
    const box = svg.closest("[data-eq-chart]");
    if (!box || !box._eqView) return;
    drag = { box, svg, x0: e.clientX, view: box._eqView.slice(),
             pan: e.shiftKey, sel: null };
    if (drag.pan) svg.style.cursor = "grabbing";
    e.preventDefault();                            // 防拖动选中文本
  });
  document.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const G = geo(drag.box);
    if (drag.pan) {                                // Shift+拖: 平移(轻量+rAF, 跟手平滑)
      const [t0, t1] = drag.view, [f0, f1] = drag.box._eqFull;
      const w = t1 - t0;
      if (w >= f1 - f0) return;                    // 已是全长, 无处可移
      const dt = -(e.clientX - drag.x0) * vbx(drag.box, drag.svg) / G.PLOT * w;
      const n0 = Math.max(f0, Math.min(t0 + dt, f1 - w));   // 两端顶住不出界
      drag.box._eqWin = [n0, n0 + w];
      clearPreset(drag.box);
      drag.box._eqLight = true;
      scheduleDraw(drag.box);
      return;
    }
    // 框选: 半透明选框实时跟手(松手才重画, 选框本身零开销)
    const rect = drag.svg.getBoundingClientRect();
    const k = vbx(drag.box, drag.svg);
    const xa = Math.max(G.L, Math.min(G.W - G.R, (Math.min(drag.x0, e.clientX) - rect.left) * k));
    const xb = Math.max(G.L, Math.min(G.W - G.R, (Math.max(drag.x0, e.clientX) - rect.left) * k));
    if (!drag.sel) {
      drag.sel = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      drag.sel.setAttribute("y", 10);
      drag.sel.setAttribute("height", 228);
      drag.sel.setAttribute("fill", "#2563eb");
      drag.sel.setAttribute("opacity", "0.15");
      drag.svg.appendChild(drag.sel);
    }
    drag.sel.setAttribute("x", xa.toFixed(1));
    drag.sel.setAttribute("width", Math.max(0, xb - xa).toFixed(1));
  });
  document.addEventListener("mouseup", (e) => {
    if (!drag) return;
    const d = drag;
    drag = null;
    d.svg.style.cursor = "crosshair";
    if (d.pan) { d.box._eqLight = false; draw(d.box); return; }   // 松手恢复全量(点/面积)
    if (d.sel) d.sel.remove();
    const G = geo(d.box);
    const rect = d.svg.getBoundingClientRect();
    const k = vbx(d.box, d.svg);
    const pxa = (Math.min(d.x0, e.clientX) - rect.left) * k;
    const pxb = (Math.max(d.x0, e.clientX) - rect.left) * k;
    if (pxb - pxa < 8) return;                     // 点一下不算选
    const [t0, t1] = d.view;
    const tt = (px) => t0 + Math.max(0, Math.min(1, (px - G.L) / G.PLOT)) * (t1 - t0);
    const n0 = tt(pxa), n1 = tt(pxb);
    if (n1 - n0 < 604800) return;                  // 最小窗 7 天
    d.box._eqWin = [n0, n1];
    clearPreset(d.box);
    draw(d.box);
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
    const G = geo(box);
    const [t0, t1] = box._eqView, [f0, f1] = box._eqFull;
    const rect = svg.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, ((e.clientX - rect.left) * vbx(box, svg) - G.L) / G.PLOT));
    const ct = t0 + frac * (t1 - t0);              // 光标所指时刻为缩放中心
    const k = e.deltaY > 0 ? 1.25 : 0.8;
    let n0 = ct - (ct - t0) * k, n1 = ct + (t1 - ct) * k;
    if (n1 - n0 < 604800) return;                  // 最小窗 7 天
    n0 = Math.max(f0, n0); n1 = Math.min(f1, n1);
    box._eqWin = (n0 <= f0 && n1 >= f1) ? null : [n0, n1];
    clearPreset(box);
    box._eqLight = true;                           // 连续滚轮走轻量, 停 160ms 恢复全量
    scheduleDraw(box);
    clearTimeout(box._eqWT);
    box._eqWT = setTimeout(() => { box._eqLight = false; draw(box); }, 160);
  }, { passive: false });

  function scheduleDraw(box) {                     // rAF 合帧: 一帧最多画一次
    if (box._eqRaf) return;
    box._eqRaf = requestAnimationFrame(() => { box._eqRaf = null; draw(box); });
  }

  // 悬停十字线 + 数值气泡: 光标所在时刻, 每条曲线的当时余额(逐序列二分定位)
  document.addEventListener("mousemove", (e) => {
    const svg = e.target.closest && e.target.closest("[data-eq-svg]");
    if (!svg || drag) {
      document.querySelectorAll("[data-eq-tip]:not([hidden])").forEach((t) => (t.hidden = true));
      document.querySelectorAll("[data-eq-hair]").forEach((l) => l.remove());
      return;
    }
    const box = svg.closest("[data-eq-chart]");
    const tip = box && box.querySelector("[data-eq-tip]");
    if (!box || !tip || !box._eqS || !box._eqView) return;
    const G = geo(box);
    const rect = svg.getBoundingClientRect();
    const vx = (e.clientX - rect.left) * vbx(box, svg);
    if (vx < G.L || vx > G.W - G.R) { tip.hidden = true; return; }
    const [v0, v1] = box._eqView;
    const t = v0 + Math.max(0, Math.min(1, (vx - G.L) / G.PLOT)) * (v1 - v0);
    let hair = svg.querySelector("[data-eq-hair]");
    if (!hair) {
      hair = document.createElementNS("http://www.w3.org/2000/svg", "line");
      hair.setAttribute("data-eq-hair", "");
      hair.setAttribute("y1", "10"); hair.setAttribute("y2", "238");
      hair.setAttribute("stroke", "currentColor");
      hair.setAttribute("stroke-dasharray", "3 3");
      hair.setAttribute("opacity", "0.35");
      svg.appendChild(hair);
    }
    hair.setAttribute("x1", vx.toFixed(1)); hair.setAttribute("x2", vx.toFixed(1));
    const fmt = (v) => Math.round(v).toLocaleString();
    const rows = [];
    for (const s of box._eqS) {                    // 二分: 该时刻前最近一笔的余额
      let a = 0, b = s.pts.length - 1, hit = null;
      if (s.pts[0][0] <= t) {
        while (a <= b) {
          const m = (a + b) >> 1;
          if (s.pts[m][0] <= t) { hit = s.pts[m]; a = m + 1; } else b = m - 1;
        }
      }
      if (hit) rows.push([s, hit]);
    }
    if (!rows.length) { tip.hidden = true; return; }
    rows.sort((p, q) => q[1][1] - p[1][1]);        // 多曲线按余额降序, 和图上高低对应
    const single = box._eqS.length === 1;
    let cellLine = "";                             // 底色开着时: 气泡带上当日格
    if (box._eqBand) {
      const seg = box._eqBand.find((g) => g[0] <= t && t < g[1]);
      if (seg) cellLine = ` <span style="color:${CELLS[seg[2]] || "#999"}">■</span> ${seg[2]}`;
    }
    tip.innerHTML = `<b>${new Date(t * 1000).toISOString().slice(0, 10)}</b>${cellLine}<br>` +
      rows.map(([s, h]) =>
        `<span style="color:${s._col}">●</span> ${single ? "" : "#" + (s.id ?? "") + " "}` +
        `${fmt(h[1])}${single && h[3] ? `<span class="muted"> · 该笔 ${h[2] >= 0 ? "+" : ""}${h[2].toFixed(2)}</span>` : ""}`
      ).join("<br>");
    const cx = e.clientX - rect.left;
    if (cx > rect.width * 0.6) { tip.style.left = ""; tip.style.right = (rect.width - cx + 14) + "px"; }
    else { tip.style.right = ""; tip.style.left = (cx + 14) + "px"; }
    tip.style.top = "8px";
    tip.hidden = false;
  });

  // 手数下拉: 懒取配置页的手数预设填充(与策略页同一套, 唯一源=config 表)
  let lotCfg = null;
  async function fillLotSelects() {
    const sels = [...document.querySelectorAll("select[data-eq-lots]")].filter((s) => !s._eqFilled);
    if (!sels.length) return;
    if (lotCfg === null) {
      try { lotCfg = await fetch("/strategies/volume_presets.json").then((r) => r.json()); }
      catch (e) { lotCfg = false; }
    }
    if (!lotCfg || !lotCfg.presets || !lotCfg.presets.length) return;
    const dflt = lotCfg.default || lotCfg.presets[0];
    const vals = lotCfg.presets.includes(dflt) ? lotCfg.presets : [dflt, ...lotCfg.presets];
    for (const sel of sels) {
      sel._eqFilled = true;
      sel.innerHTML = vals.map((v) =>
        `<option value="${v}"${v === dflt ? " selected" : ""}>${v}</option>`).join("");
    }
  }
  document.addEventListener("change", (e) => {
    if (e.target.matches("select[data-eq-lots]")) drawAll();   // 换档即时重画
  });
  document.addEventListener("change", (e) => {     // 图例勾选: 显/隐单条曲线
    if (!e.target.matches("[data-eq-vis]")) return;
    const box = e.target.closest("[data-eq-chart]");
    const arr = box && box._eqLegendArr;
    if (!arr) return;
    const s = arr[+e.target.dataset.eqVis];
    if (s) s._off = !e.target.checked;
    draw(box);
  });

  // regime 版本下拉: 首次绘制时懒取版本清单填充; 切换时取该版本时间线连续段铺底色
  let vers = null;
  async function fillRegimeSelects() {
    const sels = [...document.querySelectorAll("[data-eq-regime]")].filter((s) => !s._eqFilled);
    if (!sels.length) return;
    if (vers === null) {
      try { vers = await fetch("/strategies/regime_versions.json").then((r) => r.json()); }
      catch (e) { vers = false; }
    }
    if (!vers || !vers.versions) return;
    for (const sel of sels) {
      sel._eqFilled = true;
      for (const v of vers.versions) {
        const o = document.createElement("option");
        o.value = v.id;
        o.textContent = `v${v.id}${v.id === vers.current ? "·默认" : ""} ${v.label || ""}`;
        sel.appendChild(o);
      }
    }
  }
  document.addEventListener("change", async (e) => {
    if (!e.target.matches("[data-eq-regime]")) return;
    const box = e.target.closest("[data-eq-chart]");
    if (!box) return;
    const endEl = box.querySelector("[data-eq-end]");
    const say = (t) => { if (endEl) endEl.textContent = t; };   // 取不到就说话, 不沉默
    if (!e.target.value) { box._eqBand = null; draw(box); return; }
    const sym = box.dataset.eqSymbol || "";
    let msg = "";
    if (!sym) {
      box._eqBand = null;
      msg = "先载入曲线再开 regime 底色(品种未知)";
    } else {
      try {
        const d = await fetch("/strategies/regime_band.json?symbol=" + encodeURIComponent(sym)
          + "&version=" + encodeURIComponent(e.target.value),
          { cache: "no-store" }).then((r) => r.json());
        if (d.error) { box._eqBand = null; msg = "regime 底色取失败: " + d.error; }
        else if (!d.segments || !d.segments.length) {
          box._eqBand = null;
          msg = `v${e.target.value} 没有 ${sym} 的时间线 — 去「全局货币regime」页该版本下重建`;
        } else {
          box._eqBand = d.segments;
          msg = `regime v${e.target.value} · ${d.segments.length} 段已铺`;
        }
      } catch (err) { box._eqBand = null; msg = "regime 底色取失败: " + err; }
    }
    draw(box);                       // 先画后说 — draw 会重写读数行, 消息必须最后落笔
    say(msg);
  });

  let rsT;                                         // 窗口改宽 → 防抖重画(画布宽随容器)
  window.addEventListener("resize", () => { clearTimeout(rsT); rsT = setTimeout(drawAll, 150); });

  window.drawEquity = drawAll;
  if (document.readyState !== "loading") drawAll();
  else document.addEventListener("DOMContentLoaded", drawAll);
})();
