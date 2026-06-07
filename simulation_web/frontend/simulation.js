/* ================================================================
   simulation.js — 控制面板 UI、狀態顯示、agent 檢視
   只負責 DOM 與發送控制指令；不含模擬邏輯。
   ================================================================ */
const TrafficUI = (() => {
  let send = null; // 由 app.js 注入：send(action, value)
  let profiles = {}; // name → {identity, traits}（讀自 agent_profile_output_1.txt）

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

    const dsel = $("decision-step");
    if (dsel) dsel.onchange = () => showDecisionOutput(dsel.value);
    const dref = $("decision-refresh");
    if (dref) dref.onclick = () => refreshDecisionSteps();
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
      .map(([k, v]) => `<div class="row"><span>${k}</span><b>${escapeHtml(String(v))}</b></div>`)
      .join("");

    // 決策理由
    const reason = a.decision_reason
      ? `<div class="inspect-block"><span>決策理由（選此行為模式的原因）</span><p>${escapeHtml(a.decision_reason)}</p></div>`
      : "";

    // 長期記憶摘要 + 來源標籤
    const srcLabel = a.summary_source === "llm" ? "LLM 摘要" : "模板";
    const summary = a.trip_summary
      ? `<div class="inspect-block"><span>長期記憶 · 旅次摘要 <em>（${srcLabel}）</em></span><p>${escapeHtml(a.trip_summary)}</p></div>`
      : `<div class="inspect-block"><span>長期記憶 · 旅次摘要</span><p class="muted">尚無旅次記憶。</p></div>`;

    // 人物背景（讀自 agent_profile_output_1.txt，以姓名對應）
    const persona = renderPersona(a.profile_name);

    $("agent-inspect").innerHTML = rowsHtml + reason + summary + persona;
  }

  function renderPersona(name) {
    const p = profiles[name];
    if (!p) return "";
    const id = p.identity || {};
    const tr = p.traits || {};
    const idRows = [
      ["年齡", id.age], ["職業", id.occupation], ["個人收入", id.wage],
      ["家戶收入", id.household_income], ["交通工具", id.vehicle_ownership],
      ["居住地", id.residential_location],
    ].filter(([, v]) => v);
    const first = (x) => Array.isArray(x) ? (x[0] || "") : (x || "");
    const trRows = [
      ["態度", first(tr.attitudes)], ["習慣", first(tr.habits)],
      ["決策傾向", first(tr.decision_making_tendencies)],
      ["經濟取捨", first(tr.economic_preferences_and_tradeoffs)],
    ].filter(([, v]) => v);
    const idHtml = idRows.map(([k, v]) => `<div class="row"><span>${k}</span><b>${escapeHtml(String(v))}</b></div>`).join("");
    const trHtml = trRows.map(([k, v]) => `<div class="persona-trait"><span>${k}</span><p>${escapeHtml(String(v))}</p></div>`).join("");
    return `<div class="inspect-block"><span>人物背景</span>${idHtml}${trHtml}</div>`;
  }

  function setProfiles(p) { profiles = p || {}; }

  // ---- decision making 每步輸出檢視 ----
  async function refreshDecisionSteps() {
    const sel = $("decision-step");
    if (!sel) return;
    try {
      const res = await fetch("/api/decision-outputs");
      const data = await res.json();
      const steps = data.steps || [];
      sel.innerHTML = steps.map((n) => `<option value="${n}">#${n}</option>`).join("");
      if (steps.length) showDecisionOutput(steps[steps.length - 1]);
      else $("decision-output").textContent = "尚無 decision 輸出（LLM 模式跑過才有）。";
    } catch (e) {
      $("decision-output").textContent = "讀取失敗：" + e;
    }
  }

  async function showDecisionOutput(n) {
    try {
      const res = await fetch("/api/decision-outputs/" + n);
      if (!res.ok) { $("decision-output").textContent = "找不到 #" + n; return; }
      const data = await res.json();
      let text = data.text || "";
      try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (e) {} // 能 parse 就美化
      $("decision-output").textContent = text;
    } catch (e) {
      $("decision-output").textContent = "讀取失敗：" + e;
    }
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

  return { bind, applyInitConfig, updateStats, inspectAgent, setProfiles,
           refreshDecisionSteps, toast, setConnected };
})();
