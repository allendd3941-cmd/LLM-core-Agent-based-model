/* ================================================================
   simulation.js — 控制面板 UI、狀態顯示、agent 檢視、系統日誌
   只負責 DOM 與發送控制指令；不含模擬邏輯。
   ================================================================ */
const TrafficUI = (() => {
  let send = null; // 由 app.js 注入：send(action, value)
  let profiles = {}; // name → {identity, traits}
  let busyBtn = null; // 目前顯示 spinner 的按鈕

  const $ = (id) => document.getElementById(id);

  // 「套用設定」待套用狀態（拖滑桿後高亮，套用/初始化後清除）
  function markPending() { const b = $("btn-apply"); if (b) b.classList.add("pending"); }
  function clearPending() { const b = $("btn-apply"); if (b) b.classList.remove("pending"); }

  // ---- 按鈕忙碌 spinner ----
  function markBtnBusy(el) { clearBtnBusy(); if (el) { el.classList.add("loading"); busyBtn = el; } }
  function clearBtnBusy() { if (busyBtn) { busyBtn.classList.remove("loading"); busyBtn = null; } }

  function bind(sendFn) {
    send = sendFn;

    $("btn-start").onclick = (e) => { markBtnBusy(e.currentTarget); send("start"); };
    $("btn-pause").onclick = () => send("pause");
    $("btn-step").onclick = (e) => { markBtnBusy(e.currentTarget); send("step"); };
    $("btn-reset").onclick = (e) => { markBtnBusy(e.currentTarget); send("reset"); };

    const speed = $("speed");
    speed.oninput = () => {
      $("speed-val").textContent = parseFloat(speed.value).toFixed(1) + "×";
      send("set_speed", parseFloat(speed.value));
    };

    // 事件車數/背景車/週期數/每週期分鐘：拖動只是「預覽」（更新數字、標記待套用），不自動送出；
    // 按「套用設定」才一次送 apply_config。
    const agents = $("agents");
    agents.oninput = () => { $("agents-val").textContent = agents.value; markPending(); };

    const ambient = $("ambient");
    if (ambient) ambient.oninput = () => { $("ambient-val").textContent = ambient.value; markPending(); };

    const steps = $("steps");
    if (steps) steps.oninput = () => { $("steps-val").textContent = steps.value; markPending(); };
    const stepMin = $("step-minutes");
    if (stepMin) stepMin.onchange = markPending;

    const applyBtn = $("btn-apply");
    if (applyBtn) applyBtn.onclick = (e) => {
      markBtnBusy(e.currentTarget);
      send("apply_config", {
        nb_agents: parseInt($("agents").value, 10),
        ambient: parseInt($("ambient").value, 10),
        max_steps: parseInt($("steps").value, 10),
        step_minutes: parseInt($("step-minutes").value, 10),
      });
      clearPending();
    };

    $("mode-mock").onclick = () => setMode("rule");
    $("mode-llm").onclick = () => setMode("llm");

    const regen = $("btn-regen-profiles");
    if (regen) regen.onclick = (e) => { markBtnBusy(e.currentTarget); send("regenerate_profiles"); };

    const ep = $("btn-edit-prompts");
    if (ep) ep.onclick = openPrompts;
    const pc = $("prompt-close");
    if (pc) pc.onclick = () => { $("prompt-modal").style.display = "none"; };

    const sigToggle = $("toggle-signals");
    if (sigToggle) sigToggle.onchange = () => TrafficMap.toggleSignals(sigToggle.checked);
    const ambToggle = $("toggle-ambient");
    if (ambToggle) ambToggle.onchange = () => TrafficMap.toggleAmbient(ambToggle.checked);

    const lb = $("llm-backend");
    if (lb) lb.onchange = () => refreshLlmModels(lb.value);
    const lm = $("llm-model");
    if (lm) lm.onchange = () => {
      send("set_llm", { backend: $("llm-backend").value, model: lm.value });
      updateLlmHint();
    };

    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.onclick = () => { activateTab(b.dataset.tab); expandDock(); };
    });
    const dockToggle = $("btn-dock-toggle");
    if (dockToggle) dockToggle.onclick = () => {
      const d = $("dock"); if (d) d.classList.toggle("collapsed");
      const g = $("gutter-dock"); if (g && d) g.style.display = d.classList.contains("collapsed") ? "none" : "";
      afterDockAnim();
    };
    setupResizers();
    const clearLog = $("btn-clear-log");
    if (clearLog) clearLog.onclick = () => { const b = $("system-log"); if (b) b.innerHTML = ""; };

    const chatSend = $("chat-send"), chatText = $("chat-text");
    if (chatSend) chatSend.onclick = sendChat;
    if (chatText) chatText.onkeydown = (e) => { if (e.key === "Enter") sendChat(); };
    const chips = $("chat-chips");
    if (chips) chips.onclick = (e) => {
      if (e.target.dataset && e.target.dataset.q) { $("chat-text").value = e.target.dataset.q; sendChat(); }
    };
    const ma = $("chat-mode-ask"), mc = $("chat-mode-act");
    if (ma) ma.onclick = () => setChatMode("ask");
    if (mc) mc.onclick = () => setChatMode("act");
    const ci = $("btn-clear-intervene");
    if (ci) ci.onclick = () => send("clear_intervention");
    if ($("chat-chips")) setChatMode("ask");

    const ragBtn = $("btn-rag");
    if (ragBtn) ragBtn.onclick = openRag;
    const upBtn = $("btn-upload-scenario");
    if (upBtn) upBtn.onclick = openUpload;
    const uc = $("util-close");
    if (uc) uc.onclick = () => { $("util-modal").style.display = "none"; };
  }

  async function postJson(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    return r.json();
  }

  // ===== RAG 知識庫 =====
  async function openRag() {
    $("util-title").textContent = "RAG 知識庫";
    $("util-body").innerHTML =
      `<p class="hint">上傳純文字 / markdown / csv，decision 時會檢索相關內容注入。</p>`
      + `<label class="lg-toggle"><input type="checkbox" id="rag-enabled"> 啟用 RAG</label>`
      + `<div style="margin:8px 0"><input type="file" id="rag-file" accept=".txt,.md,.csv,.json" /> `
      + `<button class="seg-btn" id="rag-add">加入</button></div>`
      + `<div id="rag-stat" class="hint"></div>`
      + `<button class="seg-btn" id="rag-clear" style="margin-top:6px">清空知識庫</button>`;
    $("util-modal").style.display = "flex";
    const refresh = async () => {
      const s = await (await fetch("/api/rag/status")).json();
      $("rag-enabled").checked = s.enabled;
      $("rag-stat").textContent = `共 ${s.chunks} 塊；來源：`
        + (s.sources.map((x) => `${x.name}(${x.chunks})`).join("、") || "無");
    };
    await refresh();
    $("rag-enabled").onchange = async () => { await postJson("/api/rag/toggle", { enabled: $("rag-enabled").checked }); refresh(); };
    $("rag-add").onclick = async () => {
      const f = $("rag-file").files[0]; if (!f) return;
      await postJson("/api/rag/add", { name: f.name, text: await f.text() }); refresh();
    };
    $("rag-clear").onclick = async () => { await postJson("/api/rag/clear", {}); refresh(); };
  }

  // ===== 上傳自訂場景 =====
  function openUpload() {
    $("util-title").textContent = "上傳自訂場景";
    $("util-body").innerHTML =
      `<p class="hint">上傳本專案格式的路網 graphml（由 build_scenario / build_roads 產生）＋選填人口 CSV。</p>`
      + `<div class="prompt-field"><label>場景 key（英數）</label><input id="up-key" type="text" /></div>`
      + `<div class="prompt-field"><label>顯示名稱</label><input id="up-name" type="text" /></div>`
      + `<div class="prompt-field"><label>縣市篩選（如 高雄）</label><input id="up-county" type="text" value="臺南|台南" /></div>`
      + `<div class="prompt-field"><label>目的地 lat / lng / 區名</label>`
      + `<input id="up-lat" type="text" placeholder="lat" /> <input id="up-lng" type="text" placeholder="lng" /> <input id="up-town" type="text" placeholder="區名" /></div>`
      + `<div class="prompt-field"><label>路網 graphml</label><input id="up-graphml" type="file" accept=".graphml,.xml" /></div>`
      + `<div class="prompt-field"><label>人口 CSV（選填）</label><input id="up-pop" type="file" accept=".csv" /></div>`
      + `<button class="seg-btn" id="up-go">上傳並註冊</button> <span id="up-stat" class="hint"></span>`;
    $("util-modal").style.display = "flex";
    $("up-go").onclick = doUpload;
  }

  async function doUpload() {
    const stat = $("up-stat"); stat.textContent = "上傳中…";
    const gf = $("up-graphml").files[0];
    if (!gf) { stat.textContent = "請選 graphml"; return; }
    const pf = $("up-pop").files[0];
    const body = {
      key: $("up-key").value, name: $("up-name").value, county_filter: $("up-county").value,
      dest_lat: parseFloat($("up-lat").value) || null, dest_lng: parseFloat($("up-lng").value) || null,
      dest_town: $("up-town").value, roads_graphml: await gf.text(),
      population_csv: pf ? await pf.text() : "",
    };
    const r = await postJson("/api/scenario/upload", body);
    if (r.ok) {
      stat.textContent = `成功（${r.nodes} 節點）。切換中…`;
      setScenarios({ list: r.scenarios, active: body.key });
      send("set_scenario", body.key);
    } else {
      stat.textContent = "失敗：" + (r.error || "未知");
    }
  }

  let llmVllmModels = [];

  function setLlmInit(llm) {
    if (!llm) return;
    llmVllmModels = llm.vllm_models || [];
    const lb = $("llm-backend");
    if (lb && llm.backend) lb.value = llm.backend;
    refreshLlmModels(llm.backend || "ollama", llm.current_model || "");
  }

  async function refreshLlmModels(backend, preselect) {
    const lm = $("llm-model");
    if (!lm) return;
    let models = [];
    if (backend === "vllm") {
      models = llmVllmModels;
    } else {
      try {
        const res = await fetch("/api/llm/models?backend=ollama");
        models = (await res.json()).models || [];
      } catch (e) { models = []; }
    }
    lm.innerHTML = models.length
      ? models.map((m) => `<option value="${m.id}">${escapeHtml(m.label || m.id)}</option>`).join("")
      : `<option value="">（無可用模型）</option>`;
    if (preselect && models.some((m) => m.id === preselect)) lm.value = preselect;
    updateLlmHint();
  }

  function updateLlmHint() {
    const hint = $("llm-model-hint");
    if (!hint) return;
    const backend = $("llm-backend").value;
    const id = $("llm-model").value;
    if (backend === "vllm") {
      const m = llmVllmModels.find((x) => x.id === id);
      hint.textContent = m
        ? `${m.params} · context ${m.max_context}（${m.note}）· 對齊目標：請自行 vllm serve 此模型`
        : "vLLM：請先 vllm serve 對應模型";
    } else {
      hint.textContent = id ? `Ollama 模型（context 由系統設 ~8192）` : "Ollama 未偵測到模型（請先 ollama pull）";
    }
  }

  const CORE_LABEL = { rule: "規則式", llm: "LLM", mock: "規則式" };

  function setMode(mode) {
    $("mode-mock").classList.toggle("active", mode !== "llm");
    $("mode-llm").classList.toggle("active", mode === "llm");
    send("set_mode", mode);
  }

  function applyInitConfig(cfg) {
    if (!cfg) return;
    $("m-cycle").textContent = `0 / ${cfg.max_steps}`;

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

      const steps = $("steps");
      if (steps) { steps.min = ui.steps_min; steps.max = ui.steps_max; steps.step = ui.steps_step; }
      const sm = $("step-minutes");
      if (sm && ui.step_minutes_options) {
        sm.innerHTML = ui.step_minutes_options.map((m) => `<option value="${m}">${m} 分</option>`).join("");
      }
    }

    $("agents").value = cfg.nb_agents;
    $("agents-val").textContent = cfg.nb_agents;

    const stepsEl = $("steps");
    if (stepsEl) { stepsEl.value = cfg.max_steps; $("steps-val").textContent = cfg.max_steps; }
    const smEl = $("step-minutes");
    if (smEl) smEl.value = cfg.step_minutes;

    if (cfg.ambient) {
      const amb = $("ambient");
      if (amb) {
        amb.max = cfg.ambient.max;
        amb.value = cfg.ambient.count;
        $("ambient-val").textContent = cfg.ambient.count;
      }
      $("m-ambient").textContent = cfg.ambient.active != null ? cfg.ambient.active : 0;
    }
    if (cfg.current_core) {
      $("mode-mock").classList.toggle("active", cfg.current_core !== "llm");
      $("mode-llm").classList.toggle("active", cfg.current_core === "llm");
    }
    if (cfg.decision_source) $("m-source").textContent = CORE_LABEL[cfg.decision_source] || cfg.decision_source;
    setLlmInit(cfg.llm);
    clearPending();
  }

  function updateStats(state) {
    $("m-cycle").textContent = `${state.cycle} / ${state.max_steps}`;
    $("m-elapsed").textContent = `${state.elapsed_minutes} 分`;
    $("m-source").textContent = CORE_LABEL[state.decision_source] || state.decision_source;
    $("m-arrived").textContent = state.status_distribution.arrived || 0;
    $("m-pending").textContent = (state.status_distribution && state.status_distribution.created) || 0;
    $("m-ambient").textContent = (state.metrics && state.metrics.ambient_count) || 0;
    $("m-crowded").textContent = state.metrics.crowded_road_count;
    $("m-avgcong").textContent = Number(state.metrics.average_congestion_proxy).toFixed(2);
  }

  function inspectAgent(a) {
    const rows = [
      ["ID", a.agent_id],
      ["姓名", a.profile_name || "—"],
      ["車種", a.vehicle_type],
      ["行為模式", a.active_mode],
      ["狀態", a.waiting_at_signal ? "等紅燈" : a.route_status],
      ["起點區", a.origin_town],
      ["目前區", a.current_town || "—"],
      ["速度", `${a.speed_kmh} km/h`],
      ["壅塞", Number(a.congestion_proxy).toFixed(2)],
      ["距終點", `${(a.distance_to_destination / 1000).toFixed(2)} km`],
      ["鄰近車輛", a.nearby_agent_count],
      ["上次重決", a.last_decision_cycle != null ? `第 ${a.last_decision_cycle} 步` : "—"],
    ];
    const rowsHtml = rows
      .map(([k, v]) => `<div class="row"><span>${k}</span><b>${escapeHtml(String(v))}</b></div>`)
      .join("");

    const reason = a.decision_reason
      ? `<div class="inspect-block"><span>決策理由（選此行為模式的原因）</span><p>${escapeHtml(a.decision_reason)}</p></div>`
      : "";

    let summary;
    if (a.trip_summary) {
      const srcLabel = a.summary_source === "llm" ? "LLM 摘要" : "模板";
      summary = `<div class="inspect-block"><span>長期記憶 · 旅次摘要 <em>（${srcLabel}）</em></span><p>${escapeHtml(a.trip_summary)}</p></div>`;
    } else {
      const placeholder = a.summary_source === "pending" ? "等待 LLM 摘要…" : "尚無旅次記憶。";
      summary = `<div class="inspect-block"><span>長期記憶 · 旅次摘要</span><p class="muted">${placeholder}</p></div>`;
    }

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

  // ---- 決策日誌（即時，走 WebSocket）----
  function updateDecisions(decisions, health) {
    decisions = decisions || [];
    health = health || {};
    const h = $("decision-health");
    if (h) {
      h.innerHTML = health.source === "rule"
        ? `<b>規則式核心</b>：無 LLM 決策日誌（確定性、不產生 LLM 決策）。`
        : `本步重決 <b>${health.triggered || 0}</b> 台 · 解析成功 <b>${health.decided || 0}</b>`
          + ` · fallback <b>${health.fallback || 0}</b>（fallback 多＝LLM 解析有問題）`;
    }
    const box = $("decision-output");
    if (!box) return;
    if (!decisions.length) {
      box.innerHTML = `<span class="muted">${health.source === "rule"
        ? "規則式核心：無 LLM 決策。" : "本步無事件車重決（無壅塞觸發）。"}</span>`;
      return;
    }
    box.innerHTML = decisions.map((d) =>
      `<div class="dec-row"><b>${escapeHtml(d.name)}</b> → <span class="dec-mode">${escapeHtml(d.mode)}</span>`
      + `<p class="dec-reason">${escapeHtml(d.reason || "")}</p></div>`).join("");
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

  // ===== 系統日誌（專業 logging）=====
  function fmtTime() { return new Date().toTimeString().slice(0, 8); }

  function log(level, msg) {
    const box = $("system-log");
    if (!box) return;
    const line = document.createElement("div");
    line.className = "log-line log-" + (level || "info");
    line.innerHTML = `<span class="log-ts">${fmtTime()}</span><span class="log-msg">${escapeHtml(String(msg))}</span>`;
    box.appendChild(line);
    while (box.children.length > 200) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }

  // ===== 全域忙碌動畫 + 執行心跳 =====
  function setBusyBar(on) {
    const b = $("busy-bar");
    if (b) b.classList.toggle("active", !!on);
  }

  function setRunState(state, cycle) {
    const el = $("run-state"), t = $("run-state-text");
    if (!el || !t) return;
    el.className = "run-state run-" + state;
    t.textContent = state === "running" ? `執行中 第 ${cycle || 0} 步`
      : state === "paused" ? "已暫停"
      : state === "done" ? "已完成"
      : state === "busy" ? "處理中…"
      : "待命";
  }

  function expandDock() {
    const d = $("dock"); if (d) d.classList.remove("collapsed");
    const g = $("gutter-dock"); if (g) g.style.display = "";
    afterDockAnim();
  }

  // 底部面板高度變動（收合/展開/切分頁）後，等 CSS 過場結束再讓地圖重算尺寸補滿圖磚。
  function afterDockAnim() {
    setTimeout(() => { if (window.TrafficMap && TrafficMap.resize) TrafficMap.resize(); }, 280);
  }

  // ===== 可拖曳窗格（左面板寬 / 底部面板高；localStorage 記憶、雙擊還原）=====
  function setupResizers() {
    const layout = $("layout");
    if (!layout) return;
    const dock = $("dock");
    const gv = $("gutter-left");
    const gh = $("gutter-dock");
    const LW = "ui.leftW", DH = "ui.dockH";
    const savedW = localStorage.getItem(LW);
    if (savedW) layout.style.setProperty("--left-w", savedW);
    const savedH = localStorage.getItem(DH);
    if (savedH) layout.style.setProperty("--dock-h", savedH);

    let raf = null;
    const mapResize = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => { raf = null; if (window.TrafficMap && TrafficMap.resize) TrafficMap.resize(); });
    };
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));

    function bindGutter(gutter, varName, key, computeVal) {
      if (!gutter) return;
      gutter.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        try { gutter.setPointerCapture(e.pointerId); } catch (_) {}
        gutter.classList.add("dragging");
        const move = (ev) => { layout.style.setProperty(varName, computeVal(ev)); mapResize(); };
        const up = () => {
          gutter.classList.remove("dragging");
          try { gutter.releasePointerCapture(e.pointerId); } catch (_) {}
          gutter.removeEventListener("pointermove", move);
          gutter.removeEventListener("pointerup", up);
          const v = getComputedStyle(layout).getPropertyValue(varName).trim();
          if (v) localStorage.setItem(key, v);
          mapResize();
        };
        gutter.addEventListener("pointermove", move);
        gutter.addEventListener("pointerup", up);
      });
      gutter.addEventListener("dblclick", () => {       // 雙擊還原預設
        layout.style.removeProperty(varName);
        localStorage.removeItem(key);
        mapResize();
      });
    }

    bindGutter(gv, "--left-w", LW, (ev) =>
      clamp(ev.clientX - layout.getBoundingClientRect().left - 12, 240, 560) + "px");
    bindGutter(gh, "--dock-h", DH, (ev) => {
      const stage = dock.parentElement.getBoundingClientRect();
      return clamp(stage.bottom - ev.clientY, 120, window.innerHeight * 0.7) + "px";
    });
  }

  // ===== 右側分頁 =====
  function activateTab(name) {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  }

  // ===== 編輯 Prompts =====
  async function openPrompts() {
    const box = $("prompt-fields");
    box.innerHTML = "載入中…";
    $("prompt-modal").style.display = "flex";
    try {
      const data = await (await fetch("/api/prompts")).json();
      box.innerHTML = "";
      Object.entries(data).forEach(([name, p]) => {
        const wrap = document.createElement("div");
        wrap.className = "prompt-field";
        wrap.innerHTML =
          `<label>${escapeHtml(p.label)} ${p.overridden ? "<em>(已自訂)</em>" : ""}</label>`
          + `<textarea data-name="${name}">${escapeHtml(p.current)}</textarea>`
          + `<div class="prompt-btns">`
          + `<button class="seg-btn" data-act="save" data-name="${name}">套用</button>`
          + `<button class="seg-btn" data-act="reset" data-name="${name}">還原預設</button></div>`;
        box.appendChild(wrap);
      });
      box.querySelectorAll("button[data-act]").forEach((b) => {
        b.onclick = () => {
          const ta = box.querySelector(`textarea[data-name="${b.dataset.name}"]`);
          if (b.dataset.act === "reset") {
            ta.value = "";
            send("set_prompt", { name: b.dataset.name, text: "" });
          } else {
            send("set_prompt", { name: b.dataset.name, text: ta.value });
          }
        };
      });
    } catch (e) {
      box.innerHTML = "載入失敗（伺服器未連線？）。";
    }
  }

  // ===== 場景切換 =====
  function setScenarios(scn) {
    const sel = $("scenario-select");
    if (!sel || !scn) return;
    sel.innerHTML = (scn.list || []).map(
      (s) => `<option value="${s.key}">${escapeHtml(s.name)}</option>`).join("");
    if (scn.active) sel.value = scn.active;
    sel.onchange = () => send("set_scenario", sel.value);
  }

  // ===== 對話 / 介入 =====
  let chatMode = "ask";
  const CHIPS = {
    ask: [["現在哪裡最塞？", "哪裡最塞？"], ["目前抵達多少人？還有多少在路上？", "抵達多少？"],
          ["整體交通狀況與趨勢如何？", "整體如何？"]],
    act: [["避開東區一帶", "避開東區"], ["從永康區湧入 300 台車", "永康湧入300"], ["避開北區", "避開北區"]],
  };

  function setChatMode(m) {
    chatMode = m;
    $("chat-mode-ask").classList.toggle("active", m === "ask");
    $("chat-mode-act").classList.toggle("active", m === "act");
    $("chat-hint").textContent = m === "act"
      ? "輸入介入指令（避開某區 / 從某區湧入 N 台），套用後即時更新地圖。"
      : "暫停時詢問當前路況（唯讀，用所選 LLM 回答）。";
    $("chat-text").placeholder = m === "act" ? "例如：避開東區 / 從永康區湧入300台" : "輸入問題後按 Enter…";
    $("chat-chips").innerHTML = (CHIPS[m] || [])
      .map(([q, l]) => `<button class="chip" data-q="${q}">${l}</button>`).join("");
  }

  function sendChat() {
    const t = $("chat-text");
    const q = (t.value || "").trim();
    if (!q) return;
    appendChat("user", q);
    t.value = "";
    send(chatMode === "act" ? "intervene" : "ask", q);
  }

  function appendChat(role, text) {
    const log = $("chat-log");
    if (!log) return;
    const div = document.createElement("div");
    div.className = "chat-msg chat-" + role;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  return { bind, applyInitConfig, updateStats, inspectAgent, setProfiles,
           updateDecisions, toast, setConnected, appendChat, setScenarios, activateTab,
           log, setBusyBar, setRunState, clearBtnBusy, expandDock };
})();
