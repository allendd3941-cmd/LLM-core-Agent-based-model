/* ================================================================
   map.js — Leaflet 地圖控制器
   行政區界、道路（依壅塞上色）、車輛 agent（乾淨彩點）、號誌、
   多底圖切換、地圖色調微調、目的地球場標記。
   ================================================================ */
const TrafficMap = (() => {
  let map = null;
  let townLayer = null;
  let roadLayer = null;
  let roadById = {};          // road_id → Leaflet polyline（用於即時上色）
  let flowOverlay = {};       // road_id → 動態疊畫的 polyline（底圖沒有的非主要道路）
  let agentMarkers = {};      // agent_id → marker（一律 canvas 彩點）
  let agentPathLayer = null;  // 被選取 agent 的整趟行走軌跡（進場/散場分色）
  let stadiumMarker = null;
  let onAgentSelect = null;
  let ambientOn = true;       // 背景常態車流顯示開關
  let lastAgents = [];        // 最近一次 agents（toggle 背景車時即時重繪）
  let lastRoads = [];         // 最近一次 roads（zoom 後重套線寬用）
  let agentRenderer = null;   // 車輛專屬高 z canvas renderer（畫在道路之上）
  let onViewChange = null;    // ⑥ 回報可視範圍（zoom+bounds）給後端的 callback
  let _viewTimer = null;      // 視圖回報節流

  // 車流監測器：像 Google 街景丟人——拖曳相機 icon 到路上放開 → 後端吸附 → 暫存點位（套用設定時送出）
  let detectorLayer = null;
  let stagedDetectors = [];       // [{lat,lng}] 暫存（套用設定時帶入 apply_config）
  let detMarkers = [];            // 地圖上的監測器 icon marker（zoom 時重算大小）
  let onDetectorRequest = null;   // (lat,lng) → app 送 snap_detector
  let placing = false;            // 拖曳放置進行中

  // agent 依「狀態」上色（與道路壅塞上色分離）；車種以「大小」區分（不用 emoji）。
  const STATE_COLOR = {
    moving: "#3FB6FF",   // 移動中（藍）
    waiting: "#FFB020",  // 等紅燈（琥珀）
    arrived: "#2FD17A",  // 已抵達（綠）
    error: "#6B7890",    // 找不到路徑（灰）
  };
  const AMBIENT_COLOR = "#586275";   // 背景常態車流：低調灰

  // 號誌圖層（獨立 canvas，與車流/道路完全分離）
  let signalRenderer = null;
  let signalLayer = null;
  let signalBars = [];
  let signalCfg = null;
  let signalsOn = true;
  let lastElapsedS = 0;

  const SIGNAL_MIN_ZOOM = 14;
  const SIGNAL_BAR_M = 16;
  const SIG_GREEN = "#19d36b";
  const SIG_RED = "#e5403a";

  const BASE_ROAD = { color: "#3a4658", weight: 1.2, opacity: 0.55 };

  // ---- 多底圖（免金鑰）----
  function buildBaseLayers() {
    const carto = "&copy; OpenStreetMap &copy; CARTO";
    const osm = "&copy; OpenStreetMap contributors";
    const esri = "Tiles &copy; Esri";
    return {
      "暗色（CARTO Dark）": L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", { attribution: carto, maxZoom: 19 }),
      "淺色（CARTO Positron）": L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", { attribution: carto, maxZoom: 19 }),
      "街道（CARTO Voyager）": L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png", { attribution: carto, maxZoom: 19 }),
      "OSM 標準": L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { attribution: osm, maxZoom: 19 }),
      "衛星影像（Esri）": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { attribution: esri, maxZoom: 19 }),
    };
  }

  // ---- 地圖色調微調控制（可開關；CSS filter 套在底圖圖磚層）----
  function addAppearanceControl() {
    const Ctrl = L.Control.extend({
      options: { position: "topright" },
      onAdd() {
        const div = L.DomUtil.create("div", "map-appearance");
        div.innerHTML =
          `<button class="ma-toggle" id="ma-toggle" title="地圖色調" aria-label="地圖色調"><i class="ti ti-adjustments" aria-hidden="true"></i></button>`
          + `<div class="ma-body" id="ma-body">`
          + `<div class="ma-title"><i class="ti ti-adjustments" aria-hidden="true"></i> 地圖色調</div>`
          + `<label>亮度 <span id="ma-b-v">100%</span></label><input id="ma-bright" type="range" min="40" max="160" value="100">`
          + `<label>對比 <span id="ma-c-v">100%</span></label><input id="ma-contrast" type="range" min="40" max="160" value="100">`
          + `<label>飽和 <span id="ma-s-v">100%</span></label><input id="ma-sat" type="range" min="0" max="200" value="100">`
          + `<button class="ma-reset" id="ma-reset">還原</button>`
          + `</div>`;
        L.DomEvent.disableClickPropagation(div);
        L.DomEvent.disableScrollPropagation(div);
        setTimeout(() => bindAppearance(div), 0);
        return div;
      },
    });
    map.addControl(new Ctrl());
  }

  function bindAppearance(div) {
    const toggle = div.querySelector("#ma-toggle");
    const body = div.querySelector("#ma-body");
    body.style.display = "none";                       // 預設收起
    toggle.onclick = () => { body.style.display = body.style.display === "none" ? "block" : "none"; };
    const b = div.querySelector("#ma-bright");
    const c = div.querySelector("#ma-contrast");
    const s = div.querySelector("#ma-sat");
    const apply = () => {
      const pane = map.getPane("tilePane");
      if (pane) pane.style.filter = `brightness(${b.value}%) contrast(${c.value}%) saturate(${s.value}%)`;
      div.querySelector("#ma-b-v").textContent = b.value + "%";
      div.querySelector("#ma-c-v").textContent = c.value + "%";
      div.querySelector("#ma-s-v").textContent = s.value + "%";
    };
    b.oninput = c.oninput = s.oninput = apply;
    div.querySelector("#ma-reset").onclick = () => { b.value = 100; c.value = 100; s.value = 100; apply(); };
  }

  function init(onSelect) {
    onAgentSelect = onSelect;
    // zoomSnap:0 → 允許小數縮放層級;放大 wheelPxPerZoomLevel → 滾輪連續、絲滑(不再一格一格跳)
    map = L.map("map", {
      zoomControl: true, preferCanvas: true,
      zoomSnap: 0, zoomDelta: 0.5, wheelDebounceTime: 20, wheelPxPerZoomLevel: 140,
    }).setView([23.06, 120.23], 12);
    const baseLayers = buildBaseLayers();
    baseLayers["暗色（CARTO Dark）"].addTo(map);     // 預設底圖
    L.control.layers(baseLayers, null, { position: "topright" }).addTo(map);
    addAppearanceControl();

    signalRenderer = L.canvas({ padding: 0.5 });
    // 車輛專屬高 z-index pane → 永遠畫在彩色道路之上
    map.createPane("agentPane");
    map.getPane("agentPane").style.zIndex = 450;
    agentRenderer = L.canvas({ pane: "agentPane", padding: 0.5 });
    detectorLayer = L.layerGroup().addTo(map);
    map.on("zoomend", rescaleDetectors);   // icon 隨比例尺美觀縮放
    map.on("zoomend", refreshSignalVisibility);
    map.on("zoomend moveend", scheduleReportView);
    map.on("zoomend", () => { if (lastRoads.length) updateRoads(lastRoads); });
    map.on("zoomend", () => { if (lastAgents.length) updateAgents(lastAgents); });  // zoom 後重套車點大小

    // 地圖容器實際尺寸一變（版面長好 / 字型遲到重排 / 視窗縮放 / 收合面板 / 拖曳分隔條）就重算尺寸，
    // 徹底解決「下半部黑塊」（不靠定時猜，ResizeObserver 會抓到所有重排）。
    let _roRaf = null;
    if (window.ResizeObserver) {
      new ResizeObserver(() => {
        if (_roRaf) return;
        _roRaf = requestAnimationFrame(() => { _roRaf = null; map.invalidateSize(); });
      }).observe(map.getContainer());
    } else {
      window.addEventListener("resize", () => map.invalidateSize());
    }
    setTimeout(() => map.invalidateSize(), 200);                 // 首次繪製保險
    window.addEventListener("load", () => map.invalidateSize()); // 所有資源（含字型）載完再補一次
  }

  // 外部（收合底部面板 / 切分頁 / 視窗變動）呼叫，讓地圖重算尺寸補滿圖磚。
  function resize() { if (map) map.invalidateSize(); }

  // 道路線寬依 zoom 縮放（細、半透明 → 不蓋住車）。
  function roadWeight() {
    const z = map ? map.getZoom() : 13;
    return z < 12 ? 1.5 : z < 14 ? 2.2 : 3;
  }
  // 車點半徑依 zoom 微幅放大（拉近更清楚）。
  function zoomBump() {
    const z = map ? map.getZoom() : 13;
    return z >= 15 ? 2 : z >= 13 ? 1 : 0;
  }

  // ⑥ 回報目前 zoom + 可視範圍（節流）
  function reportView() {
    if (!onViewChange || !map) return;
    const b = map.getBounds();
    onViewChange({
      zoom: map.getZoom(),
      bounds: { s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast() },
    });
  }
  function scheduleReportView() {
    clearTimeout(_viewTimer);
    _viewTimer = setTimeout(reportView, 250);
  }
  function setViewReporter(cb) { onViewChange = cb; reportView(); }

  // ---- 車流監測器（街景丟人式拖放）----
  function setDetectorReporter(cb) { onDetectorRequest = cb; }

  // icon 大小隨 zoom 美觀縮放
  function detIconSize() {
    const z = map ? map.getZoom() : 13;
    return z >= 15 ? 36 : z >= 13 ? 28 : z >= 11 ? 22 : 18;
  }

  function detIcon(registered) {
    const s = detIconSize();
    return L.divIcon({
      className: "det-divicon",
      html: `<div class="det-pin ${registered ? "reg" : "staged"}" style="font-size:${s}px">`
        + `<i class="ti ti-device-cctv" aria-hidden="true"></i></div>`,
      iconSize: [s, s], iconAnchor: [s / 2, s / 2],
    });
  }

  function makeDetMarker(lat, lng, label, registered) {
    const m = L.marker([lat, lng], { icon: detIcon(registered), keyboard: false })
      .addTo(detectorLayer);
    m.bindTooltip("📍 " + (label || "監測器"), { direction: "top", offset: [0, -6] });
    m._det = { lat, lng, label, registered };
    detMarkers.push(m);
    return m;
  }

  function rescaleDetectors() {
    detMarkers.forEach((m) => m.setIcon(detIcon(m._det.registered)));
  }

  // app 收到後端吸附成功的點 → 暫存 + 畫（尚未套用＝amber）
  function addStagedDetector(lat, lng, label) {
    stagedDetectors.push({ lat, lng });
    makeDetMarker(lat, lng, label, false);
  }

  function getDetectors() { return stagedDetectors.map((d) => ({ lat: d.lat, lng: d.lng })); }
  function detectorCount() { return stagedDetectors.length; }

  function clearDetectors() {
    stagedDetectors = [];
    detMarkers = [];
    if (detectorLayer) detectorLayer.clearLayers();
  }

  // init payload 帶回「已註冊（吸附後）」的監測器 → 重畫並同步暫存清單（紫＝已套用）
  function renderRegisteredDetectors(list) {
    if (!detectorLayer) return;
    detectorLayer.clearLayers();
    detMarkers = [];
    stagedDetectors = (list || []).map((d) => ({ lat: d.lat, lng: d.lng }));
    (list || []).forEach((d) => makeDetMarker(d.lat, d.lng, d.label, true));
  }

  // 拖曳時把所有道路打亮 → 提示「可以放在這些路上」（像街景把有覆蓋的街道變藍）；放開還原
  function highlightRoadsForPlacement(on) {
    if (on) {
      const hl = { color: "#5BE0FF", weight: Math.max(3, roadWeight() + 1.5), opacity: 1 };
      Object.values(roadById).forEach((l) => l.setStyle(hl));
      Object.values(flowOverlay).forEach((l) => l.setStyle(hl));
    } else if (lastRoads.length) {
      updateRoads(lastRoads);   // 還原（含即時壅塞上色）
    } else {
      Object.values(roadById).forEach((l) => l.setStyle(BASE_ROAD));
    }
  }

  function endPlacing() {
    if (!placing) return;
    placing = false;
    highlightRoadsForPlacement(false);
    const c = map && map.getContainer();
    if (c) c.classList.remove("placing-detector");
  }

  // 把左側面板的相機 icon 設成可拖放的「pegman」；拖到地圖路上放開 → 吸附放置
  function setupDetectorDrag(pegmanEl) {
    if (!pegmanEl || !map) return;
    const cont = map.getContainer();
    pegmanEl.addEventListener("dragstart", (e) => {
      placing = true;
      try { e.dataTransfer.setData("text/plain", "detector"); e.dataTransfer.effectAllowed = "copy"; } catch (_) {}
      highlightRoadsForPlacement(true);
      cont.classList.add("placing-detector");
    });
    pegmanEl.addEventListener("dragend", endPlacing);
    cont.addEventListener("dragover", (e) => {
      if (!placing) return;
      e.preventDefault();
      try { e.dataTransfer.dropEffect = "copy"; } catch (_) {}
    });
    cont.addEventListener("drop", (e) => {
      if (!placing) return;
      e.preventDefault();
      const ll = map.mouseEventToLatLng(e);   // 放開處的經緯度 → 後端吸附到最近路段
      if (onDetectorRequest) onDetectorRequest(ll.lat, ll.lng);
      endPlacing();
    });
  }

  function setInit(data) {
    if (townLayer) map.removeLayer(townLayer);
    if (roadLayer) map.removeLayer(roadLayer);
    if (signalLayer) { map.removeLayer(signalLayer); signalLayer = null; }
    Object.values(agentMarkers).forEach((m) => map.removeLayer(m));
    Object.values(flowOverlay).forEach((l) => map.removeLayer(l));
    clearAgentPath();
    agentMarkers = {};
    flowOverlay = {};
    roadById = {};
    signalBars = [];

    townLayer = L.geoJSON(data.towns_geojson, {
      style: { color: "#4a5a72", weight: 1, fillColor: "#16202e", fillOpacity: 0.25 },
    }).addTo(map);

    roadLayer = L.geoJSON(data.roads_geojson, {
      style: BASE_ROAD,
      onEachFeature: (feature, layer) => {
        const id = feature.properties && feature.properties.road_id;
        if (id) roadById[id] = layer;
      },
    }).addTo(map);

    if (stadiumMarker) map.removeLayer(stadiumMarker);
    const destName = (data.scenario && data.scenario.name) || "目的地";
    stadiumMarker = L.circleMarker([data.stadium.lat, data.stadium.lng], {
      radius: 9, color: "#fff", weight: 2, fillColor: "#ff3b3b", fillOpacity: 1,
    }).addTo(map).bindPopup(`${destName}（目的地）`);

    setSignals(data.signals);
    renderRegisteredDetectors(data.detectors);

    try { map.invalidateSize(); map.fitBounds(townLayer.getBounds().pad(0.05)); } catch (e) {}
  }

  // ---- 號誌：每路口兩條相位軸短桿 ----
  function setSignals(cfg) {
    if (!cfg || !cfg.signals || !cfg.signals.length) { signalCfg = null; return; }
    signalCfg = { cycle_s: cfg.cycle_s, yellow_s: cfg.yellow_s };
    signalLayer = L.layerGroup();
    signalBars = [];
    cfg.signals.forEach((s) => {
      if (!s.two) return;
      const e0 = barEndpoints(s.lat, s.lng, s.ax, SIGNAL_BAR_M);
      const e1 = barEndpoints(s.lat, s.lng, s.ax + 90, SIGNAL_BAR_M);
      const opt = { renderer: signalRenderer, weight: 3, opacity: 0.95, lineCap: "round" };
      const g0 = L.polyline(e0, { ...opt, color: SIG_RED });
      const g1 = L.polyline(e1, { ...opt, color: SIG_RED });
      signalLayer.addLayer(g0); signalLayer.addLayer(g1);
      signalBars.push({ g0, g1, off: s.off });
    });
    updateSignalPhase(lastElapsedS);
    refreshSignalVisibility();
  }

  function barEndpoints(lat, lng, axisDeg, Lm) {
    const th = (axisDeg * Math.PI) / 180;
    const dym = Lm * Math.sin(th), dxm = Lm * Math.cos(th);
    const dlat = dym / 111320;
    const dlng = dxm / (111320 * Math.cos((lat * Math.PI) / 180));
    return [[lat - dlat, lng - dlng], [lat + dlat, lng + dlng]];
  }

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

  // agent 狀態（優先序：抵達 > error > 等紅燈 > 移動中）。
  function agentState(a) {
    if (a.route_status === "arrived") return "arrived";
    if (a.route_status === "error") return "error";
    if (a.waiting_at_signal) return "waiting";
    return "moving";
  }

  // 把座標相同的 agent 排成整齊的方格列（排排站），避免疊成一點。只動顯示座標，真實資料不變。
  // 後端已把停在節點的車沿馬路排隊；這裡只是後備，處理少數仍落在同一點的車（不再排成圓圈）。
  function spreadPositions(agents) {
    const groups = {};
    agents.forEach((a) => {
      const key = a.lat.toFixed(5) + "," + a.lng.toFixed(5);
      (groups[key] = groups[key] || []).push(a);
    });
    const SP = 0.00008; // 約 9 公尺：每台間距
    const pos = {};
    Object.values(groups).forEach((list) => {
      if (list.length === 1) {
        pos[list[0].agent_id] = [list[0].lat, list[0].lng];
        return;
      }
      list.sort((p, q) => String(p.agent_id).localeCompare(String(q.agent_id))); // 確定性、不跳動
      const perRow = Math.max(1, Math.ceil(Math.sqrt(list.length)));   // 排成近正方的列陣
      list.forEach((a, i) => {
        const r = Math.floor(i / perRow), c = i % perRow;
        pos[a.agent_id] = [a.lat - SP * r, a.lng + SP * c];
      });
    });
    return pos;
  }

  function updateAgents(agents) {
    lastAgents = agents;
    const eventAgents = agents.filter((a) => a.role !== "ambient");
    const ambientAgents = agents.filter((a) => a.role === "ambient");
    const seen = new Set();
    const pos = spreadPositions(ambientOn ? agents : eventAgents);
    eventAgents.forEach((a) => {
      seen.add(a.agent_id);
      upsertAgentDot(a, pos[a.agent_id] || [a.lat, a.lng], agentState(a));
    });
    if (ambientOn) {
      ambientAgents.forEach((a) => {
        seen.add(a.agent_id);
        upsertAmbientDot(a, pos[a.agent_id] || [a.lat, a.lng]);
      });
    }
    Object.keys(agentMarkers).forEach((id) => {
      if (!seen.has(id)) { map.removeLayer(agentMarkers[id]); delete agentMarkers[id]; }
    });
  }

  function toggleAmbient(on) {
    ambientOn = on;
    if (lastAgents.length) updateAgents(lastAgents);
  }

  // 事件車：乾淨彩色圓點。車種用大小區分（汽車大、機車小），狀態用顏色，細暗描邊提升對比。
  function upsertAgentDot(a, ll, state) {
    const radius = (a.vehicle_type === "機車" ? 4 : 6) + zoomBump();
    let m = agentMarkers[a.agent_id];
    if (!m) {
      m = L.circleMarker(ll, {
        renderer: agentRenderer, radius, color: "#0b0f16", weight: 1.5,
        fillColor: STATE_COLOR[state], fillOpacity: 0.95,
      }).addTo(map);
      m.on("click", () => onAgentSelect && onAgentSelect(m._agentData));
      agentMarkers[a.agent_id] = m;
    } else {
      m.setLatLng(ll);
      m.setStyle({ fillColor: STATE_COLOR[state], radius });
    }
    m._agentData = a;
  }

  // 背景常態車流：低調灰小點（不可點選，純表現路網基礎負載）。
  function upsertAmbientDot(a, ll) {
    const radius = 3 + zoomBump();
    let m = agentMarkers[a.agent_id];
    if (!m) {
      m = L.circleMarker(ll, {
        renderer: agentRenderer, radius, stroke: false,
        fillColor: AMBIENT_COLOR, fillOpacity: 0.7, interactive: false,
      }).addTo(map);
      agentMarkers[a.agent_id] = m;
    } else {
      m.setLatLng(ll);
      m.setStyle({ radius });
    }
  }

  function updateRoads(roads) {
    lastRoads = roads;
    Object.values(roadById).forEach((l) => l.setStyle(BASE_ROAD));
    const w = roadWeight();
    const seen = new Set();
    roads.forEach((r) => {
      const layer = roadById[r.road_id];
      if (layer) {
        layer.setStyle({ color: r.color, weight: w, opacity: 0.85 });
      } else if (r.coords && r.coords.length > 1) {
        seen.add(r.road_id);
        const latlngs = r.coords.map((c) => [c[1], c[0]]);
        let ov = flowOverlay[r.road_id];
        if (!ov) {
          ov = L.polyline(latlngs, { color: r.color, weight: w, opacity: 0.85 }).addTo(map);
          flowOverlay[r.road_id] = ov;
        } else {
          ov.setLatLngs(latlngs);
          ov.setStyle({ color: r.color, weight: w, opacity: 0.85 });
        }
      }
    });
    Object.keys(flowOverlay).forEach((id) => {
      if (!seen.has(id)) { map.removeLayer(flowOverlay[id]); delete flowOverlay[id]; }
    });
  }

  // ---- 被選取 agent 的整趟行走軌跡（點 agent 時畫；進場藍實線、散場粉紅虛線）----
  function clearAgentPath() {
    if (agentPathLayer) { map.removeLayer(agentPathLayer); agentPathLayer = null; }
  }

  function drawAgentPath(ingress, egress) {
    clearAgentPath();
    if (!map) return;
    const grp = L.layerGroup();
    if (ingress && ingress.length > 1) {
      L.polyline(ingress, { color: "#3FB6FF", weight: 4, opacity: 0.9 }).addTo(grp);
    }
    if (egress && egress.length > 1) {
      L.polyline(egress, { color: "#FF7AC6", weight: 4, opacity: 0.9, dashArray: "6 5" }).addTo(grp);
    }
    const start = (ingress && ingress[0]) || (egress && egress[0]);
    if (start) {
      L.circleMarker(start, { radius: 5, color: "#fff", weight: 1.5, fillColor: "#2FD17A", fillOpacity: 1 })
        .bindTooltip("出發地", { direction: "top" }).addTo(grp);
    }
    const endArr = (egress && egress.length) ? egress : ingress;
    const end = endArr && endArr[endArr.length - 1];
    if (end) {
      L.circleMarker(end, { radius: 5, color: "#fff", weight: 1.5, fillColor: "#ff3b3b", fillOpacity: 1 })
        .bindTooltip(egress && egress.length ? "返家終點" : "目前位置／目的地", { direction: "top" }).addTo(grp);
    }
    grp.addTo(map);
    agentPathLayer = grp;
  }

  return { init, setInit, updateAgents, updateRoads, updateSignalPhase, toggleSignals, toggleAmbient,
           setViewReporter, resize, setDetectorReporter, setupDetectorDrag, addStagedDetector,
           getDetectors, detectorCount, clearDetectors, drawAgentPath, clearAgentPath };
})();
