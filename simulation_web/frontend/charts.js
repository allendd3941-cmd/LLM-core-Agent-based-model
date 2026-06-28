/* ================================================================
   charts.js — Chart.js 即時圖表
   壅塞趨勢（折線）、抵達進度（折線）、行為模式分佈（長條）。
   ================================================================ */
const TrafficCharts = (() => {
  let congestionChart = null;
  let arrivedChart = null;
  let modeChart = null;
  let modeTotals = {};        // 行為模式「累積」次數（跨所有 step 累加）
  let lastModeCycle = -1;     // 避免同一 cycle 重複累加

  const GRID = "rgba(255,255,255,0.06)";
  const TICK = "#93a1b3";
  Chart.defaults.color = TICK;
  Chart.defaults.font.size = 11;

  const lineOpts = (yMax) => ({
    responsive: true,
    animation: false,
    plugins: { legend: { display: true, labels: { boxWidth: 12 } } },
    scales: {
      x: { grid: { color: GRID }, ticks: { maxTicksLimit: 8 } },
      y: { grid: { color: GRID }, beginAtZero: true, ...(yMax ? { max: yMax } : {}) },
    },
  });

  function init() {
    congestionChart = new Chart(document.getElementById("chart-congestion"), {
      type: "line",
      data: { labels: [], datasets: [
        { label: T("平均壅塞"), data: [], borderColor: "#FF8A3D", backgroundColor: "rgba(255,138,61,.15)", fill: true, tension: .3, pointRadius: 0 },
        { label: T("壅塞路段數"), data: [], borderColor: "#FF4D4D", tension: .3, pointRadius: 0, yAxisID: "y1" },
      ] },
      options: { ...lineOpts(1), scales: { ...lineOpts(1).scales, y1: { position: "right", grid: { display: false }, beginAtZero: true } } },
    });

    arrivedChart = new Chart(document.getElementById("chart-arrived"), {
      type: "line",
      data: { labels: [], datasets: [
        { label: T("已抵達"), data: [], borderColor: "#2FD17A", backgroundColor: "rgba(47,209,122,.15)", fill: true, tension: .3, pointRadius: 0 },
        { label: T("移動中"), data: [], borderColor: "#3FB6FF", tension: .3, pointRadius: 0 },
      ] },
      options: lineOpts(),
    });

    modeChart = new Chart(document.getElementById("chart-mode"), {
      type: "bar",
      data: { labels: [], datasets: [{ label: T("累積選擇次數"), data: [], backgroundColor: "#3FB6FF" }] },
      options: { responsive: true, animation: false, plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false } }, y: { grid: { color: GRID }, beginAtZero: true, ticks: { stepSize: 1 } } } },
    });

    const dv = document.getElementById("detector-view");
    if (dv) dv.onchange = drawDetectorTable;   // 監測器流量類型下拉 → 重畫表
    addChartDownloads();                        // 每張圖加「下載 PNG」鈕
  }

  function update(state) {
    const hist = (state.metrics && state.metrics.history) || [];
    const labels = hist.map((h) => h.cycle);
    congestionChart.data.labels = labels;
    congestionChart.data.datasets[0].data = hist.map((h) => h.average_congestion_proxy);
    congestionChart.data.datasets[1].data = hist.map((h) => h.crowded_road_count);
    congestionChart.update();

    arrivedChart.data.labels = labels;
    arrivedChart.data.datasets[0].data = hist.map((h) => h.arrived || 0);
    arrivedChart.data.datasets[1].data = hist.map((h) => h.moving || 0);
    arrivedChart.update();

    // 行為模式：累積柱狀圖（把每步的當前分佈累加，而非只看當步）
    const md = state.mode_distribution || {};
    if (state.cycle !== lastModeCycle) {
      Object.entries(md).forEach(([mode, n]) => {
        modeTotals[mode] = (modeTotals[mode] || 0) + n;
      });
      lastModeCycle = state.cycle;
    }
    modeChart.data.labels = Object.keys(modeTotals);
    modeChart.data.datasets[0].data = Object.values(modeTotals);
    modeChart.update();
  }

  function reset() {
    modeTotals = {};
    lastModeCycle = -1;
    [congestionChart, arrivedChart, modeChart].forEach((c) => {
      if (!c) return;
      c.data.labels = [];
      c.data.datasets.forEach((d) => (d.data = []));
      c.update();
    });
    // 隱藏並清掉分析圖（reset 時舊分析不該殘留）
    const card = document.getElementById("analysis-card");
    if (card) card.style.display = "none";
    [arrivalChart, travelChart, odChart, volumeChart, egressChart, egressTravelChart, egressOdChart, detectorsChart]
      .forEach((c) => { if (c) c.destroy(); });
    arrivalChart = travelChart = odChart = volumeChart = null;
    egressChart = egressTravelChart = egressOdChart = detectorsChart = null;
    _lastAnalysis = null;
    _lastDetectors = [];
    const db = document.getElementById("detector-analysis");
    if (db) db.innerHTML = "";
  }

  // ===== 模擬後交通分析（收到 type:analysis 時呼叫）=====
  let arrivalChart = null, travelChart = null, odChart = null, volumeChart = null;
  let egressChart = null, egressTravelChart = null, egressOdChart = null;
  let detectorsChart = null;
  let _lastAnalysis = null;     // 最近一次分析資料（CSV 匯出用）
  let _lastDetectors = [];      // 最近一次監測器資料（下拉切換重畫用）

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function histogram(values, nbins) {
    if (!values.length) return { labels: [], counts: [] };
    const max = Math.max(...values), min = Math.min(...values);
    const span = Math.max(1, max - min);
    const w = Math.ceil(span / nbins) || 1;
    const counts = new Array(nbins).fill(0);
    values.forEach((v) => {
      const i = Math.min(nbins - 1, Math.floor((v - min) / w));
      counts[i]++;
    });
    const labels = counts.map((_, i) => `${min + i * w}-${min + (i + 1) * w}`);
    return { labels, counts };
  }

  function renderAnalysis(data) {
    const card = document.getElementById("analysis-card");
    if (card) card.style.display = "";
    const s = data.summary || {};
    const sum = document.getElementById("analysis-summary");
    if (sum) sum.innerHTML =
      `<div class="row"><span>${T("抵達率")}</span><b>${s.arrived}/${s.total_agents}（${s.arrival_pct}%）</b></div>`
      + `<div class="row"><span>${T("平均旅行時間")}</span><b>${s.avg_travel_min} min</b></div>`
      + `<div class="row"><span>${T("號誌停等總次數")}</span><b>${s.total_signal_stops}</b></div>`;

    const labels = data.cycles || [];
    arrivalChart && arrivalChart.destroy();
    arrivalChart = new Chart(document.getElementById("chart-arrival"), {
      type: "line",
      data: { labels, datasets: [
        { label: T("累積抵達"), data: data.cumulative_arrived || [], borderColor: "#2FD17A", backgroundColor: "rgba(47,209,122,.15)", fill: true, tension: .3, pointRadius: 0 },
        { label: T("每步抵達率"), data: data.arrival_rate || [], borderColor: "#FFB020", tension: .3, pointRadius: 0, yAxisID: "y1" },
        { label: T("每步出發"), data: data.departures || [], borderColor: "#B388FF", borderDash: [4, 3], tension: .3, pointRadius: 0, yAxisID: "y1" },
      ] },
      options: { ...lineOpts(), scales: { ...lineOpts().scales, y1: { position: "right", grid: { display: false }, beginAtZero: true } } },
    });

    const h = histogram(data.travel_time_minutes || [], 12);
    travelChart && travelChart.destroy();
    travelChart = new Chart(document.getElementById("chart-traveltime"), {
      type: "bar",
      data: { labels: h.labels, datasets: [{ label: T("agent 數"), data: h.counts, backgroundColor: "#3FB6FF" }] },
      options: { responsive: true, animation: false, plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false } }, y: { grid: { color: GRID }, beginAtZero: true } } },
    });

    const od = data.od_actual || [];
    const expShare = Object.fromEntries((data.od_expected_share || []).map((r) => [r[0], r[1]]));
    const total = (data.summary && data.summary.total_agents) || 1;
    odChart && odChart.destroy();
    odChart = new Chart(document.getElementById("chart-od"), {
      type: "bar",
      data: { labels: od.map((r) => r[0]), datasets: [
        { label: T("實際"), data: od.map((r) => r[1]), backgroundColor: "#3FB6FF" },
        { label: T("重力期望"), data: od.map((r) => Math.round((expShare[r[0]] || 0) * total)), backgroundColor: "rgba(255,176,32,.7)" },
      ] },
      options: { responsive: true, animation: false, plugins: { legend: { labels: { boxWidth: 12 } } },
        scales: { x: { grid: { display: false }, ticks: { maxRotation: 60, minRotation: 60 } }, y: { grid: { color: GRID }, beginAtZero: true } } },
    });

    renderNetwork(data.network || {}, labels);
    renderEgress(data.egress || {}, labels);
    _lastAnalysis = data;
    renderDetectors(data.detectors || []);
    addChartDownloads();   // 分析圖此時才有內容 → 補綁下載鈕
  }

  // ===== 車流監測器（放在路上的計數器）=====
  function renderDetectors(dets) {
    _lastDetectors = dets || [];
    drawDetectorTable();
    detectorsChart && detectorsChart.destroy();
    detectorsChart = null;
    const canvas = document.getElementById("chart-detectors");
    if (!canvas || !_lastDetectors.length) return;
    const maxLen = Math.max(0, ..._lastDetectors.map((d) => (d.series || []).length));
    const labels = Array.from({ length: maxLen }, (_, i) => i + 1);
    const palette = ["#3FB6FF", "#FF8A3D", "#2FD17A", "#B388FF", "#FF4D4D", "#FFD600"];
    detectorsChart = new Chart(canvas, {
      type: "line",
      data: { labels, datasets: _lastDetectors.map((d, i) => ({
        label: `${d.id} ${d.label}`, data: d.series || [],
        borderColor: palette[i % palette.length], tension: .3, pointRadius: 0 })) },
      options: lineOpts(),
    });
  }

  function drawDetectorTable() {
    const box = document.getElementById("detector-analysis");
    if (!box) return;
    const viewEl = document.getElementById("detector-view");
    const view = (viewEl && viewEl.value) || "total";
    if (!_lastDetectors.length) {
      box.innerHTML = `<p class="muted">${T("尚未放置監測器（在左側「車流監測器」放置後按「套用設定」，再跑模擬）。")}</p>`;
      return;
    }
    const VL = { total: T("總車流量"), car: T("汽車"), moto: T("機車"), event: T("事件車"), ambient: T("背景車") };
    box.innerHTML = `<table><thead><tr><th>${T("監測器")}</th><th>${T("路段")}</th><th>${VL[view]}</th><th>${T("上行/下行")}</th><th>${T("汽/機·事/背")}</th></tr></thead><tbody>`
      + _lastDetectors.map((d) => {
          const b = d.both || {}, a = d.dir_a || {}, bb = d.dir_b || {};
          return `<tr><td>${esc(d.id)}</td><td>${esc(d.label)}</td>`
            + `<td><b>${b[view] || 0}</b></td>`
            + `<td>${a[view] || 0} / ${bb[view] || 0}</td>`
            + `<td>${b.car || 0}/${b.moto || 0} · ${b.event || 0}/${b.ambient || 0}</td></tr>`;
        }).join("")
      + "</tbody></table>";
  }

  // 每步通過數曲線 + 表格的數據另存 CSV（含時間序列 + 監測器）
  function downloadAnalysisCSV() {
    const d = _lastAnalysis;
    if (!d) { return; }
    const lines = ["# 時間序列",
      "cycle,elapsed_min,cumulative_arrived,arrival_rate,avg_congestion,crowded_roads,volume_event,volume_ambient"];
    const cyc = d.cycles || [], net = d.network || {};
    const at = (arr, i) => { const v = (arr || [])[i]; return v == null ? "" : v; };
    for (let i = 0; i < cyc.length; i++) {
      lines.push([cyc[i], at(d.elapsed_minutes, i), at(d.cumulative_arrived, i), at(d.arrival_rate, i),
        at(d.avg_congestion, i), at(d.crowded_road_count, i),
        at(net.volume_event, i), at(net.volume_ambient, i)].join(","));
    }
    const dets = d.detectors || [];
    if (dets.length) {
      lines.push("", "# 監測器（通過次數）", "id,label,total,car,moto,event,ambient,dir_a_total,dir_b_total");
      dets.forEach((x) => {
        const b = x.both || {}, a = x.dir_a || {}, bb = x.dir_b || {};
        lines.push([x.id, '"' + String(x.label).replace(/"/g, '""') + '"',
          b.total || 0, b.car || 0, b.moto || 0, b.event || 0, b.ambient || 0,
          a.total || 0, bb.total || 0].join(","));
      });
    }
    const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "analysis.csv";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }

  // 為每張圖卡加「下載 PNG」鈕（直接抓 canvas 像素，不需 Chart 實例）
  function addChartDownloads() {
    document.querySelectorAll(".chart-card").forEach((card) => {
      if (card.querySelector(".chart-dl")) return;
      const canvas = card.querySelector("canvas");
      if (!canvas) return;
      const btn = document.createElement("button");
      btn.className = "chart-dl"; btn.type = "button"; btn.title = T("下載此圖 PNG");
      btn.innerHTML = '<i class="ti ti-download" aria-hidden="true"></i>';
      btn.onclick = () => {
        try {
          const a = document.createElement("a");
          a.href = canvas.toDataURL("image/png");
          const h = card.querySelector("h3");
          a.download = (h ? h.textContent.trim() : "chart") + ".png";
          document.body.appendChild(a); a.click(); a.remove();
        } catch (e) {}
      };
      if (!card.style.position) card.style.position = "relative";
      card.appendChild(btn);
    });
  }

  // ===== 散場層（宣告散場後）=====
  function renderEgress(eg, labels) {
    const sum = document.getElementById("egress-summary");
    if (!eg.enabled) {
      if (sum) sum.innerHTML = `<div class="row"><span>${T("散場")}</span><b>${T("尚未宣告（按「宣告散場」後產生）")}</b></div>`;
      [egressChart, egressTravelChart, egressOdChart].forEach((c) => { if (c) c.destroy(); });
      egressChart = egressTravelChart = egressOdChart = null;
      return;
    }
    const s = eg.summary || {};
    if (sum) sum.innerHTML =
      `<div class="row"><span>${T("返家率")}</span><b>${s.returned_home}/${s.reached_stadium}（${s.return_pct}%）</b></div>`
      + `<div class="row"><span>${T("平均散場旅時")}</span><b>${s.avg_egress_travel_min} min</b></div>`
      + `<div class="row"><span>${T("清場時間（90% 返家）")}</span><b>${s.clearance_min != null ? s.clearance_min + " min" : T("未達 90%")}</b></div>`;
    egressChart && egressChart.destroy();
    egressChart = new Chart(document.getElementById("chart-egress"), {
      type: "line",
      data: { labels, datasets: [
        { label: T("累積返家"), data: eg.cumulative_home || [], borderColor: "#2FD17A", backgroundColor: "rgba(47,209,122,.15)", fill: true, tension: .3, pointRadius: 0 },
        { label: T("每步離場"), data: eg.departures || [], borderColor: "#FF8A3D", tension: .3, pointRadius: 0, yAxisID: "y1" },
      ] },
      options: { ...lineOpts(), scales: { ...lineOpts().scales, y1: { position: "right", grid: { display: false }, beginAtZero: true } } },
    });
    const h = histogram(eg.travel_time_minutes || [], 12);
    egressTravelChart && egressTravelChart.destroy();
    egressTravelChart = new Chart(document.getElementById("chart-egress-travel"), {
      type: "bar",
      data: { labels: h.labels, datasets: [{ label: T("agent 數"), data: h.counts, backgroundColor: "#FF8A3D" }] },
      options: { responsive: true, animation: false, plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false } }, y: { grid: { color: GRID }, beginAtZero: true } } },
    });
    const od = eg.od || [];
    egressOdChart && egressOdChart.destroy();
    egressOdChart = new Chart(document.getElementById("chart-egress-od"), {
      type: "bar",
      data: { labels: od.map((r) => r[0]), datasets: [{ label: T("返家數"), data: od.map((r) => r[1]), backgroundColor: "#2FD17A" }] },
      options: { responsive: true, animation: false, plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false }, ticks: { maxRotation: 60, minRotation: 60 } }, y: { grid: { color: GRID }, beginAtZero: true } } },
    });
  }

  // ===== 路網層（事件車＋背景車，交通局視角）=====
  function renderNetwork(net, labels) {
    const los = net.los || {};
    const ns = document.getElementById("network-summary");
    if (ns) ns.innerHTML =
      `<div class="row"><span>${T("背景常態車流")}</span><b>${net.ambient_count || 0} veh</b></div>`
      + `<div class="row"><span>${T("服務水準 LOS（平均 / 尖峰）")}</span><b>${los.mean_grade || "—"} / ${los.peak_grade || "—"}</b></div>`
      + `<div class="row"><span>${T("平均 / 尖峰壅塞")}</span><b>${(los.mean_congestion || 0).toFixed(2)} / ${(los.peak_congestion || 0).toFixed(2)}</b></div>`
      + `<div class="row"><span>${T("路網負載占比（事件 / 背景）")}</span><b>${net.event_load_share || 0}% / ${net.ambient_load_share || 0}%</b></div>`;

    const vopts = lineOpts();
    volumeChart && volumeChart.destroy();
    volumeChart = new Chart(document.getElementById("chart-volume"), {
      type: "line",
      data: { labels, datasets: [
        { label: T("事件車"), data: net.volume_event || [], borderColor: "#3FB6FF", backgroundColor: "rgba(63,182,255,.35)", fill: true, tension: .3, pointRadius: 0 },
        { label: T("背景車"), data: net.volume_ambient || [], borderColor: "#586275", backgroundColor: "rgba(88,98,117,.4)", fill: true, tension: .3, pointRadius: 0 },
      ] },
      options: { ...vopts, scales: { ...vopts.scales, y: { ...vopts.scales.y, stacked: true } } },
    });

    const bt = document.getElementById("bottleneck-table");
    const rows = net.bottlenecks || [];
    if (bt) bt.innerHTML = rows.length
      ? `<table><thead><tr><th>${T("路段")}</th><th>V/C</th><th>LOS</th><th>${T("尖峰車流/容量")}</th></tr></thead><tbody>`
        + rows.map((r) => `<tr><td>${esc(r.name)}</td><td>${r.vc}</td>`
          + `<td class="los los-${r.los}">${r.los}</td><td>${r.peak_flow}/${r.capacity}</td></tr>`).join("")
        + `</tbody></table>`
      : `<p class="muted">${T("無瓶頸資料（無背景車或路網未壅塞）。")}</p>`;
  }

  return { init, update, reset, renderAnalysis, downloadAnalysisCSV };
})();
