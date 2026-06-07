/* ================================================================
   simulation.js — 控制面板 UI、狀態顯示、agent 檢視
   只負責 DOM 與發送控制指令；不含模擬邏輯。
   ================================================================ */
const TrafficUI = (() => {
  let send = null; // 由 app.js 注入：send(action, value)

  const $ = (id) => document.getElementById(id);

  function bind(sendFn) {
    send = sendFn;

    $("btn-start").onclick = () => send("start");
    $("btn-pause").onclick = () => send("pause");
    $("btn-step").onclick = () => send("step");
    $("btn-reset").onclick = () => send("reset");

    const speed = $("speed");
    speed.oninput = () => {
      $("speed-val").textContent = parseFloat(speed.value).toFixed(1) + "×";
      send("set_speed", parseFloat(speed.value));
    };

    const agents = $("agents");
    agents.oninput = () => { $("agents-val").textContent = agents.value; };
    agents.onchange = () => send("set_agents", parseInt(agents.value, 10));

    $("mode-mock").onclick = () => setMode("mock");
    $("mode-llm").onclick = () => setMode("llm");
  }

  function setMode(mode) {
    $("mode-mock").classList.toggle("active", mode === "mock");
    $("mode-llm").classList.toggle("active", mode === "llm");
    send("set_mode", mode);
  }

  function applyInitConfig(cfg) {
    if (!cfg) return;
    $("m-cycle").textContent = `0 / ${cfg.max_steps}`;

    // slider 範圍由後端 [ui] 設定下發（單一真實來源），HTML 的值只是 init 前的 fallback
    const ui = cfg.ui;
    if (ui) {
      const speed = $("speed");
      speed.min = ui.speed_min;
      speed.max = ui.speed_max;
      speed.step = ui.speed_step;
      speed.value = ui.speed_default;
      $("speed-val").textContent = parseFloat(ui.speed_default).toFixed(1) + "×";

      const agents = $("agents");
      agents.min = ui.agents_min;
      agents.max = ui.agents_max;
      agents.step = ui.agents_step;
    }

    $("agents").value = cfg.nb_agents;
    $("agents-val").textContent = cfg.nb_agents;
    if (cfg.decision_source) $("m-source").textContent = cfg.decision_source;
  }

  function updateStats(state) {
    $("m-cycle").textContent = `${state.cycle} / ${state.max_steps}`;
    $("m-elapsed").textContent = `${state.elapsed_minutes} 分`;
    $("m-source").textContent = state.decision_source;
    $("m-arrived").textContent = state.status_distribution.arrived || 0;
    $("m-crowded").textContent = state.metrics.crowded_road_count;
    $("m-avgcong").textContent = Number(state.metrics.average_congestion_proxy).toFixed(2);
  }

  function inspectAgent(a) {
    const rows = [
      ["ID", a.agent_id],
      ["姓名", a.profile_name || "—"],
      ["車種", a.vehicle_type],
      ["行為模式", a.active_mode],
      ["狀態", a.route_status],
      ["起點區", a.origin_town],
      ["目前區", a.current_town || "—"],
      ["速度", `${a.speed_kmh} km/h`],
      ["壅塞", Number(a.congestion_proxy).toFixed(2)],
      ["距終點", `${(a.distance_to_destination / 1000).toFixed(2)} km`],
      ["鄰近車輛", a.nearby_agent_count],
    ];
    const rowsHtml = rows
      .map(([k, v]) => `<div class="row"><span>${k}</span><b>${v}</b></div>`)
      .join("");
    const summary = a.trip_summary
      ? `<div class="trip-summary"><span>長期記憶 · 旅次摘要</span><p>${escapeHtml(a.trip_summary)}</p></div>`
      : `<div class="trip-summary"><span>長期記憶 · 旅次摘要</span><p class="muted">尚無旅次記憶。</p></div>`;
    $("agent-inspect").innerHTML = rowsHtml + summary;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove("show"), 2500);
  }

  function setConnected(ok) {
    $("conn-dot").className = "dot " + (ok ? "dot-on" : "dot-off");
    $("conn-text").textContent = ok ? "已連線" : "連線中斷";
  }

  return { bind, applyInitConfig, updateStats, inspectAgent, toast, setConnected };
})();
