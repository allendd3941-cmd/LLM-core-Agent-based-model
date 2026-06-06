// 完整主模型：GAMA 作為 API client，外部 Python LLM server 只負責回傳 origin / active_mode。
// 注意：本檔只 import api_post_module.gaml，不修改該套件。
model TrafficABM_Tainan_LLM

// 本檔與 api_post_module.gaml 放在同一資料夾，使用相對 import 避免絕對路徑編碼問題。
import "../includes/api_post_module.gaml"

global {
    // ============================================================
    // [可調整參數區] 常用設定集中放在這裡，之後主要改這一段即可。
    // ============================================================

    // === 圖資與座標設定 ===
    // [可調整] ROADLINK shapefile 路徑。
    // ROADLINK 是 agent 可行走道路；座標為 TWD97 TM Taiwan，單位 meter。
    file shape_file_roads <- file("../data/ROADLINK.shp");

    // [可調整] TOWN_MOI shapefile 路徑。
    // TOWN_MOI 是鄉鎮市區界線；只保留 COUNTYNAME = "臺南市"。
    file shape_file_towns <- file("../data/TOWN_MOI_1140318_3826.shp");

    // [可調整] 固定終點 point 圖層。此圖層已是 EPSG:3826，agent 的 target 會固定使用這個點。
    file shape_file_destination_point <- shape_file("../data/亞太棒球場_point.shp", "EPSG:3826");

    // === 模擬時間設定 ===
    // [可調整] max_steps 控制總模擬步數；step_minutes 與 step 要保持一致。
    // 每個 GAMA cycle 代表現實 5 分鐘，36 cycle 後停止。
    int max_steps <- 36;
    int step_minutes <- 5;
    float step <- 5 #mn;

    // === API 與 agent 設定 ===
    // [可調整] enable_api_post=false 時，不呼叫 Python，只測試 GAMA 空間邏輯。
    // 若 Python server 還沒啟動，可先改 false 測試 GAMA 空間邏輯。
    bool enable_api_post <- true;

    // [可調整] 初始 agent 數量。
    int nb_agents <- 10;

    // [可調整] 固定終點 point 載入失敗時使用的 fallback 終點鄉鎮市區。
    // TODO: fallback 終點鄉鎮市區。請改成任一臺南市 TOWNNAME，例如 "東區"、"安南區"、"歸仁區"。
    string destination_town_name <- "安定區";

    // [可調整] Python response 缺少生成地或解析失敗時使用的預設行政區。
    // API 沒回覆或回覆無法解析時的 fallback 生成行政區。
    string default_origin_town <- "東區";

    // [可調整] agent 感知半徑；數值越大，nearby_agent_count 與 congestion_proxy 會涵蓋越遠。
    float default_perception_radius <- 300 #m;

    // [可調整] agent 預設移動偏好；Python 回傳 active_mode 後可覆蓋這些值。
    string default_mode_name <- "active_mode";
    // [可調整] LLM 未回傳 vehicle_type 時使用；目前只支援 "汽車" 與 "機車"。
    string default_vehicle_type <- "汽車";
    float default_desired_speed <- 40.0;
    float default_speed_car_preference <- 45.0;
    float default_speed_moto_preference <- 35.0;
    list<string> default_road_type_preference <- ["primary", "secondary", "tertiary", "residential"];
    float default_route_randomness <- 0.15;
    float default_comfort_weight <- 0.20;
    float default_time_weight <- 0.45;
    float default_distance_weight <- 0.25;
    float default_capacity_weight <- 0.10;

    // [可調整] Python API 連線設定。
    string api_host_value <- "127.0.0.1";
    int api_port_value <- 8001;
    string api_endpoint_value <- "/from-gama";
    bool api_log_response <- true;

    // [可調整] agent 感知、移動與抵達判斷。
    float arrival_distance_threshold <- 0 #m;
    float crowded_speed_factor <- 0.55;
    float missing_road_speed_cap <- 40.0;
    float crowded_road_threshold <- 0.5;

    // [可調整] 道路壅塞估計與視覺化門檻。
    int active_road_min_flow <- 1;
    float capacity_fallback_vehicle_count <- 10.0;
    float flow_weight_multiplier <- 2.0;
    int road_flow_high_threshold <- 8;
    int road_flow_medium_threshold <- 3;

    // [可調整] agent 顯示大小。
    float car_display_size <- 450.0;
    float moto_display_size <- 220.0;

    // === 輸出檔設定 ===
    // [可調整] CSV 輸出路徑。
    // CSV 輸出路徑，用來檢查 agent memory 與道路流量。
    string agent_memory_path <- "../output/agent_memory.csv";
    string road_flow_path <- "../output/road_flow.csv";

    // ============================================================
    // [固定模型狀態區] 下方通常不用調整。
    // ============================================================

    // 以 ROADLINK 範圍作為世界範圍，讓路網是主要空間參考。
    geometry shape <- envelope(shape_file_roads);
    graph road_network;

    // 每個 step 送給 Python 前重建的 payload。
    list<map> payload_agents_data <- [];

    // API 初始化狀態。
    bool init_request_sent <- false;
    bool init_response_applied <- false;
    map last_api_response <- [];

    // ROADLINK 欄位摘要，會傳給 Python 讓 LLM 知道道路資料可用欄位。
    list<string> road_schema_fields <- [
        "index", "a", "b", "length", "highway", "NAME", "highway_ty",
        "speed_car", "speed_moto", "lanes", "capacity",
        "time_car", "time_moto", "time", "wkt"
    ];

    // TOWN_MOI 欄位摘要，以 TOWNNAME 比對 "東區" 這類 origin。
    list<string> town_schema_fields <- [
        "TOWNID", "TOWNCODE", "COUNTYNAME", "TOWNNAME",
        "TOWNENG", "COUNTYID", "COUNTYCODE"
    ];

    // 先完整列出可傳給 Python 的 GAMA 內部環境變數，後續可由使用者刪減。
    // 目前 API payload 已刪減為最小代表欄位，下列 catalog 僅保留仍會送出的欄位。
    list<string> exportable_environment_variables <- [
        "environment.cycle",
        "environment.elapsed_minutes",
        "environment.agent_count",
        "environment.destination_town",
        "environment.active_road_count",
        "environment.crowded_road_count",
        "environment.average_congestion_proxy",
        "agent.agent_id",
        "agent.origin_town",
        "agent.destination_town",
        "agent.active_mode",
        "agent.vehicle_type",
        "agent.environment.current_town",
        "agent.environment.current_road_id",
        "agent.environment.route_status",
        "agent.environment.nearby_agent_count",
        "agent.environment.congestion_proxy",
        "agent.environment.distance_to_destination_m",
        "memory.cycle",
        "memory.current_town",
        "memory.current_road_id",
        "memory.active_mode",
        "memory.vehicle_type",
        "memory.route_status",
        "memory.nearby_agent_count",
        "memory.congestion_proxy",
        "memory.distance_to_destination_m"
    ];

    // 從 TOWN_MOI 篩出的臺南市鄉鎮市區清單，會傳給 Python。
    list<string> available_town_names <- [];
    // 固定終點座標；init 載入 destination_point 後設定，所有 agent 共用同一個 target。
    point fixed_destination_location <- nil;

    init {
        // === 載入 ROADLINK ===
        // 將 shapefile 每條道路轉成 road agent，保留所有必要 DBF 欄位。
        create road from: shape_file_roads with: [
            road_id::string(read("index")),
            node_a::string(read("a")),
            node_b::string(read("b")),
            road_length::float(read("length")),
            highway::string(read("highway")),
            road_name::string(read("NAME")),
            highway_type::string(read("highway_ty")),
            speed_car::float(read("speed_car")),
            speed_moto::float(read("speed_moto")),
            lanes::float(read("lanes")),
            capacity::float(read("capacity")),
            time_car::float(read("time_car")),
            time_moto::float(read("time_moto")),
            travel_time::float(read("time")),
            wkt_text::string(read("wkt"))
        ];

        // 建立道路 graph，vehicle 的 goto 會沿 road_network 移動。
        road_network <- as_edge_graph(road);

        // === 載入 TOWN_MOI ===
        // 將鄉鎮市區界線轉為 town agent，後面只保留臺南市。
        create town from: shape_file_towns with: [
            town_id::string(read("TOWNID")),
            town_code::string(read("TOWNCODE")),
            county_name::string(read("COUNTYNAME")),
            town_name::string(read("TOWNNAME")),
            town_eng::string(read("TOWNENG")),
            county_id::string(read("COUNTYID")),
            county_code::string(read("COUNTYCODE"))
        ];

        // TOWN_MOI 的 .prj 是 GCS_TWD97[2020]，ROADLINK 是 TWD97 TM Taiwan。
        // GAMA 通常可依 .prj 轉換；若載入後不重疊，請先在 GIS 軟體把 TOWN_MOI 轉成 TWD97 TM Taiwan。
        // 目前 shape_file_towns 已改用 repo 內的 TOWN_MOI_1140318_3826.shp，因此不再呼叫 runtime CRS transform。
        write "[DIAG] TOWN_MOI uses pre-projected TWD97 TM Taiwan file; runtime CRS transform skipped.";

        // 僅保留臺南市。
        ask town where (each.county_name != "臺南市") {
            do die;
        }

        // 建立可用臺南市行政區清單。
        available_town_names <- [];
        loop area over: town {
            available_town_names << area.town_name;
        }

        create destination_point from: shape_file_destination_point;
        if (!empty(destination_point)) {
            fixed_destination_location <- one_of(destination_point).location;
            write "[DIAG] destination point loaded count = " + string(length(destination_point));
            write "[DIAG] fixed destination point = " + string(fixed_destination_location);
        } else {
            write "[WARN] No destination point loaded; fallback to destination_town road point.";
        }

        // === 初始化輸出檔 ===
        // agent_memory.csv 用來驗證每步 memory 是否正確紀錄。
        save [
            "cycle", "agent_id", "origin_town", "destination_town",
            "current_town", "current_road_id", "next_road_id",
            "active_mode", "vehicle_type", "speed_kmh", "distance_moved_m",
            "nearby_agent_count", "route_status", "api_status", "warning"
        ] to: agent_memory_path format: "csv" rewrite: true;

        // road_flow.csv 用來驗證道路 flow 與 congestion proxy。
        save [
            "cycle", "road_id", "road_name", "highway", "highway_type",
            "current_flow", "capacity", "congestion_proxy"
        ] to: road_flow_path format: "csv" rewrite: true;

        // === 建立 API client ===
        // llm_api_client 繼承 api_post_module.gaml 裡的 api_poster，不修改套件。
        create llm_api_client number: 1 {
            api_host <- api_host_value;
            api_port <- api_port_value;
            api_endpoint <- api_endpoint_value;
            log_response <- api_log_response;
        }

        // === 建立 agent ===
        // 先用 fallback 行政區放置 agent；若 Python 回傳 origin，會重新放置到 API 指定行政區道路上。
        create vehicle number: nb_agents {
            agent_id <- name;
            mode_name <- default_mode_name;
            vehicle_type <- default_vehicle_type;
            origin_town <- default_origin_town;
            destination_town <- destination_town_name;
            location <- choose_road_point_in_town(origin_town);
            target <- choose_fixed_target_point();
            route_status <- "waiting_for_api_origin";
            waiting_for_origin <- enable_api_post;
        }

        // 第一次請求 Python，要求回傳每個 agent 的 origin / active_mode。
        if (enable_api_post) {
            do send_initial_request;
        }
    }

    action send_initial_request {
        // 初始化請求：保留 cycle / vehicles，避免外部 Python 現有 schema 不相容。
        list<map> requested_agents <- [];
        ask vehicle {
            requested_agents << [
                "agent_id"::agent_id,
                "fallback_origin_town"::origin_town,
                "fixed_destination_town"::destination_town,
                "active_mode"::build_active_mode_payload(),
                "vehicle_type"::vehicle_type
            ];
        }

        map init_payload <- [
            "request_type"::"init_agents",
            "model"::"TrafficABM_Tainan_LLM",
            "model_name"::"TrafficABM_Tainan_LLM",
            "cycle"::cycle,
            "vehicles"::length(vehicle),
            "step_minutes"::step_minutes,
            "max_steps"::max_steps,
            "available_towns"::available_town_names,
            "road_schema"::road_schema_fields,
            "town_schema"::town_schema_fields,
            "requested_agents"::requested_agents,
            "environment_variable_catalog"::exportable_environment_variables,
            "crs_note"::"ROADLINK=TWD97 TM Taiwan meter; TOWN_MOI uses pre-projected TWD97 TM Taiwan file."
        ];

        ask one_of(llm_api_client) {
            do post(init_payload);
        }
        init_request_sent <- true;
    }

    map build_overall_environment_payload {
        // step request 的整體環境摘要，只保留代表模擬時間、agent 規模與壅塞狀態的欄位。
        int active_road_count <- 0;
        int crowded_road_count <- 0;
        float congestion_sum <- 0.0;

        loop rd over: road {
            if (rd.current_flow >= active_road_min_flow) {
                active_road_count <- active_road_count + 1;
            }
            if (rd.congestion_proxy >= crowded_road_threshold) {
                crowded_road_count <- crowded_road_count + 1;
            }
            congestion_sum <- congestion_sum + rd.congestion_proxy;
        }

        return [
            "cycle"::cycle,
            "elapsed_minutes"::cycle * step_minutes,
            "agent_count"::length(vehicle),
            "destination_town"::destination_town_name,
            "active_road_count"::active_road_count,
            "crowded_road_count"::crowded_road_count,
            "average_congestion_proxy"::(active_road_count > 0 ? congestion_sum / active_road_count : 0.0)
        ];
    }

    action send_step_request {
        // 每個 step 都送出 agent memory 與當前環境狀態。
        // 目前只傳送整體環境摘要與每個 agent 的精簡局部狀態，避免送出全部道路 raw flow。
        payload_agents_data <- [];
        ask vehicle {
            payload_agents_data << build_api_agent_payload();
        }

        map step_payload <- [
            "request_type"::"step_update",
            "model"::"TrafficABM_Tainan_LLM",
            "model_name"::"TrafficABM_Tainan_LLM",
            "cycle"::cycle,
            "environment"::build_overall_environment_payload(),
            "agents_status"::payload_agents_data,
            "environment_variable_catalog"::exportable_environment_variables
        ];

        ask one_of(llm_api_client) {
            do post(step_payload);
        }
    }

    string normalize_town_name(string raw_value) {
        // 從 API 回覆文字中掃描臺南市 TOWNNAME；可處理純字串 "東區" 或 JSON 文字。
        string cleaned <- string(raw_value);
        loop area over: town {
            if (cleaned contains area.town_name) {
                return area.town_name;
            }
        }
        return default_origin_town;
    }

    point choose_road_point_in_town(string requested_town_name) {
        // 在指定鄉鎮市區內找 ROADLINK，並在線段上隨機挑一個點。
        string normalized_town <- normalize_town_name(requested_town_name);
        town selected_town <- town first_with (each.town_name = normalized_town and each.county_name = "臺南市");

        if (selected_town = nil) {
            write "[WARN] Cannot find town '" + requested_town_name + "', fallback to " + default_origin_town;
            selected_town <- town first_with (each.town_name = default_origin_town and each.county_name = "臺南市");
        }

        if (selected_town != nil) {
            // line-in-polygon 用 intersects 比 overlapping 穩；overlap 對「道路完全在行政區內」可能不成立。
            list<road> roads_in_town <- road where (each.shape intersects selected_town.shape);
            if (!empty(roads_in_town)) {
                road selected_road <- one_of(roads_in_town);
                return any_location_in(selected_road);
            }

            road nearest_road <- road closest_to selected_town;
            if (nearest_road != nil) {
                write "[WARN] No intersecting ROADLINK in " + selected_town.town_name + "; use nearest road instead.";
                return any_location_in(nearest_road);
            }

            if (!empty(road)) {
                write "[WARN] Spatial query failed for " + selected_town.town_name + "; use random ROADLINK because road count = " + string(length(road));
                return any_location_in(one_of(road));
            }

            write "[ERROR] No ROADLINK agents loaded. Check shape_file_roads path and ROADLINK shp/dbf/shx files.";
            return selected_town.location;
        }

        // 最後防呆：若鄉鎮界載入失敗，隨機放在任一 ROADLINK。
        if (!empty(road)) {
            return any_location_in(one_of(road));
        }

        write "[ERROR] No ROADLINK agents loaded, cannot place agent on road.";
        return {0, 0};
    }

    string extract_origin_from_body(string body) {
        // 支援純字串、JSON map、OD converter row；失敗時用 town name 掃描。
        string origin <- "";
        try {
            unknown parsed <- from_json(body);
            if (parsed is map) {
                map reply <- map(parsed);
                if (reply.keys contains "origin") {
                    origin <- string(reply["origin"]);
                } else if (reply.keys contains "residential_location") {
                    origin <- string(reply["residential_location"]);
                } else if (reply.keys contains "origin_town") {
                    origin <- string(reply["origin_town"]);
                } else if (reply.keys contains "origin_taz") {
                    origin <- string(reply["origin_taz"]);
                } else if (reply.keys contains "出發點") {
                    origin <- string(reply["出發點"]);
                } else if (reply.keys contains "起點") {
                    origin <- string(reply["起點"]);
                }
            } else {
                origin <- string(parsed);
            }
        } catch {
            origin <- body;
        }

        return normalize_town_name(origin);
    }

    map extract_active_mode_from_body(string body) {
        // 支援 active_mode map、mode 字串，或 OD converter 回傳的 mode 欄位。
        map mode_payload <- [];
        try {
            unknown parsed <- from_json(body);
            if (parsed is map) {
                map reply <- map(parsed);
                if (reply.keys contains "active_mode") {
                    if (reply["active_mode"] is map) {
                        mode_payload <- map(reply["active_mode"]);
                    } else {
                        mode_payload <- ["mode_name"::string(reply["active_mode"])];
                    }
                } else if (reply.keys contains "active mode") {
                    mode_payload <- ["mode_name"::string(reply["active mode"])];
                } else if (reply.keys contains "mode") {
                    mode_payload <- ["mode_name"::string(reply["mode"])];
                }
            }
        } catch {
            mode_payload <- [];
        }
        return mode_payload;
    }

    string normalize_vehicle_type(string raw_value) {
        // vehicle_type 獨立於 active_mode，只接受 LLM 回傳的 "汽車" / "機車"。
        string cleaned <- string(raw_value);
        if (cleaned contains "機車") {
            return "機車";
        }
        if (cleaned contains "汽車") {
            return "汽車";
        }
        if (default_vehicle_type contains "機車") {
            return "機車";
        }
        return "汽車";
    }

    string extract_vehicle_type_from_body(string body) {
        // 支援 top-level vehicle_type；若 response 沒有此欄位，回傳空字串表示不更新。
        string response_vehicle_type <- "";
        try {
            unknown parsed <- from_json(body);
            if (parsed is map) {
                map reply <- map(parsed);
                if (reply.keys contains "vehicle_type") {
                    response_vehicle_type <- string(reply["vehicle_type"]);
                }
            }
        } catch {
            response_vehicle_type <- "";
        }
        return response_vehicle_type = "" ? "" : normalize_vehicle_type(response_vehicle_type);
    }

    action apply_init_response(string body) {
        // 第一次回覆：若有 agents / initial_vehicles list，逐一套用；否則將同一 origin 套用到所有 agent。
        bool applied_by_agent_list <- false;

        try {
            unknown parsed <- from_json(body);
            if (parsed is map) {
                map reply <- map(parsed);
                list<map> rows <- [];

                if (reply.keys contains "agents") {
                    rows <- list<map>(reply["agents"]);
                } else if (reply.keys contains "initial_vehicles") {
                    rows <- list<map>(reply["initial_vehicles"]);
                }

                if (!empty(rows)) {
                    applied_by_agent_list <- true;
                    loop row over: rows {
                        string response_agent_id <- (row.keys contains "agent_id") ? string(row["agent_id"]) : "";
                        string response_agent_name <- (row.keys contains "agent name") ? string(row["agent name"]) : "";
                        response_agent_id <- (response_agent_id = "" and response_agent_name != "") ? response_agent_name : response_agent_id;
                        string response_origin <- (row.keys contains "origin") ? string(row["origin"]) : "";
                        response_origin <- (response_origin = "" and (row.keys contains "residential_location")) ? string(row["residential_location"]) : response_origin;
                        response_origin <- (response_origin = "" and (row.keys contains "origin_town")) ? string(row["origin_town"]) : response_origin;
                        response_origin <- (response_origin = "" and (row.keys contains "origin_taz")) ? string(row["origin_taz"]) : response_origin;
                        response_origin <- normalize_town_name(response_origin);
                        string response_vehicle_type <- (row.keys contains "vehicle_type") ? normalize_vehicle_type(string(row["vehicle_type"])) : default_vehicle_type;

                        map response_mode <- [];
                        if (row.keys contains "active_mode") {
                            if (row["active_mode"] is map) {
                                response_mode <- map(row["active_mode"]);
                            } else {
                                response_mode <- ["mode_name"::string(row["active_mode"])];
                            }
                        } else if (row.keys contains "active mode") {
                            response_mode <- ["mode_name"::string(row["active mode"])];
                        } else if (row.keys contains "mode") {
                            response_mode <- ["mode_name"::string(row["mode"])];
                        } else if (row.keys contains "type") {
                            response_mode <- ["mode_name"::string(row["type"])];
                        }

                        vehicle target_agent <- nil;
                        if (response_agent_id != "") {
                            target_agent <- vehicle first_with (each.agent_id = response_agent_id or each.name = response_agent_id);
                        }
                        if (target_agent = nil) {
                            target_agent <- one_of(vehicle where (each.waiting_for_origin));
                        }

                        if (target_agent != nil) {
                            ask target_agent {
                                profile_agent_name <- response_agent_name;
                                do place_from_origin(response_origin, response_mode, response_vehicle_type);
                            }
                        }
                    }
                }
            }
        } catch {
            applied_by_agent_list <- false;
        }

        if (!applied_by_agent_list) {
            string response_origin <- extract_origin_from_body(body);
            map response_mode <- extract_active_mode_from_body(body);
            string response_vehicle_type <- extract_vehicle_type_from_body(body);
            if (response_vehicle_type = "") {
                response_vehicle_type <- default_vehicle_type;
            }
            ask vehicle {
                do place_from_origin(response_origin, response_mode, response_vehicle_type);
            }
        }

        init_response_applied <- true;
    }

    action apply_step_response(string body) {
        // 後續 step 若回傳 active_mode，就更新 agent mode；若未回傳則保留原設定。
        bool applied_by_agent_list <- false;

        try {
            unknown parsed <- from_json(body);
            if (parsed is map) {
                map reply <- map(parsed);
                list<map> rows <- [];
                if (reply.keys contains "agents") {
                    rows <- list<map>(reply["agents"]);
                }

                if (!empty(rows)) {
                    applied_by_agent_list <- true;
                    loop row over: rows {
                        string response_agent_id <- (row.keys contains "agent_id") ? string(row["agent_id"]) : "";
                        string response_agent_name <- (row.keys contains "agent name") ? string(row["agent name"]) : "";
                        response_agent_id <- (response_agent_id = "" and response_agent_name != "") ? response_agent_name : response_agent_id;
                        string response_vehicle_type <- (row.keys contains "vehicle_type") ? normalize_vehicle_type(string(row["vehicle_type"])) : "";

                        map response_mode <- [];
                        if (row.keys contains "active_mode") {
                            if (row["active_mode"] is map) {
                                response_mode <- map(row["active_mode"]);
                            } else {
                                response_mode <- ["mode_name"::string(row["active_mode"])];
                            }
                        } else if (row.keys contains "active mode") {
                            response_mode <- ["mode_name"::string(row["active mode"])];
                        } else if (row.keys contains "mode") {
                            response_mode <- ["mode_name"::string(row["mode"])];
                        }

                        vehicle target_agent <- nil;
                        if (response_agent_id != "") {
                            target_agent <- vehicle first_with (each.agent_id = response_agent_id or each.name = response_agent_id or each.profile_agent_name = response_agent_id);
                        }

                        if (target_agent != nil) {
                            ask target_agent {
                                if (!empty(response_mode)) {
                                    do apply_active_mode(response_mode);
                                }
                                if (response_vehicle_type != "") {
                                    do apply_vehicle_type(response_vehicle_type);
                                }
                                last_api_response_summary <- body;
                            }
                        }
                    }
                }
            }
        } catch {
            applied_by_agent_list <- false;
        }

        if (!applied_by_agent_list) {
            map response_mode <- extract_active_mode_from_body(body);
            string response_vehicle_type <- extract_vehicle_type_from_body(body);
            if (!empty(response_mode) or response_vehicle_type != "") {
                ask vehicle {
                    if (!empty(response_mode)) {
                        do apply_active_mode(response_mode);
                    }
                    if (response_vehicle_type != "") {
                        do apply_vehicle_type(response_vehicle_type);
                    }
                    last_api_response_summary <- body;
                }
            }
        }
    }

    reflex apply_api_response when: enable_api_post and length(llm_api_client) > 0 {
        // 從子類 API client 取得最新 response，再決定是 init 或 step response。
        llm_api_client client <- one_of(llm_api_client);
        if (client != nil and client.has_new_response) {
            last_api_response <- client.last_response;

            if (!init_response_applied) {
                do apply_init_response(client.last_body);
            } else {
                do apply_step_response(client.last_body);
            }

            ask client {
                do clear_latest_response;
            }
        }
    }

    reflex update_road_network_weights {
        // 依道路 flow 更新路網權重，讓壅塞路段較不容易被選。
        road_network <- road_network with_weights (road as_map (each::each.weight));
    }

    reflex send_payload_to_api when: enable_api_post and init_request_sent and cycle < max_steps {
        // 每個 step 發送一次精簡狀態；payload 保留 cycle 相容既有 Python。
        do send_step_request;
    }

    reflex record_road_flow {
        // 每步紀錄所有道路 flow，供 CSV 檢查；API payload 不再同步完整道路清單。
        ask road {
            save [
                cycle, road_id, road_name, highway, highway_type,
                current_flow, capacity, congestion_proxy
            ] to: road_flow_path format: "csv" rewrite: false;
        }
    }

    reflex stop_at_max_steps when: cycle >= max_steps {
        // 36 個 step 後暫停目前 simulation；這版 GAMA headless 使用 pause，避免 halt 編譯失敗。
        write "Simulation reached " + string(max_steps) + " steps (" + string(max_steps * step_minutes) + " minutes).";
        do pause;
    }
}

