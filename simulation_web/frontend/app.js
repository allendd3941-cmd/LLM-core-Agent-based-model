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
    start: "開始模擬", step: "單步", reset: "重設", apply_config: "套用設定",
    set_scenario: "切換場景", regenerate_profiles: "重新生成人物", set_agents: "設定事件車數",
    set_ambient: "設定背景車", set_max_steps: "設定週期數", set_step_minutes: "設定每週期分鐘",
  };

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws`;
  }

  function beginBusy(action) {
    TrafficUI.setBusyBar(true);
    TrafficUI.setRunState("busy");
    clearTimeout(busyTimer);
    busyTimer = setTimeout(() => { endBusy(); TrafficUI.log("warn", "操作逾時（90 秒未回應），已解除忙碌狀態。"); }, 90000);
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
        TrafficUI.log("info", "送出指令：" + (ACTION_LABEL[action] || action));
      }
      ws.send(JSON.stringify({ type: "control", action, value }));
    } else {
      TrafficUI.log("error", "未連線，指令未送出：" + (ACTION_LABEL[action] || action));
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
      TrafficUI.log("success", "WebSocket 已連線。");
      clearTimeout(reconnectTimer);
    };

    ws.onclose = () => {
      TrafficUI.setConnected(false);
      TrafficUI.log("warn", "連線中斷，2 秒後自動重連…");
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
          TrafficCharts.reset();
          endBusy();
          TrafficUI.setRunState("idle");
          TrafficUI.log("success", "初始化完成，可開始模擬。");
          break;
        case "state_update":
          TrafficMap.updateRoads(msg.roads);
          TrafficMap.updateAgents(msg.agents);
          TrafficMap.updateSignalPhase((msg.elapsed_minutes || 0) * 60);
          TrafficUI.updateStats(msg);
          TrafficUI.updateDecisions(msg.decisions, msg.decision_health);
          TrafficCharts.update(msg);
          endBusy();
          TrafficUI.setRunState("running", msg.cycle);
          TrafficUI.log("step",
            `第 ${msg.cycle} 步 · 抵達 ${(msg.status_distribution && msg.status_distribution.arrived) || 0}`
            + ` · 移動 ${(msg.status_distribution && msg.status_distribution.moving) || 0}`
            + ` · 平均壅塞 ${Number(msg.metrics.average_congestion_proxy).toFixed(2)}`);
          break;
        case "analysis":
          TrafficCharts.renderAnalysis(msg);
          TrafficUI.activateTab("analysis");
          TrafficUI.expandDock();
          TrafficUI.log("success", "交通分析已產生（分析分頁）。");
          break;
        case "chat":
          TrafficUI.appendChat("bot", msg.text);
          TrafficUI.log("info", "對話回覆已接收。");
          break;
        case "status":
          TrafficUI.toast(msg.message);
          handleStatus(msg.message);
          break;
      }
    };
  }

  // 啟動
  window.addEventListener("DOMContentLoaded", () => {
    TrafficMap.init((agent) => TrafficUI.inspectAgent(agent));
    TrafficMap.setViewReporter((v) => send("set_view", v));
    TrafficCharts.init();
    TrafficUI.bind(send);
    TrafficUI.log("info", "前端介面已就緒，連線中…");
    connect();
  });
})();
