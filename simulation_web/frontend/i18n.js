/*
   i18n.js — 前端 UI 介面文字「中→英」字串表(供論文截圖)。
   ── 設計(只動前端顯示,不碰後端內容) ──
   - DICT 是 zh→en 對照表;**只翻前端自寫的 UI chrome**(按鈕/標題/圖例/控制項/前端 toast)。
   - 後端送來的動態內容(status 訊息、決策理由、persona、車種值、場景名…)一律照實顯示,**不在此表**。
   - 靜態 index.html:DOMContentLoaded 後自動「走訪文字節點 + 指定屬性」查表置換;
     **只改文字節點、不碰元素**→ <i> 圖示自然保留(R17),且 index.html 零改動。
   - 動態 JS 字串:呼叫 `T('中文')` 查表(LANG='en' 回英文、否則回原中文)。
   - 可逆:原始碼維持中文;設 `window.LANG='zh'` 後重整即回中文(走訪變 no-op)。
   - 本檔必須在 map.js/charts.js/simulation.js/app.js **之前**載入(R16:那些檔頂層用到 T())。
*/
(function () {
  "use strict";

  // 預設英文(論文截圖);改成 'zh' 即回中文。
  var LANG = (typeof window !== "undefined" && window.LANG) || "en";

  // ── zh → en 對照表(僅前端 UI 文字)──
  var EN = {
    // 頂列 / 品牌
    "LLM-abm 互動式交通模擬平台": "LLM-abm Interactive Traffic Simulation Platform",
    "LLM 驅動的 Agent-Based 交通數位分身 · 特殊活動交通衝擊分析 · 臺南亞太棒球場":
      "LLM-driven Agent-Based Traffic Digital Twin · Special-Event Impact Analysis · Tainan Asia-Pacific Baseball Stadium",
    "模擬播放控制": "Simulation playback controls",
    "開始 / 繼續": "Start / Resume",
    "開始": "Start",
    "暫停": "Pause",
    "單步": "Step",
    "重設": "Reset",
    "待命": "Standby",
    "連線中…": "Connecting…",

    // 活動階段
    "活動階段": "Event Phase",
    "進場": "Ingress",
    "散場": "Egress",
    "宣告散場開始": "Declare Egress Start",
    "比賽結束後宣告散場 → 車輛陸續離場返家。":
      "Declare egress after the game ends → vehicles leave for home in waves.",

    // 模擬設定
    "模擬設定": "Simulation Settings",
    "場景（圖層）": "Scenario (Layer)",
    "上傳場景": "Upload Scenario",
    "事件車數量": "Event Vehicles",
    "背景常態車流": "Background Traffic",
    "常態背景車流（重力 OD）；0＝關閉。": "Steady background traffic (gravity OD); 0 = off.",
    "週期數": "Cycles",
    "每週期分鐘": "Minutes per Cycle",
    "套用設定": "Apply Settings",
    "調整參數後套用（進行中需先重設）。": "Apply after adjusting (reset first if running).",

    // 進出場時間型態
    "進出場時間型態": "Arrival / Departure Timing",
    "進場出發型態": "Ingress Departure Pattern",
    "均勻分散": "Uniform",
    "早到偏多": "Early-heavy",
    "接近開賽尖峰": "Peak near game start",
    "進場出發視窗（分鐘）": "Ingress Departure Window (min)",
    "事件車在此視窗內陸續出發；0＝同時出發。":
      "Event vehicles depart within this window; 0 = all at once.",
    "散場離場型態": "Egress Departure Pattern",
    "一窩蜂": "All at once",
    "拖長疏散": "Prolonged dispersal",
    "散場離場視窗（分鐘）": "Egress Departure Window (min)",
    "散場目的地": "Egress Destination",
    "回居住地": "To Residence",
    "回出發地": "To Origin",
    "跨旅次記憶（進場經驗影響散場）": "Cross-trip Memory (ingress affects egress)",
    "開啟": "On",
    "關閉": "Off",
    "改後按上方「套用設定」生效；散場開始時間用「活動階段」的宣告散場。開／關跨旅次記憶可同 seed 比較散場路徑差異。":
      "Click \"Apply Settings\" above to take effect; start egress via \"Event Phase\". Toggle cross-trip memory to compare egress routes under the same seed.",

    // 車流監測器
    "車流監測器": "Traffic Detectors",
    "已放置 0 隻監測器": "0 detectors placed",
    "按住拖曳到地圖的路上放開即放置": "Drag and drop onto a road on the map to place",
    "拖相機到地圖路上放開即放置（自動吸附最近道路）；放好後按「套用設定」生效。":
      "Drag the camera onto a road and drop (auto-snaps to nearest road); click \"Apply Settings\" to take effect.",
    "清除全部監測器": "Clear All Detectors",

    // 決策核心
    "決策核心": "Decision Core",
    "規則式": "Rule-based",
    "demo 預設 LLM；規則式為內部 fallback / paper baseline":
      "Demo defaults to LLM; rule-based is the internal fallback / paper baseline",
    "LLM：依人格與感知決策（預設）；規則式為 fallback／對照基線。":
      "LLM: decides from persona & perception (default); rule-based is the fallback / baseline.",
    "LLM 模型": "LLM Model",
    "（整套 LLM 共用）": "(shared across all)",
    "重新生成人物": "Regenerate Personas",
    "重生 persona 原型池。": "Regenerate the persona prototype pool.",

    // Agent 檢視
    "Agent 檢視": "Agent Inspector",
    "點地圖上的車輛查看狀態。": "Click a vehicle on the map to inspect it.",
    "尚未選取。": "Nothing selected.",

    // gutter 提示
    "拖曳調整寬度，雙擊還原": "Drag to resize width; double-click to reset",
    "拖曳調整高度，雙擊還原": "Drag to resize height; double-click to reset",

    // KPI
    "週期": "Cycle",
    "已歷時": "Elapsed",
    "0 分": "0 min",
    "事件已抵達": "Event Arrived",
    "已返家": "Returned Home",
    "未出發": "Not Departed",
    "背景車": "Background",
    "壅塞路段": "Congested Roads",
    "平均壅塞": "Avg. Congestion",

    // 圖例
    "道路壅塞": "Road Congestion",
    "順暢": "Free-flow",
    "輕度": "Light",
    "壅塞": "Congested",
    "嚴重": "Severe",
    "事件車狀態": "Event Vehicle Status",
    "移動中": "Moving",
    "等紅燈": "At Red Light",
    "已抵達": "Arrived",
    "目的地（球場）": "Destination (Stadium)",
    "放大到街區層級才會顯示號誌": "Signals appear when zoomed to block level",
    "號誌": "Signals",
    "顯示/隱藏背景常態車流": "Show/hide background traffic",
    "背景車流": "Background Traffic",
    "顯示/隱藏球場抵達圈（事件車終點分散於此圈內的停車節點）":
      "Show/hide the stadium arrival circle (event destinations spread over parking nodes within it)",
    "抵達圈": "Arrival Circle",

    // 分頁
    "即時": "Live",
    "分析": "Analysis",
    "對話": "Chat",
    "日誌": "Log",
    "收合 / 展開": "Collapse / Expand",

    // 即時圖表
    "壅塞趨勢": "Congestion Trend",
    "抵達進度": "Arrival Progress",
    "行為模式分佈（累積）": "Behavior-mode Distribution (cumulative)",

    // 分析層
    "① 事件層（只算前往球場的事件車）": "① Event Layer (event vehicles to the stadium only)",
    "抵達曲線（累積 / 每步抵達率）": "Arrival Curve (cumulative / per-step rate)",
    "旅行時間分布（分鐘）": "Travel-time Distribution (min)",
    "出發地分布（Top，實際 vs 重力期望）": "Origin Distribution (top: actual vs. gravity-expected)",
    "② 路網層（事件車＋背景車，交通局視角）":
      "② Network Layer (event + background, transport-authority view)",
    "路網車流量隨時間（事件 vs 背景，堆疊）":
      "Network Volume over Time (event vs. background, stacked)",
    "Top 瓶頸路段（整趟尖峰 V/C 與服務水準）": "Top Bottleneck Links (peak V/C & level of service)",
    "③ 散場層（宣告散場後才有資料）": "③ Egress Layer (data after egress is declared)",
    "疏散曲線（累積返家 / 每步離場）": "Evacuation Curve (cumulative home / per-step departures)",
    "散場旅行時間分布（分鐘）": "Egress Travel-time Distribution (min)",
    "返家地分布（Top）": "Home-destination Distribution (top)",
    "④ 車流監測器（放置在路上的計數器）": "④ Traffic Detectors (counters placed on roads)",
    "顯示流量類型": "Flow type",
    "總車流量": "Total volume",
    "汽車": "Car",
    "機車": "Motorcycle",
    "car": "Car",
    "motorcycle": "Motorcycle",
    "路況": "Traffic",
    "任務": "Task",
    "人格": "Persona",
    "事件車": "Event",
    "監測器流量隨時間（每步通過數）": "Detector Volume over Time (passes per step)",
    "⑤ 匯出 GIS 圖層（給交通局 QGIS/ArcGIS 分析）":
      "⑤ Export GIS Layers (for transport-authority QGIS/ArcGIS analysis)",
    "道路服務水準 LOS": "Road Level of Service (LOS)",
    "車流量": "Flow Volume",
    "壅塞程度": "Congestion Level",
    "監測器點位": "Detector Locations",
    "全部圖層": "All Layers",
    "下載 Shapefile": "Download Shapefile",
    "下載分析數據 CSV": "Download Analysis CSV",
    "⑥ 匯出驗證 CSV（對比真實監視器）": "⑥ Export Validation CSV (vs. real cameras)",
    "週末 weekend（14:00 起）": "Weekend (from 14:00)",
    "平日 weekday（16:30 起）": "Weekday (from 16:30)",
    "下載驗證 CSV（事件車）": "Download Validation CSV (event vehicles)",
    "模擬完成後在此顯示交通分析。": "Traffic analysis appears here after the run completes.",

    // 對話 / 介入
    "問（唯讀）": "Ask (read-only)",
    "介入": "Intervene",
    "暫停時詢問當前路況（唯讀）。": "Ask about current conditions while paused (read-only).",
    "輸入問題後按 Enter…": "Type a question and press Enter…",
    "清除介入": "Clear Intervention",

    // 日誌
    "系統日誌": "System Log",
    "清除日誌": "Clear log",
    "決策日誌（即時）": "Decision Log (live)",
    "尚無決策（LLM 核心、壅塞觸發時才會重決）。":
      "No decisions yet (LLM core re-decides only when congestion triggers).",
    "LLM 壅塞觸發重決的車與原因 + 解析健康度。":
      "Vehicles & reasons re-decided on congestion + parsing health.",

    // modals
    "編輯 Prompts（即時生效，可還原）": "Edit Prompts (applies instantly, revertible)",
    // 註：「關閉」統一在前面 i18n 為 "Off"(進出場下拉可見);modal 關閉鈕 aria-label 不可見、沿用 "Off" 無妨。

    // ── 以下為 JS 動態產生的 UI 文字(由 T() 查表)──
    // map.js：底圖切換 / 色調面板 / 標記 / popup
    "暗色（CARTO Dark）": "Dark (CARTO Dark)",
    "淺色（CARTO Positron）": "Light (CARTO Positron)",
    "街道（CARTO Voyager）": "Streets (CARTO Voyager)",
    "OSM 標準": "OSM Standard",
    "衛星影像（Esri）": "Satellite (Esri)",
    "地圖色調": "Map Tint",
    "亮度": "Brightness",
    "對比": "Contrast",
    "飽和": "Saturation",
    "還原": "Reset",
    "📍 監測器": "📍 Detector",
    "出發地": "Origin",
    "返家終點": "Home destination",
    "目前位置／目的地": "Current location / destination",
    "路段": "Road",

    // simulation.js：run-state pill / 決策核心 / inspector / action labels / decision-health
    "執行中": "Running",
    "已暫停": "Paused",
    "已完成": "Done",
    "處理中…": "Processing…",
    "車種": "Vehicle",
    "決策理由（選此行為模式的原因）": "Decision reason (why this mode)",
    "旅次摘要": "Trip summary",
    "尚無旅次記憶。": "No trip memory yet.",
    "尚無決策理由。": "No decision reason yet.",
    "人物背景": "Persona",
    "前往目的地": "Heading to destination",
    "改道中": "Rerouting",
    "路徑異常": "Route error",
    "規則式核心": "Rule-based core",
    "已連線": "Connected",
    "連線中斷": "Disconnected",
    "前往球場": "To the stadium",
    "目前路況如何？": "How is traffic now?",
    "哪裡最塞？": "Where is it most congested?",
    "避開東區一帶": "Avoid the East District area",
    "從善化湧入 50 台": "Send 50 vehicles in from Shanhua",

    // charts.js：dataset / 表頭 / summary / 占位
    "壅塞路段數": "Congested links",
    "累積選擇次數": "Cumulative selections",
    "累積抵達": "Cumulative arrivals",
    "每步抵達率": "Arrivals per step",
    "每步出發": "Departures per step",
    "agent 數": "Agents",
    "實際": "Actual",
    "重力期望": "Gravity-expected",
    "累積返家": "Cumulative home",
    "每步離場": "Departures per step (egress)",
    "返家數": "Home count",
    "抵達率": "Arrival rate",
    "平均旅行時間": "Avg. travel time",
    "號誌停等總次數": "Total signal stops",
    "返家率": "Home-return rate",
    "平均散場旅時": "Avg. egress travel time",
    "服務水準 LOS（平均 / 尖峰）": "LOS (mean / peak)",
    "平均 / 尖峰壅塞": "Mean / peak congestion",
    "路網負載占比（事件 / 背景）": "Network load share (event / background)",
    "監測器": "Detector",
    "上行/下行": "Up / Down",
    "汽/機·事/背": "Car/Moto · Event/Bg",
    "尖峰車流/容量": "Peak flow / capacity",
    "下載此圖 PNG": "Download this chart as PNG",
    "下載 PNG": "Download PNG",
    "清場時間（90% 返家）": "Clearance time (90% home)",
    "未達 90%": "below 90%",
    "尚未放置監測器（在左側「車流監測器」放置後按「套用設定」，再跑模擬）。":
      "No detectors placed (add them via \"Traffic Detectors\" on the left, click \"Apply Settings\", then run).",
    "尚未宣告（按「宣告散場」後產生）": "Not declared yet (appears after \"Declare Egress\")",
    "無瓶頸資料（無背景車或路網未壅塞）。": "No bottleneck data (no background traffic, or uncongested network).",

    // app.js：前端自寫的連線 / 流程訊息
    "WebSocket 已連線。": "WebSocket connected.",
    "連線中斷，2 秒後自動重連…": "Disconnected; reconnecting in 2 s…",
    "初始化完成，可開始模擬。": "Initialization complete; ready to run.",
    "交通分析已產生（分析分頁）。": "Traffic analysis ready (Analysis tab).",
    "對話回覆已接收。": "Chat reply received.",

    // simulation.js：欄位名 / inspector 標籤 / persona 標籤 / 散場按鈕 / 決策核心 / 模型 / modals
    "進場出發視窗": "Ingress departure window",
    "散場離場視窗": "Egress departure window",
    "數值": "Value",
    "散場進行中": "Egress in progress",
    "姓名": "Name",
    "行為模式": "Mode",
    "狀態": "Status",
    "起點區": "Origin district",
    "目前區": "Current district",
    "速度": "Speed",
    "距終點": "Dist. to dest.",
    "鄰近車輛": "Nearby vehicles",
    "上次重決": "Last re-decision",
    "年齡": "Age",
    "職業": "Occupation",
    "個人收入": "Personal income",
    "家戶收入": "Household income",
    "交通工具": "Vehicle",
    "居住地": "Residence",
    "態度": "Attitude",
    "習慣": "Habits",
    "決策傾向": "Decision tendency",
    "經濟取捨": "Economic tradeoffs",
    "（無可用模型）": "(no models available)",
    "無 LLM 決策日誌（確定性、不產生 LLM 決策）。":
      "no LLM decision log (deterministic; produces no LLM decisions).",
    "尚無決策（LLM 壅塞觸發時記錄，逐步累積）。":
      "No decisions yet (recorded when LLM congestion triggers; accumulates over time).",
    "RAG 知識庫": "Upload Documents",
    "上傳純文字 / markdown / csv，decision 時會檢索相關內容注入。":
      "Upload plain text / markdown / csv; relevant content is retrieved and injected at decision time.",
    "啟用 RAG": "Enable RAG",
    "加入": "Add",
    "清空知識庫": "Clear knowledge base",
    "上傳自訂場景": "Upload Custom Scenario",
    "上傳本專案格式的路網 graphml（由 build_scenario / build_roads 產生）＋選填人口 CSV。":
      "Upload a road-network graphml in this project's format (from build_scenario / build_roads) + optional population CSV.",
    "場景 key（英數）": "Scenario key (alphanumeric)",
    "顯示名稱": "Display name",
    "縣市篩選（如 高雄）": "County filter (e.g. Kaohsiung)",
    "目的地 lat / lng / 區名": "Destination lat / lng / district",
    "區名": "District",
    "路網 graphml": "Road-network graphml",
    "人口 CSV（選填）": "Population CSV (optional)",
    "上傳並註冊": "Upload & register",
    "套用": "Apply",
    "還原預設": "Reset to default",
    "(已自訂)": "(custom)",

    // app.js：ACTION_LABEL(送出指令日誌用)
    "開始模擬": "Start run",
    "切換場景": "Switch scenario",
    "設定事件車數": "Set event-vehicle count",
    "設定背景車": "Set background count",
    "設定週期數": "Set cycles",
    "設定每週期分鐘": "Set minutes per cycle"
  };

  function T(zh) {
    if (LANG !== "en") return zh;
    return Object.prototype.hasOwnProperty.call(EN, zh) ? EN[zh] : zh;
  }

  // 走訪靜態 DOM 文字節點 + 指定屬性,查表置換(只改文字、不碰元素 → 保留 <i> 圖示)。
  var I18N_ATTRS = ["title", "placeholder", "aria-label", "data-tip"];

  function translateTextNodes(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var batch = [];
    var n;
    while ((n = walker.nextNode())) batch.push(n);
    for (var i = 0; i < batch.length; i++) {
      var node = batch[i];
      var raw = node.nodeValue;
      if (!raw) continue;
      var trimmed = raw.trim();
      if (!trimmed) continue;
      if (Object.prototype.hasOwnProperty.call(EN, trimmed)) {
        node.nodeValue = raw.replace(trimmed, EN[trimmed]); // 保留前後空白
      }
    }
  }

  function translateAttributes(root) {
    for (var a = 0; a < I18N_ATTRS.length; a++) {
      var attr = I18N_ATTRS[a];
      var els = root.querySelectorAll("[" + attr + "]");
      for (var i = 0; i < els.length; i++) {
        var v = els[i].getAttribute(attr);
        if (v && Object.prototype.hasOwnProperty.call(EN, v.trim())) {
          els[i].setAttribute(attr, EN[v.trim()]);
        }
      }
    }
  }

  function translateStatic() {
    if (LANG !== "en") return;
    try {
      // 分頁標題
      var docTitle = (document.title || "").trim();
      if (Object.prototype.hasOwnProperty.call(EN, docTitle)) document.title = EN[docTitle];
      translateTextNodes(document.body);
      translateAttributes(document);
    } catch (e) {
      /* 翻譯失敗不應影響功能;靜默退回原文 */
      if (window.console) console.warn("[i18n] translateStatic failed:", e);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", translateStatic);
  } else {
    translateStatic();
  }

  // 對外:JS 動態字串用 T();需要時可手動再跑 translateStatic()。
  window.T = T;
  window.I18N = { T: T, LANG: LANG, translateStatic: translateStatic, dict: EN };
})();