species llm_api_client parent: api_poster {
    // 這個子類只補上 request/response 狀態；api_host、api_port、api_endpoint、connect、send 都繼承自套件。
    int request_counter <- 0;
    string last_request_type <- "none";
    map last_payload <- [];
    bool has_new_response <- false;
    int response_counter <- 0;
    string last_code <- "";
    string last_body <- "";
    map last_response <- [];

    action post(map payload) {
        // 覆寫 post 只為了記錄 metadata；實際 HTTP POST 呼叫父類 api_poster 的 post，不修改套件。
        request_counter <- request_counter + 1;
        last_payload <- payload;
        last_request_type <- (payload.keys contains "request_type") ? string(payload["request_type"]) : "unknown";
        has_new_response <- false;

        invoke post(payload);
    }

    action clear_latest_response {
        // 主模型處理完 response 後清除旗標，避免同一筆回覆重複套用。
        has_new_response <- false;
    }

    reflex receive_reply {
        // 覆寫父類 receive_reply，保留 BODY 給主模型解析 origin / active_mode。
        loop while: has_more_message() {
            message mess <- fetch_message();
            map raw_contents <- map(mess.contents);

            last_code <- (raw_contents.keys contains "CODE") ? string(raw_contents["CODE"]) : "";
            last_body <- (raw_contents.keys contains "BODY") ? string(raw_contents["BODY"]) : string(mess.contents);

            response_counter <- response_counter + 1;
            last_response <- [
                "request_id"::request_counter,
                "request_type"::last_request_type,
                "received_cycle"::cycle,
                "code"::last_code,
                "body"::last_body
            ];
            has_new_response <- true;

            if (log_response) {
                write "API CODE:";
                write last_code;
                write "API BODY:";
                write last_body;
            }
        }
    }
}

