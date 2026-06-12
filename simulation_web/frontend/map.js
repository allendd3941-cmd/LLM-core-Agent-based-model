/* ================================================================
   map.js — Leaflet 地圖控制器
   行政區界、道路（依壅塞上色）、車輛 agent、目的地球場標記。
   ================================================================ */
const TrafficMap = (() => {
  let map = null;
  let townLayer = null;
  let roadLayer = null;
  let roadById = {};          // road_id → Leaflet polyline（用於即時上色）
  let flowOverlay = {};       // road_id → 動態疊畫的 polyline（底圖沒有的非主要道路）
  let agentMarkers = {};      // agent_id → marker
  let agentMode = null;       // "icon"（少量→emoji 圖標）| "dot"（大量→canvas 彩點），依數量自動切
  let stadiumMarker = null;
  let onAgentSelect = null;

  // agent 依「狀態」上色（與道路壅塞上色分離，不再重複混淆）
  const AGENT_ICON_MAX = 150;  // ≤ 此數用 emoji 圖標；超過退回 canvas 彩點保效能
  const STATE_COLOR = {
    moving: "#3fb6ff",   // 移動中（藍）
    waiting: "#ffb300",  // 等紅燈（琥珀）
    arrived: "#00c853",  // 已抵達（綠）
    error: "#7a8699",    // 找不到路徑（灰）
  };

  // 號誌圖層（獨立 canvas，與車流/道路完全分離）
  let signalRenderer = null;  // 專用 canvas renderer，避免與車輛 marker 互搶
  let signalLayer = null;     // L.layerGroup
  let signalBars = [];        // [{g0, g1, off}] g0/g1 為 Leaflet polyline
  let signalCfg = null;       // {cycle_s, yellow_s}
  let signalsOn = true;       // 使用者開關
  let lastElapsedS = 0;       // 最近一次相位時間（zoom 進來時補上色用）

  const SIGNAL_MIN_ZOOM = 14; // 號誌是細節層級：放大到此才顯示（兼顧效能與可讀性）
  const SIGNAL_BAR_M = 16;    // 每條相位軸短桿的半長（公尺）
  const SIG_GREEN = "#19d36b";
  const SIG_RED = "#e5403a";

  const BASE_ROAD = { color: "#3a4658", weight: 1.2, opacity: 0.55 };

  function init(onSelect) {
    onAgentSelect = onSelect;
    map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([23.06, 120.23], 12);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution: "&copy; OpenStreetMap &copy; CARTO",
      maxZoom: 19,
    }).addTo(map);
    signalRenderer = L.canvas({ padding: 0.5 });
    map.on("zoomend", refreshSignalVisibility);
  }

  function setInit(data) {
    // 清掉舊圖層（reset 時會重送 init）
    if (townLayer) map.removeLayer(townLayer);
    if (roadLayer) map.removeLayer(roadLayer);
    if (signalLayer) { map.removeLayer(signalLayer); signalLayer = null; }
    Object.values(agentMarkers).forEach((m) => map.removeLayer(m));
    Object.values(flowOverlay).forEach((l) => map.removeLayer(l));
    agentMarkers = {};
    agentMode = null;
    flowOverlay = {};
    roadById = {};
    signalBars = [];

    // 行政區界
    townLayer = L.geoJSON(data.towns_geojson, {
      style: { color: "#4a5a72", weight: 1, fillColor: "#16202e", fillOpacity: 0.25 },
    }).addTo(map);

    // 道路底圖（主要道路）
    roadLayer = L.geoJSON(data.roads_geojson, {
      style: BASE_ROAD,
      onEachFeature: (feature, layer) => {
        const id = feature.properties && feature.properties.road_id;
        if (id) roadById[id] = layer;
      },
    }).addTo(map);

    // 目的地球場
    if (stadiumMarker) map.removeLayer(stadiumMarker);
    stadiumMarker = L.circleMarker([data.stadium.lat, data.stadium.lng], {
      radius: 9, color: "#fff", weight: 2, fillColor: "#ff3b3b", fillOpacity: 1,
    }).addTo(map).bindPopup("🏟️ 亞太棒球場（目的地）");

    // 號誌圖層（方向相位短桿）
    setSignals(data.signals);

    try { map.fitBounds(townLayer.getBounds().pad(0.05)); } catch (e) {}
  }

  // ---- 號誌：建立每路口的兩條相位軸短桿（組0＝ax 路軸，組1＝垂直）----
  function setSignals(cfg) {
    if (!cfg || !cfg.signals || !cfg.signals.length) { signalCfg = null; return; }
    signalCfg = { cycle_s: cfg.cycle_s, yellow_s: cfg.yellow_s };
    signalLayer = L.layerGroup();
    signalBars = [];
    cfg.signals.forEach((s) => {
      if (!s.two) return; // 只畫兩相位路口（單軸/匝道型恆綠，不畫桿避免雜訊）
      const e0 = barEndpoints(s.lat, s.lng, s.ax, SIGNAL_BAR_M);
      const e1 = barEndpoints(s.lat, s.lng, s.ax + 90, SIGNAL_BAR_M);
      const opt = { renderer: signalRenderer, weight: 3, opacity: 0.95, lineCap: "round" };
      const g0 = L.polyline(e0, { ...opt, color: SIG_RED });
      const g1 = L.polyline(e1, { ...opt, color: SIG_RED });
      signalLayer.addLayer(g0); signalLayer.addLayer(g1);
      signalBars.push({ g0, g1, off: s.off });
    });
    updateSignalPhase(lastElapsedS);     // 先上一次色
    refreshSignalVisibility();            // 依 zoom / 開關決定是否掛上地圖
  }

  // 公尺座標方位角 axisDeg（0=東,90=北）→ 以 (lat,lng) 為中心、半長 Lm 的線段兩端 latlng。
  function barEndpoints(lat, lng, axisDeg, Lm) {
    const th = (axisDeg * Math.PI) / 180;
    const dym = Lm * Math.sin(th), dxm = Lm * Math.cos(th);
    const dlat = dym / 111320;
    const dlng = dxm / (111320 * Math.cos((lat * Math.PI) / 180));
    return [[lat - dlat, lng - dlng], [lat + dlat, lng + dlng]];
  }

  // 依模擬時間重算每路口相位並上色（組0綠↔組1紅，黃燈尾段兩組皆紅）。
  function updateSignalPhase(elapsedS) {
    lastElapsedS = elapsedS;
    if (!signalCfg || !signalLayer) return;
    const cycle = signalCfg.cycle_s, yellow = signalCfg.yellow_s, half = cycle / 2;
    signalBars.forEach((b) => {
      const tc = (((elapsedS + b.off) % cycle) + cycle) % cycle;
      const g0green = tc >= 0 && tc < half - yellow;
      const g1green = tc >= half && tc < cycle - yellow;
      b.g0.setStyle({ color: g0green ? SIG_GREEN : SIG_RED });
      b.g1.setStyle({ color: g1green ? SIG_GREEN : SIG_RED });
    });
  }

  // 號誌只在「開關開 且 zoom 夠近」時掛上地圖（效能 + 可讀性）。
  function refreshSignalVisibility() {
    if (!signalLayer) return;
    const show = signalsOn && map.getZoom() >= SIGNAL_MIN_ZOOM;
    const on = map.hasLayer(signalLayer);
    if (show && !on) map.addLayer(signalLayer);
    else if (!show && on) map.removeLayer(signalLayer);
  }

  function toggleSignals(on) {
    signalsOn = on;
    refreshSignalVisibility();
  }

  // agent 狀態（優先序：抵達 > error > 等紅燈 > 移動中）。顏色只反映「狀態」，壅塞由道路表示。
  function agentState(a) {
    if (a.route_status === "arrived") return "arrived";
    if (a.route_status === "error") return "error";
    if (a.waiting_at_signal) return "waiting";
    return "moving";
  }

  // emoji 圖標 HTML：車種 emoji + 狀態色外框 + 角落狀態徽章（等紅燈🚦 / 抵達🏁）。
  function agentIconHtml(a, state) {
    const veh = a.vehicle_type === "機車" ? "🏍️" : "🚗";
    const badge = state === "waiting" ? "🚦" : state === "arrived" ? "🏁" : "";
    const ring = STATE_COLOR[state];
    return `<div class="agent-pin" style="border-color:${ring}">`
      + `<span class="agent-veh">${veh}</span>`
      + (badge ? `<span class="agent-badge">${badge}</span>` : "")
      + `</div>`;
  }

  function makeAgentIcon(a, state) {
    return L.divIcon({ html: agentIconHtml(a, state), className: "", iconSize: [24, 24], iconAnchor: [12, 12] });
  }

  // 把座標相同的 agent 在畫面上散開成一個小圈（spiderfy），避免疊成一點看不到。
  // 只動「顯示座標」，agent 的真實資料不變（inspect 仍顯示原值）。
  function spreadPositions(agents) {
    const groups = {};
    agents.forEach((a) => {
      const key = a.lat.toFixed(5) + "," + a.lng.toFixed(5);
      (groups[key] = groups[key] || []).push(a);
    });
    const R = 0.00013; // 約 14 公尺
    const pos = {};
    Object.values(groups).forEach((list) => {
      if (list.length === 1) {
        pos[list[0].agent_id] = [list[0].lat, list[0].lng];
        return;
      }
      list.forEach((a, i) => {
        const ang = (2 * Math.PI * i) / list.length;
        pos[a.agent_id] = [a.lat + R * Math.sin(ang), a.lng + R * Math.cos(ang)];
      });
    });
    return pos;
  }

  function updateAgents(agents) {
    // 智慧混合：agent 少 → emoji 圖標（好看）；多 → canvas 彩點（保效能）。
    const mode = agents.length <= AGENT_ICON_MAX ? "icon" : "dot";
    if (mode !== agentMode) {  // 模式切換 → 清掉舊 marker 重建（避免混用兩種 marker 型別）
      Object.values(agentMarkers).forEach((m) => map.removeLayer(m));
      agentMarkers = {};
      agentMode = mode;
    }
    const seen = new Set();
    const pos = spreadPositions(agents);
    agents.forEach((a) => {
      seen.add(a.agent_id);
      const ll = pos[a.agent_id] || [a.lat, a.lng];
      const state = agentState(a);
      if (mode === "icon") upsertAgentIcon(a, ll, state);
      else upsertAgentDot(a, ll, state);
    });
    // 移除已不存在的（reset / set_agents）
    Object.keys(agentMarkers).forEach((id) => {
      if (!seen.has(id)) { map.removeLayer(agentMarkers[id]); delete agentMarkers[id]; }
    });
  }

  function upsertAgentIcon(a, ll, state) {
    let m = agentMarkers[a.agent_id];
    if (!m) {
      m = L.marker(ll, { icon: makeAgentIcon(a, state) }).addTo(map);
      m.on("click", () => onAgentSelect && onAgentSelect(m._agentData));
      agentMarkers[a.agent_id] = m;
      m._state = state; m._veh = a.vehicle_type;
    } else {
      m.setLatLng(ll);
      if (m._state !== state || m._veh !== a.vehicle_type) {  // 狀態/車種變了才換 icon（省重繪）
        m.setIcon(makeAgentIcon(a, state));
        m._state = state; m._veh = a.vehicle_type;
      }
    }
    m._agentData = a;  // 每步更新，確保點擊看到最新資料（含 trip_summary）
  }

  function upsertAgentDot(a, ll, state) {
    const radius = a.vehicle_type === "機車" ? 4 : 6;
    let m = agentMarkers[a.agent_id];
    if (!m) {
      m = L.circleMarker(ll, {
        radius, color: "#0b0f16", weight: 1, fillColor: STATE_COLOR[state], fillOpacity: 0.95,
      }).addTo(map);
      m.on("click", () => onAgentSelect && onAgentSelect(m._agentData));
      agentMarkers[a.agent_id] = m;
    } else {
      m.setLatLng(ll);
      m.setStyle({ fillColor: STATE_COLOR[state], radius });
    }
    m._agentData = a;
  }

  function updateRoads(roads) {
    // 先把所有主要道路還原底色
    Object.values(roadById).forEach((l) => l.setStyle(BASE_ROAD));
    const seen = new Set();
    roads.forEach((r) => {
      const layer = roadById[r.road_id];
      if (layer) {
        // 底圖已有（主要道路）→ 直接上色
        layer.setStyle({ color: r.color, weight: 4, opacity: 0.95 });
      } else if (r.coords && r.coords.length > 1) {
        // 底圖沒有（非主要道路）→ 用 snapshot 帶來的幾何疊畫一條，讓壅塞也看得到
        seen.add(r.road_id);
        const latlngs = r.coords.map((c) => [c[1], c[0]]); // [lng,lat] → [lat,lng]
        let ov = flowOverlay[r.road_id];
        if (!ov) {
          ov = L.polyline(latlngs, { color: r.color, weight: 4, opacity: 0.95 }).addTo(map);
          flowOverlay[r.road_id] = ov;
        } else {
          ov.setLatLngs(latlngs);
          ov.setStyle({ color: r.color, weight: 4, opacity: 0.95 });
        }
      }
    });
    // 移除這步已無流量的疊畫
    Object.keys(flowOverlay).forEach((id) => {
      if (!seen.has(id)) { map.removeLayer(flowOverlay[id]); delete flowOverlay[id]; }
    });
  }

  return { init, setInit, updateAgents, updateRoads, updateSignalPhase, toggleSignals };
})();
