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
        { label: "平均壅塞", data: [], borderColor: "#ff6d00", backgroundColor: "rgba(255,109,0,.15)", fill: true, tension: .3, pointRadius: 0 },
        { label: "壅塞路段數", data: [], borderColor: "#d50000", tension: .3, pointRadius: 0, yAxisID: "y1" },
      ] },
      options: { ...lineOpts(1), scales: { ...lineOpts(1).scales, y1: { position: "right", grid: { display: false }, beginAtZero: true } } },
    });

    arrivedChart = new Chart(document.getElementById("chart-arrived"), {
      type: "line",
      data: { labels: [], datasets: [
        { label: "已抵達", data: [], borderColor: "#00c853", backgroundColor: "rgba(0,200,83,.15)", fill: true, tension: .3, pointRadius: 0 },
        { label: "移動中", data: [], borderColor: "#3fb6ff", tension: .3, pointRadius: 0 },
      ] },
      options: lineOpts(),
    });

    modeChart = new Chart(document.getElementById("chart-mode"), {
      type: "bar",
      data: { labels: [], datasets: [{ label: "累積選擇次數", data: [], backgroundColor: "#3fb6ff" }] },
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
  }

  return { init, update, reset };
})();