species town {
    // TOWN_MOI 的臺南市鄉鎮市區 agent。
    string town_id;
    string town_code;
    string county_name;
    string town_name;
    string town_eng;
    string county_id;
    string county_code;

    aspect default {
        draw shape color: #lightgray border: #darkgray;
    }
}

species destination_point {
    aspect default {
        draw circle(300) color: #red border: #black;
    }
}

species road {
    // ROADLINK 道路 agent，保留 DBF 欄位給路網、速度限制與 payload 使用。
    string road_id;
    string node_a;
    string node_b;
    float road_length;
    string highway;
    string road_name;
    string highway_type;
    float speed_car;
    float speed_moto;
    float lanes;
    float capacity;
    float time_car;
    float time_moto;
    float travel_time;
    string wkt_text;

    int current_flow <- 0;
    float congestion_proxy <- 0.0;
    float weight <- max([shape.perimeter, 1.0]);

    map build_road_payload {
        // ROADLINK 可輸出欄位：Python 可用這些欄位判斷路段成本與偏好。
        return [
            "road_id"::road_id,
            "NAME"::road_name,
            "highway"::highway,
            "highway_ty"::highway_type,
            "length"::road_length,
            "lanes"::lanes,
            "capacity"::capacity,
            "speed_car"::speed_car,
            "speed_moto"::speed_moto,
            "time_car"::time_car,
            "time_moto"::time_moto,
            "time"::travel_time,
            "current_flow"::current_flow,
            "congestion_proxy"::congestion_proxy
        ];
    }

    reflex update_flow {
        // 用目前在此 edge 上的 vehicle 數作為 flow，並估計壅塞程度。
        current_flow <- vehicle count (road(each.current_edge) = self);
        congestion_proxy <- (capacity > 0.0) ? min([1.0, current_flow / capacity]) : min([1.0, current_flow / capacity_fallback_vehicle_count]);
        weight <- max([shape.perimeter, 1.0]) * (1.0 + current_flow * flow_weight_multiplier);
    }

    aspect default {
        rgb color_by_flow <- (current_flow > road_flow_high_threshold) ? #red : ((current_flow > road_flow_medium_threshold) ? #orange : #black);
        draw shape color: color_by_flow;
    }
}

