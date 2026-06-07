/* ================================================================
   map.js — Leaflet 地圖控制器
   行政區界、道路（依壅塞上色）、車輛 agent、目的地球場標記。
   ================================================================ */
const TrafficMap = (() => {
  let map = null;
  let townLayer = null;
  let roadLayer = null;
  let roadById = {};          // road_id → Leaflet polyline（用於即時上色）
  let agentMarkers = {};      // agent_id → marker
  let stadiumMarker = null;
  let onAgentSelect = null;

  const BASE_ROAD = { color: "#3a4658", weight: 1.2, opacity: 0.55 };

  function init(onSelect) {
    onAgentSelect = onSelect;
    map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([23.06, 120.23], 12);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution: "&copy; OpenStreetMap &copy; CARTO",
      maxZoom: 19,
    }).addTo(map);
  }

  function setInit(data) {
    // 清掉舊圖層（reset 時會重送 init）
    if (townLayer) map.removeLayer(townLayer);
    if (roadLayer) map.removeLayer(roadLayer);
    Object.values(agentMarkers).forEach((m) => map.removeLayer(m));
    agentMarkers = {};
    roadById = {};

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

    try { map.fitBounds(townLayer.getBounds().pad(0.05)); } catch (e) {}
  }

  function colorFor(status, congestion) {
    if (status === "arrived") return "#00c853";
    if (status === "error") return "#7a8699";
    if (congestion >= 0.7) return "#d50000";
    if (congestion >= 0.4) return "#ff6d00";
    return "#3fb6ff";
  }

  function updateAgents(agents) {
    const seen = new Set();
    agents.forEach((a) => {
      seen.add(a.agent_id);
      const color = colorFor(a.route_status, a.congestion_proxy);
      const radius = a.vehicle_type === "機車" ? 4 : 6;
      let m = agentMarkers[a.agent_id];
      if (!m) {
        m = L.circleMarker([a.lat, a.lng], {
          radius, color: "#0b0f16", weight: 1, fillColor: color, fillOpacity: 0.95,
        }).addTo(map);
        // 用 m._agentData（每步更新）而非閉包捕捉的初始 a，確保點擊看到最新一步資料（含 trip_summary）
        m.on("click", () => onAgentSelect && onAgentSelect(m._agentData));
        agentMarkers[a.agent_id] = m;
      } else {
        m.setLatLng([a.lat, a.lng]);
        m.setStyle({ fillColor: color, radius });
      }
      m._agentData = a;
    });
    // 移除已不存在的（reset / set_agents）
    Object.keys(agentMarkers).forEach((id) => {
      if (!seen.has(id)) { map.removeLayer(agentMarkers[id]); delete agentMarkers[id]; }
    });
  }

  function updateRoads(roads) {
    // 先把所有主要道路還原底色，再對有流量的道路上色
    Object.values(roadById).forEach((l) => l.setStyle(BASE_ROAD));
    roads.forEach((r) => {
      const layer = roadById[r.road_id];
      if (layer) layer.setStyle({ color: r.color, weight: 4, opacity: 0.95 });
    });
  }

  return { init, setInit, updateAgents, updateRoads };
})();
