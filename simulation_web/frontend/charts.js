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
        { label: "平均壅塞", data: [], borderColor: "#FF8A3D", backgroundColor: "rgba(255,138,61,.15)", fill: true, tension: .3, pointRadius: 0 },
        { label: "壅塞路段數", data: [], borderColor: "#FF4D4D", tension: .3, pointRadius: 0, yAxisID: "y1" },
      ] },
      options: { ...lineOpts(1), scales: { ...lineOpts(1).scales, y1: { position: "right", grid: { display: false }, beginAtZero: true } } },
    });

    arrivedChart = new Chart(document.getElementById("chart-arrived"), {
      type: "line",
      data: { labels: [], datasets: [
        { label: "已抵達", data: [], borderColor: "#2FD17A", backgroundColor: "rgba(47,209,122,.15)", fill: true, tension: .3, pointRadius: 0 },
        { label: "移動中", data: [], borderColor: "#3FB6FF", tension: .3, pointRadius: 0 },
      ] },
      options: lineOpts(),
    });

    modeChart = new Chart(document.getElementById("chart-mode"), {
      type: "bar",
      data: { labels: [], datasets: [{ label: "累積選擇次數", data: [], backgroundColor: "#3FB6FF" }] },
      options: { responsive: true, animation: false, plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false } }, y: { grid: { color: GRID }, beginAtZero: true, ticks: { stepSize: 1 } } } },
    });
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
    [arrivalChart, travelChart, odChart, volumeChart].forEach((c) => { if (c) c.destroy(); });
    arrivalChart = travelChart = odChart = volumeChart = null;
  }

  // ===== 模擬後交通分析（收到 type:analysis 時呼叫）=====
  let arrivalChart = null, travelChart = null, odChart = null, volumeChart = null;

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
      `<div class="row"><span>抵達率</span><b>${s.arrived}/${s.total_agents}（${s.arrival_pct}%）</b></div>`
      + `<div class="row"><span>平均旅行時間</span><b>${s.avg_travel_min} 分</b></div>`
      + `<div class="row"><span>號誌停等總次數</span><b>${s.total_signal_stops}</b></div>`;

    const labels = data.cycles || [];
    arrivalChart && arrivalChart.destroy();
    arrivalChart = new Chart(document.getElementById("chart-arrival"), {
      type: "line",
      data: { labels, datasets: [
        { label: "累積抵達", data: data.cumulative_arrived || [], borderColor: "#2FD17A", backgroundColor: "rgba(47,209,122,.15)", fill: true, tension: .3, pointRadius: 0 },
        { label: "每步抵達率", data: data.arrival_rate || [], borderColor: "#FFB020", tension: .3, pointRadius: 0, yAxisID: "y1" },
        { label: "每步出發", data: data.departures || [], borderColor: "#B388FF", borderDash: [4, 3], tension: .3, pointRadius: 0, yAxisID: "y1" },
      ] },
      options: { ...lineOpts(), scales: { ...lineOpts().scales, y1: { position: "right", grid: { display: false }, beginAtZero: true } } },
    });

    const h = histogram(data.travel_time_minutes || [], 12);
    travelChart && travelChart.destroy();
    travelChart = new Chart(document.getElementById("chart-traveltime"), {
      type: "bar",
      data: { labels: h.labels, datasets: [{ label: "agent 數", data: h.counts, backgroundColor: "#3FB6FF" }] },
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
        { label: "實際", data: od.map((r) => r[1]), backgroundColor: "#3FB6FF" },
        { label: "重力期望", data: od.map((r) => Math.round((expShare[r[0]] || 0) * total)), backgroundColor: "rgba(255,176,32,.7)" },
      ] },
      options: { responsive: true, animation: false, plugins: { legend: { labels: { boxWidth: 12 } } },
        scales: { x: { grid: { display: false }, ticks: { maxRotation: 60, minRotation: 60 } }, y: { grid: { color: GRID }, beginAtZero: true } } },
    });

    renderNetwork(data.network || {}, labels);
  }

  // ===== 路網層（事件車＋背景車，交通局視角）=====
  function renderNetwork(net, labels) {
    const los = net.los || {};
    const ns = document.getElementById("network-summary");
    if (ns) ns.innerHTML =
      `<div class="row"><span>背景常態車流</span><b>${net.ambient_count || 0} 台</b></div>`
      + `<div class="row"><span>服務水準 LOS（平均 / 尖峰）</span><b>${los.mean_grade || "—"} / ${los.peak_grade || "—"}</b></div>`
      + `<div class="row"><span>平均 / 尖峰壅塞</span><b>${(los.mean_congestion || 0).toFixed(2)} / ${(los.peak_congestion || 0).toFixed(2)}</b></div>`
      + `<div class="row"><span>路網負載占比（事件 / 背景）</span><b>${net.event_load_share || 0}% / ${net.ambient_load_share || 0}%</b></div>`;

    const vopts = lineOpts();
    volumeChart && volumeChart.destroy();
    volumeChart = new Chart(document.getElementById("chart-volume"), {
      type: "line",
      data: { labels, datasets: [
        { label: "事件車", data: net.volume_event || [], borderColor: "#3FB6FF", backgroundColor: "rgba(63,182,255,.35)", fill: true, tension: .3, pointRadius: 0 },
        { label: "背景車", data: net.volume_ambient || [], borderColor: "#586275", backgroundColor: "rgba(88,98,117,.4)", fill: true, tension: .3, pointRadius: 0 },
      ] },
      options: { ...vopts, scales: { ...vopts.scales, y: { ...vopts.scales.y, stacked: true } } },
    });

    const bt = document.getElementById("bottleneck-table");
    const rows = net.bottlenecks || [];
    if (bt) bt.innerHTML = rows.length
      ? `<table><thead><tr><th>路段</th><th>V/C</th><th>LOS</th><th>尖峰車流/容量</th></tr></thead><tbody>`
        + rows.map((r) => `<tr><td>${esc(r.name)}</td><td>${r.vc}</td>`
          + `<td class="los los-${r.los}">${r.los}</td><td>${r.peak_flow}/${r.capacity}</td></tr>`).join("")
        + `</tbody></table>`
      : `<p class="muted">無瓶頸資料（無背景車或路網未壅塞）。</p>`;
  }

  return { init, update, reset, renderAnalysis };
})();