species vehicle skills: [moving] {
    // === Agent identity ===
    string agent_id;
    string profile_agent_name <- "";

    // === active_mode 可擴充交通模式 ===
    // 之後要新增移動偏好，只要加 attribute 並同步 build_active_mode_payload。
    string mode_name <- "active_mode";
    string vehicle_type <- "汽車";
    float desired_speed <- default_desired_speed;
    float speed_car_preference <- default_speed_car_preference;
    float speed_moto_preference <- default_speed_moto_preference;
    list<string> road_type_preference <- default_road_type_preference;
    float route_randomness <- default_route_randomness;
    float comfort_weight <- default_comfort_weight;
    float time_weight <- default_time_weight;
    float distance_weight <- default_distance_weight;
    float capacity_weight <- default_capacity_weight;
    map active_mode_custom_params <- [];

    // === 旅程狀態 ===
    string origin_town <- "";
    string destination_town <- "";
    point target;
    string route_status <- "created";
    bool waiting_for_origin <- false;
    string next_road_id <- "calculating";
    point external_forced_target <- nil;
    path external_forced_path <- nil;

    // === 感知與移動狀態 ===
    float speed <- default_desired_speed #km / #h;
    float perception_radius <- default_perception_radius;
    bool is_crowded <- false;
    float distance_moved_last_step <- 0.0;
    string selected_action <- "none";

    // === API 與 memory ===
    list<map> travel_memory <- [];
    string api_status <- "not_sent";
    string last_api_response_summary <- "";
    string warning_message <- "";

    map build_active_mode_payload {
        // active_mode 統一輸出格式。
        return [
            "mode_name"::mode_name,
            "move_speed"::desired_speed,
            "speed_car"::speed_car_preference,
            "speed_moto"::speed_moto_preference,
            "road_type_preference"::road_type_preference,
            "route_randomness"::route_randomness,
            "comfort_weight"::comfort_weight,
            "time_weight"::time_weight,
            "distance_weight"::distance_weight,
            "capacity_weight"::capacity_weight,
            "custom_params"::active_mode_custom_params
        ];
    }

    action apply_active_mode(map mode_payload) {
        // 從 Python 回覆套用 active_mode；不存在欄位就保留 GAMA 預設。
        if (mode_payload = nil) {
            return;
        }

        if (mode_payload.keys contains "mode_name") {
            mode_name <- string(mode_payload["mode_name"]);
        } else if (mode_payload.keys contains "mode") {
            mode_name <- string(mode_payload["mode"]);
        }

        if (mode_payload.keys contains "move_speed") {
            desired_speed <- float(mode_payload["move_speed"]);
        }
        if (mode_payload.keys contains "speed_car") {
            speed_car_preference <- float(mode_payload["speed_car"]);
        }
        if (mode_payload.keys contains "speed_moto") {
            speed_moto_preference <- float(mode_payload["speed_moto"]);
        }
        if (mode_payload.keys contains "route_randomness") {
            route_randomness <- float(mode_payload["route_randomness"]);
        }
        if (mode_payload.keys contains "comfort_weight") {
            comfort_weight <- float(mode_payload["comfort_weight"]);
        }
        if (mode_payload.keys contains "time_weight") {
            time_weight <- float(mode_payload["time_weight"]);
        }
        if (mode_payload.keys contains "distance_weight") {
            distance_weight <- float(mode_payload["distance_weight"]);
        }
        if (mode_payload.keys contains "capacity_weight") {
            capacity_weight <- float(mode_payload["capacity_weight"]);
        }
        if (mode_payload.keys contains "custom_params") {
            active_mode_custom_params <- map(mode_payload["custom_params"]);
        }
    }

    string normalize_vehicle_type(string raw_value) {
        // vehicle 內部版：只讓車種維持在 "汽車" / "機車"。
        string cleaned <- string(raw_value);
        if (cleaned contains "機車") {
            return "機車";
        }
        if (cleaned contains "汽車") {
            return "汽車";
        }
        if (default_vehicle_type contains "機車") {
            return "機車";
        }
        return "汽車";
    }

    action apply_vehicle_type(string requested_vehicle_type) {
        if (requested_vehicle_type = "") {
            return;
        }
        vehicle_type <- normalize_vehicle_type(requested_vehicle_type);
    }

    action place_from_origin(string requested_origin, map mode_payload, string requested_vehicle_type) {
        // Python 第一次回覆 origin 後，把 agent 移到該行政區內的 ROADLINK 隨機點。
        origin_town <- normalize_town_name(requested_origin);
        location <- choose_road_point_in_town(origin_town);
        target <- choose_fixed_target_point();
        waiting_for_origin <- false;
        route_status <- "spawned_from_api";
        api_status <- "init_response_applied";
        do apply_vehicle_type(requested_vehicle_type);
        do apply_active_mode(mode_payload);
    }

    string normalize_town_name(string raw_value) {
        // vehicle 內部版：避免從 species 內跨 scope 呼叫 global function 造成 GAML parser 解析錯誤。
        string cleaned <- string(raw_value);
        loop area over: town {
            if (cleaned contains area.town_name) {
                return area.town_name;
            }
        }
        return default_origin_town;
    }

    point choose_road_point_in_town(string requested_town_name) {
        // vehicle 內部版：直接在本 species scope 呼叫，避免 world.function(...) 在部分 GAMA 版本被標錯。
        string normalized_town <- normalize_town_name(requested_town_name);
        town selected_town <- town first_with (each.town_name = normalized_town and each.county_name = "臺南市");

        if (selected_town = nil) {
            write "[WARN] Cannot find town '" + requested_town_name + "', fallback to " + default_origin_town;
            selected_town <- town first_with (each.town_name = default_origin_town and each.county_name = "臺南市");
        }

        if (selected_town != nil) {
            // line-in-polygon 用 intersects 比 overlapping 穩；overlap 對「道路完全在行政區內」可能不成立。
            list<road> roads_in_town <- road where (each.shape intersects selected_town.shape);
            if (!empty(roads_in_town)) {
                road selected_road <- one_of(roads_in_town);
                return any_location_in(selected_road);
            }

            road nearest_road <- road closest_to selected_town;
            if (nearest_road != nil) {
                write "[WARN] No intersecting ROADLINK in " + selected_town.town_name + "; use nearest road instead.";
                return any_location_in(nearest_road);
            }

            if (!empty(road)) {
                write "[WARN] Spatial query failed for " + selected_town.town_name + "; use random ROADLINK because road count = " + string(length(road));
                return any_location_in(one_of(road));
            }

            write "[ERROR] No ROADLINK agents loaded. Check shape_file_roads path and ROADLINK shp/dbf/shx files.";
            return selected_town.location;
        }

        if (!empty(road)) {
            return any_location_in(one_of(road));
        }

        write "[ERROR] No ROADLINK agents loaded, cannot place agent on road.";
        return {0, 0};
    }

    point choose_fixed_target_point {
        if (fixed_destination_location != nil) {
            return fixed_destination_location;
        }
        write "[WARN] fixed_destination_location is nil; fallback to destination_town road point.";
        return choose_road_point_in_town(destination_town);
    }

    road current_road {
        // 找出 agent 當前所在或最近 ROADLINK。
        road current_rd <- road(current_edge);
        if (current_rd = nil) {
            current_rd <- road closest_to self;
        }
        return current_rd;
    }

    town current_town {
        // 找出 agent 目前所在臺南市行政區；若不在 polygon 內，取最近行政區。
        list<town> overlapping_towns <- town overlapping self;
        town current_area <- empty(overlapping_towns) ? nil : one_of(overlapping_towns);
        if (current_area = nil) {
            current_area <- town closest_to self;
        }
        return current_area;
    }

    map build_environment_payload {
        // 將 simulation、spatial、road、agent、neighborhood、memory 組成完整環境狀態。
        // 目前 API payload 僅保留代表當前 agent 局部環境的欄位。
        road current_rd <- current_road();
        town current_area <- current_town();
        list<vehicle> nearby_agents <- (vehicle at_distance perception_radius) - self;

        return [
            "current_town"::(current_area = nil ? "" : current_area.town_name),
            "current_road_id"::(current_rd = nil ? "" : current_rd.road_id),
            "route_status"::route_status,
            "nearby_agent_count"::length(nearby_agents),
            "congestion_proxy"::(current_rd = nil ? 0.0 : current_rd.congestion_proxy),
            "distance_to_destination_m"::(target = nil ? 0.0 : location distance_to target)
        ];
    }

    map build_memory_entry {
        // 每一步的旅行記憶；36 steps 數量不大，先保留完整 memory。
        // 目前只保留判斷移動趨勢、壅塞感知與是否抵達所需的代表欄位。
        road current_rd <- current_road();
        town current_area <- current_town();
        list<vehicle> nearby_agents <- (vehicle at_distance perception_radius) - self;

        return [
            "cycle"::cycle,
            "current_town"::(current_area = nil ? "" : current_area.town_name),
            "current_road_id"::(current_rd = nil ? "" : current_rd.road_id),
            "active_mode"::mode_name,
            "vehicle_type"::vehicle_type,
            "route_status"::route_status,
            "nearby_agent_count"::length(nearby_agents),
            "congestion_proxy"::(current_rd = nil ? 0.0 : current_rd.congestion_proxy),
            "distance_to_destination_m"::(target = nil ? 0.0 : location distance_to target)
        ];
    }

    map build_api_agent_payload {
        // 單一 agent 每 step 送給 Python 的完整 payload。
        // active_mode 只傳目前 mode 名稱；完整參數留在 GAMA 內部運算使用。
        return [
            "agent_id"::agent_id,
            "agent_name"::profile_agent_name,
            "origin_town"::origin_town,
            "destination_town"::destination_town,
            "active_mode"::mode_name,
            "vehicle_type"::vehicle_type,
            "environment"::build_environment_payload(),
            "memory"::travel_memory
        ];
    }

    reflex perceive_environment when: !waiting_for_origin {
        // 依道路速限與附近 agent 調整實際速度。
        road current_rd <- current_road();
        float base_speed <- desired_speed;

        if (current_rd != nil) {
            float road_limit <- (vehicle_type = "機車") ? current_rd.speed_moto : current_rd.speed_car;
            if (road_limit > 0.0 and base_speed > road_limit) {
                base_speed <- road_limit;
            }
            if (road_limit <= 0.0 and base_speed > missing_road_speed_cap) {
                base_speed <- missing_road_speed_cap;
            }
        }

        list<vehicle> nearby_agents <- (vehicle at_distance perception_radius) - self;
        is_crowded <- length(nearby_agents) > 0;
        speed <- (is_crowded ? base_speed * crowded_speed_factor : base_speed) #km / #h;
    }

    reflex move when: !waiting_for_origin and route_status != "arrived" {
        // 優先遵守外部強制路徑，其次外部強制目標，否則前往固定 destination_town 的 ROADLINK 點。
        point before_move <- location;

        if (external_forced_path != nil) {
            selected_action <- "follow_external_route";
            do follow path: external_forced_path;
            if (location = external_forced_path.target) {
                external_forced_path <- nil;
            }
        } else if (external_forced_target != nil) {
            selected_action <- "goto_external_target";
            do goto target: external_forced_target on: road_network recompute_path: true;
            if (location = external_forced_target) {
                external_forced_target <- nil;
            }
        } else {
            selected_action <- is_crowded ? "goto_destination_recompute_path" : "goto_destination";
            do goto target: target on: road_network recompute_path: is_crowded;
        }

        distance_moved_last_step <- before_move distance_to location;

        road current_rd <- current_road();
        next_road_id <- (current_rd = nil) ? "unknown" : current_rd.road_id;

        if (target != nil and location distance_to target < arrival_distance_threshold) {
            route_status <- "arrived";
            selected_action <- "arrived";
        } else {
            route_status <- "moving";
        }
    }

    reflex record_agent_status {
        // 每步更新 memory 並輸出 CSV，供後續驗證與 Python payload 使用。
        road current_rd <- current_road();
        town current_area <- current_town();
        list<vehicle> nearby_agents <- (vehicle at_distance perception_radius) - self;
        map memory_entry <- build_memory_entry();
        travel_memory << memory_entry;

        save [
            cycle,
            agent_id,
            origin_town,
            destination_town,
            (current_area = nil ? "" : current_area.town_name),
            (current_rd = nil ? "" : current_rd.road_id),
            next_road_id,
            mode_name,
            vehicle_type,
            speed * 3.6,
            distance_moved_last_step,
            length(nearby_agents),
            route_status,
            api_status,
            warning_message
        ] to: agent_memory_path format: "csv" rewrite: false;
    }

    aspect default {
        // agent 視覺化：壅塞 magenta、抵達 green、移動中 blue。
        rgb agent_color <- route_status = "arrived" ? #green : (is_crowded ? #magenta : #blue);
        if (vehicle_type = "汽車") {
            draw triangle(car_display_size) color: agent_color rotate: heading + 90;
        } else {
            draw circle(moto_display_size) color: agent_color;
        }
    }
}

experiment Traffic_Simulation type: gui {
    // GUI 參數：不用改程式也能調整 API、agent 數量與 fallback 行政區。
    parameter "Enable Python API POST" var: enable_api_post;
    parameter "Number of agents" var: nb_agents min: 1 max: 200;
    parameter "Fallback destination town" var: destination_town_name;
    parameter "Fallback origin town" var: default_origin_town;

    output {
        display main_display {
            species town aspect: default;
            species destination_point aspect: default;
            species road aspect: default;
            species vehicle aspect: default;
        }
    }
}
