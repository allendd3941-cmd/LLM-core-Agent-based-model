/* ================================================================
   app.js — 主控制器 / WebSocket 連線管理
   把 WebSocket 訊息分派給 TrafficMap / TrafficCharts / TrafficUI。
   ================================================================ */
(() => {
  let ws = null;
  let reconnectTimer = null;

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws`;
  }

  function send(action, value) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "control", action, value }));
    }
  }

  function connect() {
    ws = new WebSocket(wsUrl());

    ws.onopen = () => {
      TrafficUI.setConnected(true);
      clearTimeout(reconnectTimer);
    };

    ws.onclose = () => {
      TrafficUI.setConnected(false);
      reconnectTimer = setTimeout(connect, 2000); // 自動重連
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      switch (msg.type) {
        case "init":
          TrafficMap.setInit(msg);
          TrafficUI.applyInitConfig(msg.config);
          TrafficUI.setProfiles(msg.agent_profiles || {});
          TrafficUI.refreshDecisionSteps();
          TrafficCharts.reset();
          break;
        case "state_update":
          TrafficMap.updateRoads(msg.roads);
          TrafficMap.updateAgents(msg.agents);
          TrafficUI.updateStats(msg);
          TrafficCharts.update(msg);
          break;
        case "status":
          TrafficUI.toast(msg.message);
          break;
      }
    };
  }

  // 啟動
  window.addEventListener("DOMContentLoaded", () => {
    TrafficMap.init((agent) => TrafficUI.inspectAgent(agent));
    TrafficCharts.init();
    TrafficUI.bind(send);
    connect();
  });
})();
