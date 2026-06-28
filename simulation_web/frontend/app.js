/* ================================================================
   app.js — 主控制器 / WebSocket 連線管理
   分派 WS 訊息給 TrafficMap / TrafficCharts / TrafficUI；
   並驅動全域忙碌動畫、執行心跳、系統日誌。
   ================================================================ */
(() => {
  let ws = null;
  let reconnectTimer = null;
  let busyTimer = null;

  // 觸發「重新初始化 / 開始跑」的重操作 → 顯示忙碌動畫直到後端回應。
  const HEAVY = new Set([
    "start", "step", "reset", "apply_config", "set_scenario",
    "regenerate_profiles", "set_agents", "set_ambient", "set_max_steps", "set_step_minutes",
  ]);
  const ACTION_LABEL = {
    start: T("開始模擬"), step: T("單步"), reset: T("重設"), apply_config: T("套用設定"),
    set_scenario: T("切換場景"), regenerate_profiles: T("重新生成人物"), set_agents: T("設定事件車數"),
    set_ambient: T("設定背景車"), set_max_steps: T("設定週期數"), set_step_minutes: T("設定每週期分鐘"),
  };

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws`;
  }

  function beginBusy(action) {
    TrafficUI.setBusyBar(true);
    TrafficUI.setRunState("busy");
    clearTimeout(busyTimer);
    busyTimer = setTimeout(() => { endBusy(); TrafficUI.log("warn", "Operation timed out (no response in 90 s); cleared busy state."); }, 90000);
  }

  function endBusy() {
    TrafficUI.setBusyBar(false);
    TrafficUI.clearBtnBusy();
    clearTimeout(busyTimer);
    busyTimer = null;
  }

  function send(action, value) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      if (HEAVY.has(action)) {
        beginBusy(action);
        TrafficUI.log("info", "Command sent: " + (ACTION_LABEL[action] || action));
      }
      ws.send(JSON.stringify({ type: "control", action, value }));
    } else {
      TrafficUI.log("error", "Not connected; command not sent: " + (ACTION_LABEL[action] || action));
    }
  }

  // 依 status 文字判斷日誌等級與執行心跳。
  function handleStatus(message) {
    const m = String(message || "");
    let level = "info";
    if (/失敗|錯誤/.test(m)) level = "error";
    else if (/無法|逾時/.test(m)) level = "warn";
    else if (/完成|已套用|已切換|已重設|已設定|初始化完成|設為/.test(m)) level = "success";
    TrafficUI.log(level, m);

    if (/暫停/.test(m)) { TrafficUI.setRunState("paused"); endBusy(); }
    else if (/完成/.test(m)) { TrafficUI.setRunState("done"); endBusy(); }
    else if (/重設/.test(m)) { TrafficUI.setRunState("idle"); endBusy(); }
    else if (/開始執行/.test(m)) { TrafficUI.setRunState("running", 0); }
    else if (/無法/.test(m)) { endBusy(); }
  }

  function connect() {
    ws = new WebSocket(wsUrl());

    ws.onopen = () => {
      TrafficUI.setConnected(true);
      TrafficUI.log("success", T("WebSocket 已連線。"));
      clearTimeout(reconnectTimer);
    };

    ws.onclose = () => {
      TrafficUI.setConnected(false);
      TrafficUI.log("warn", T("連線中斷，2 秒後自動重連…"));
      endBusy();
      reconnectTimer = setTimeout(connect, 2000);
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      switch (msg.type) {
        case "init":
          TrafficMap.setInit(msg);
          TrafficUI.applyInitConfig(msg.config);
          TrafficUI.setScenarios(msg.scenario);
          TrafficUI.setProfiles(msg.agent_profiles || {});
          TrafficUI.resetDecisions();   // 重設清掉決策歷史，不吃上次模擬
          TrafficCharts.reset();
          endBusy();
          TrafficUI.setRunState("idle");
          TrafficUI.log("success", T("初始化完成，可開始模擬。"));
          break;
        case "state_update":
          TrafficMap.updateRoads(msg.roads);
          TrafficMap.updateAgents(msg.agents);
          TrafficMap.updateSignalPhase((msg.elapsed_minutes || 0) * 60);
          TrafficUI.updateStats(msg);
          TrafficUI.updateDecisions(msg.decisions, msg.decision_health, msg.cycle, msg.rag_provenance);
          TrafficCharts.update(msg);
          endBusy();
          TrafficUI.setRunState("running", msg.cycle);
          TrafficUI.log("step",
            `Step ${msg.cycle} · arrived ${(msg.status_distribution && msg.status_distribution.arrived) || 0}`
            + ` · moving ${(msg.status_distribution && msg.status_distribution.moving) || 0}`
            + ` · avg congestion ${Number(msg.metrics.average_congestion_proxy).toFixed(2)}`);
          break;
        case "analysis":
          TrafficCharts.renderAnalysis(msg);
          TrafficUI.activateTab("analysis");
          TrafficUI.expandDock();
          TrafficUI.log("success", T("交通分析已產生（分析分頁）。"));
          break;
        case "chat":
          TrafficUI.appendChat("bot", msg.text);
          TrafficUI.log("info", T("對話回覆已接收。"));
          break;
        case "status":
          TrafficUI.toast(msg.message);
          handleStatus(msg.message);
          break;
        case "detector_snap":
          if (msg.ok) {
            TrafficMap.addStagedDetector(msg.lat, msg.lng, msg.label);
            TrafficUI.onDetectorPlaced(msg.label);
          } else {
            const d = msg.dist ? ` (nearest road ~${Math.round(msg.dist)} m away)` : "";
            TrafficUI.toast("Too far from any road; move closer or zoom in and try again." + d);
            TrafficUI.log("warn", "Detector placement failed: no road nearby to snap to." + d);
          }
          break;
        case "download":
          triggerDownload(msg.url, msg.name);
          TrafficUI.log("success", "Download ready: " + (msg.label || msg.name));
          break;
        case "agent_path":
          if (typeof TrafficMap !== "undefined" && TrafficMap.drawAgentPath) {
            TrafficMap.drawAgentPath(msg.ingress, msg.egress);
          }
          break;
      }
    };
  }

  // 觸發瀏覽器下載（GIS shapefile zip 等）
  function triggerDownload(url, name) {
    const a = document.createElement("a");
    a.href = url;
    a.download = name || "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // 啟動
  window.addEventListener("DOMContentLoaded", () => {
    TrafficMap.init((agent) => TrafficUI.inspectAgent(agent));
    TrafficMap.setViewReporter((v) => send("set_view", v));
    TrafficMap.setDetectorReporter((lat, lng) => send("snap_detector", { lat, lng }));
    TrafficCharts.init();
    TrafficUI.bind(send);
    TrafficUI.log("info", "Frontend ready; connecting…");
    connect();
  });
})();
