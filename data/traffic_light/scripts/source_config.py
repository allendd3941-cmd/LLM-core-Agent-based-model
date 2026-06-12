from __future__ import annotations

from dataclasses import dataclass, field


TAIWAN_CITIES = [
    "基隆市",
    "臺北市",
    "新北市",
    "桃園市",
    "新竹市",
    "新竹縣",
    "苗栗縣",
    "臺中市",
    "彰化縣",
    "南投縣",
    "雲林縣",
    "嘉義市",
    "嘉義縣",
    "臺南市",
    "高雄市",
    "屏東縣",
    "宜蘭縣",
    "花蓮縣",
    "臺東縣",
    "澎湖縣",
    "金門縣",
    "連江縣",
]


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    source_name: str
    agency: str
    url: str
    landing_url: str
    fmt: str
    covered_cities: str
    parser: str
    official: bool = True
    license: str = "政府資料開放授權條款-第1版"
    note: str = ""
    file_name: str = ""
    request_method: str = "GET"
    request_body: str = ""
    extra: dict[str, str] = field(default_factory=dict)


OVERPASS_QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="TW"][admin_level=2]->.tw;
(
  node["highway"="traffic_signals"](area.tw);
  way["highway"="traffic_signals"](area.tw);
  relation["highway"="traffic_signals"](area.tw);
);
out center tags;
"""


SOURCE_SPECS = [
    SourceSpec(
        source_id="GOVTW_CATALOG",
        source_name="政府資料開放平臺資料集全量匯出",
        agency="數位發展部",
        url="https://data.gov.tw/datasets/export/csv",
        landing_url="https://data.gov.tw/datasets/export/csv",
        fmt="CSV",
        covered_cities="全國",
        parser="catalog",
        file_name="data_gov_tw_datasets_export.csv",
        note="用於搜尋、查證與產生候選來源清單，不直接產生點位。",
    ),
    SourceSpec(
        source_id="TPE_SIGNAL_POINTS",
        source_name="臺北市號誌位置",
        agency="臺北市交通管制工程處",
        url="https://data.taipei/api/dataset/4a599738-6550-448a-9a78-03b26c67e249/resource/9da220ce-f5a9-40a8-aacc-dc17978f3a87/download",
        landing_url="https://data.taipei/dataset/detail?id=4a599738-6550-448a-9a78-03b26c67e249",
        fmt="CSV",
        covered_cities="臺北市",
        parser="taipei_points",
        file_name="taipei_signal_points.csv",
    ),
    SourceSpec(
        source_id="TPE_PED_PHASE",
        source_name="臺北市行人專用時相路口及時段",
        agency="臺北市交通管制工程處",
        url="https://data.taipei/api/dataset/733872cf-f931-4d27-aba7-f01d7c271bdc/resource/637df73c-6c11-49ae-825f-fd27f2d222c1/download",
        landing_url="https://data.taipei/dataset/detail?id=733872cf-f931-4d27-aba7-f01d7c271bdc",
        fmt="CSV",
        covered_cities="臺北市",
        parser="taipei_ped_phase",
        file_name="taipei_pedestrian_phase.csv",
        note="只有行人專用時相實施時段，未提供一般號誌相位秒數。",
    ),
    SourceSpec(
        source_id="TPE_TIMING_TABLE",
        source_name="臺北市路口號誌時制計畫三合一報表左半邊",
        agency="臺北市政府交通局",
        url="https://tcgbusfs.blob.core.windows.net/dotapp/timing_plan_table.json",
        landing_url="https://data.taipei/dataset/detail?id=0d639f73-cbcc-42c3-aa53-20efac199701",
        fmt="JSON",
        covered_cities="臺北市",
        parser="taipei_timing_json",
        file_name="taipei_timing_plan_table.json",
        note="時制計畫資料，座標需與臺北市號誌位置資料以號誌編號或路口名銜接。",
    ),
    SourceSpec(
        source_id="TPE_TIMING_PLAN",
        source_name="臺北市路口號誌時制計畫三合一報表右半邊",
        agency="臺北市政府交通局",
        url="https://tcgbusfs.blob.core.windows.net/dotapp/timing_plan.json",
        landing_url="https://data.taipei/dataset/detail?id=0d639f73-cbcc-42c3-aa53-20efac199701",
        fmt="JSON",
        covered_cities="臺北市",
        parser="taipei_timing_json",
        file_name="taipei_timing_plan.json",
        note="時制計畫資料，座標需與臺北市號誌位置資料以號誌編號或路口名銜接。",
    ),
    SourceSpec(
        source_id="TPE_TIMING_CSV",
        source_name="臺北市政府交通局路口時制號誌資料",
        agency="臺北市政府交通局",
        url="https://data.taipei/api/dataset/0d639f73-cbcc-42c3-aa53-20efac199701/resource/9522d9e8-131e-4890-9379-f17c523238a0/download",
        landing_url="https://data.taipei/dataset/detail?id=0d639f73-cbcc-42c3-aa53-20efac199701",
        fmt="CSV",
        covered_cities="臺北市",
        parser="taipei_timing_csv",
        file_name="taipei_timing_signal.csv",
        note="資料集中欄位含 icid、icname、deviceid、segmenttype、subsegment、InfoTime。",
    ),
    SourceSpec(
        source_id="TNN_SIGNAL_SHP_A",
        source_name="臺南市公共管線圖資-交通號誌系統號誌 A",
        agency="臺南市政府工務局",
        url="https://data.tainan.gov.tw/Resource/e8a18b1e-770b-421f-afa6-fb53e1089cd7?fileId=0e56d943-fa44-4255-8245-22258322de22&importFileType=ShapeFile&handler=DLGisFile",
        landing_url="https://data.gov.tw/dataset/137239",
        fmt="SHP",
        covered_cities="臺南市",
        parser="tainan_signal_shp",
        file_name="tainan_signal_system_signal_a.zip",
        note="公共管線圖資，官方說明為參考圖資。",
    ),
    SourceSpec(
        source_id="TNN_SIGNAL_SHP_B",
        source_name="臺南市公共管線圖資-交通號誌系統號誌 B",
        agency="臺南市政府工務局",
        url="https://data.tainan.gov.tw/Resource/28b3f930-104f-4fdf-8faf-6c4e0cf6ec5b?fileId=af6c0e74-0b22-4266-a155-853461562453&importFileType=ShapeFile&handler=DLGisFile",
        landing_url="https://data.gov.tw/dataset/137239",
        fmt="SHP",
        covered_cities="臺南市",
        parser="tainan_signal_shp",
        file_name="tainan_signal_system_signal_b.zip",
        note="公共管線圖資，官方說明為參考圖資。",
    ),
    SourceSpec(
        source_id="TNN_WIFI_SIGNAL",
        source_name="臺南市交通號誌(路口紅綠燈)4G_WiFi點位資料",
        agency="臺南市政府交通局",
        url="https://data.tainan.gov.tw/File/ResourceCsvDownload/3bf1de50-11d9-455b-9fc7-9f1bc6dcd9b8",
        landing_url="https://data.gov.tw/dataset/79978",
        fmt="CSV",
        covered_cities="臺南市",
        parser="tainan_wifi_points",
        file_name="tainan_signal_wifi_points.csv",
        note="僅為交通號誌 4G/WiFi 點位，非完整號誌清冊。",
    ),
    SourceSpec(
        source_id="TNN_PED_PHASE",
        source_name="臺南市行人專用時相位置與實施時間一覽表",
        agency="臺南市政府交通局",
        url="https://data.tainan.gov.tw/File/ResourceCsvDownload/fce53714-0761-41e5-9c84-7e86b1250713",
        landing_url="https://data.gov.tw/dataset/102305",
        fmt="CSV",
        covered_cities="臺南市",
        parser="tainan_ped_phase",
        file_name="tainan_pedestrian_phase.csv",
        note="提供行人專用時相位置與實施時間，未提供經緯度或一般相位秒數。",
    ),
    SourceSpec(
        source_id="CHA_SIGNAL_POINTS",
        source_name="彰化縣路口號誌經緯度資料",
        agency="彰化縣政府交通處",
        url="https://email.chcg.gov.tw/df/zw3diqdbhynvsgpla8kxx7gzpjbq8n",
        landing_url="https://data.gov.tw/dataset/37695",
        fmt="CSV",
        covered_cities="彰化縣",
        parser="changhua_points",
        file_name="changhua_signal_points.csv",
    ),
    SourceSpec(
        source_id="PENGHU_SIGNAL_POINTS_PHASE",
        source_name="紅綠燈號誌位置一覽表",
        agency="澎湖縣政府",
        url="https://opendataap2.penghu.gov.tw/./resource/files/2025-06-28/5c0f014618c12cf477eeb7ad60f68609.csv",
        landing_url="https://data.gov.tw/dataset/113153",
        fmt="CSV",
        covered_cities="澎湖縣",
        parser="penghu_points_phase",
        file_name="penghu_signal_points_phase.csv",
        note="同時含位置、運作情形、部分時制欄位。",
    ),
    SourceSpec(
        source_id="TC_SIGNAL_BOX",
        source_name="臺中市路口號誌箱",
        agency="臺中市政府交通局",
        url="https://newdatacenter.taichung.gov.tw/api/v1/no-auth/resource.download?rid=2968d46a-3afb-49b6-995b-16ea343b19d6",
        landing_url="https://data.gov.tw/dataset/82304",
        fmt="CSV",
        covered_cities="臺中市",
        parser="taichung_signal_box",
        file_name="taichung_signal_box.csv",
        note="僅含號誌箱設置位置文字，無公開座標；不直接轉為 Shapefile 點位。",
    ),
    SourceSpec(
        source_id="TC_SIGNAL_GIS_INDEX",
        source_name="臺中市政府交通局號誌燈圖資",
        agency="臺中市政府交通局",
        url="https://newdatacenter.taichung.gov.tw/api/v1/no-auth/resource.download?rid=dbb814b4-d595-4109-be87-40612dd93926",
        landing_url="https://data.gov.tw/dataset/103743",
        fmt="CSV",
        covered_cities="臺中市",
        parser="taichung_gis_index",
        file_name="taichung_signal_gis_index.csv",
        note="索引型資料，若連結欄提供可公開下載圖資，下載腳本會嘗試另存。",
    ),
    SourceSpec(
        source_id="CY_COUNTY_PHASE_INDEX",
        source_name="嘉義縣各鄉鎮交通號誌路段及時段清冊",
        agency="嘉義縣政府",
        url="https://ws-tm.cyhg.gov.tw/Download.ashx?u=LzAwMS9VcGxvYWQvMTM0Mi9yZWxmaWxlLzEyNTU4LzIyMTY2NS9hOTg3M2E3OC00ZGQyLTRiMTQtYmI4Yi1mZmNiNDNjZDhlYjguY3N2&n=MTE15ZiJ576p57ijMTjphInpjq7kuqTpgJromZ%2foqozmuIXlhopfT1BFTkRBVEFfLmNzdg%3d%3d",
        landing_url="https://data.gov.tw/dataset/156838",
        fmt="CSV",
        covered_cities="嘉義縣",
        parser="chiayi_county_phase_index",
        file_name="chiayi_county_phase_index.csv",
        note="索引型資料，通常需逐一開啟鄉鎮連結；未提供經緯度。",
    ),
    SourceSpec(
        source_id="KHH_PED_EXCLUSIVE_PDF",
        source_name="高雄市行人專用路口明細",
        agency="高雄市政府交通局",
        url="https://www.tbkc.gov.tw/FileOutput/Page/%E9%AB%98%E9%9B%84%E5%B8%82%E8%A1%8C%E4%BA%BA%E5%B0%88%E7%94%A8114.12.pdf?id=bc9c3dfd-b01a-4e65-9399-b7184a208c8e",
        landing_url="https://www.tbkc.gov.tw/Message/Gopen/PedestrianArea",
        fmt="PDF",
        covered_cities="高雄市",
        parser="kaohsiung_pdf_phase",
        file_name="kaohsiung_pedestrian_exclusive.pdf",
        note="PDF 明細，含路段、路口、日期、設置時段；未提供座標。",
    ),
    SourceSpec(
        source_id="KHH_PED_LEAD_PDF",
        source_name="高雄市行人早開路口明細",
        agency="高雄市政府交通局",
        url="https://www.tbkc.gov.tw/FileOutput/Page/%E9%AB%98%E9%9B%84%E5%B8%82%E8%A1%8C%E4%BA%BA%E6%97%A9%E9%96%8B114.12.pdf?id=a3a0f575-6db5-463d-8554-a796cd1cbb06",
        landing_url="https://www.tbkc.gov.tw/Message/Gopen/PedestrianArea",
        fmt="PDF",
        covered_cities="高雄市",
        parser="kaohsiung_pdf_phase",
        file_name="kaohsiung_pedestrian_lead.pdf",
        note="PDF 明細，含路段、路口、日期、設置時段；未提供座標。",
    ),
    SourceSpec(
        source_id="KHH_PED_LEAD_T_PDF",
        source_name="高雄市行人早開(T型路口)明細",
        agency="高雄市政府交通局",
        url="https://www.tbkc.gov.tw/FileOutput/Page/%E9%AB%98%E9%9B%84%E5%B8%82%E8%A1%8C%E4%BA%BA%E6%97%A9%E9%96%8B%28T%E5%9E%8B%E8%B7%AF%E5%8F%A3%29114.12.pdf?id=722db2e0-1e0d-41f8-8b2b-496bfb91e0d5",
        landing_url="https://www.tbkc.gov.tw/Message/Gopen/PedestrianArea",
        fmt="PDF",
        covered_cities="高雄市",
        parser="kaohsiung_pdf_phase",
        file_name="kaohsiung_pedestrian_lead_t.pdf",
        note="PDF 明細，含路段、路口、日期、設置時段；未提供座標。",
    ),
    SourceSpec(
        source_id="OSM_TRAFFIC_SIGNALS",
        source_name="OpenStreetMap highway=traffic_signals Taiwan",
        agency="OpenStreetMap contributors",
        url="https://overpass-api.de/api/interpreter",
        landing_url="https://www.openstreetmap.org/",
        fmt="Overpass JSON",
        covered_cities="全國",
        parser="osm_traffic_signals",
        official=False,
        license="Open Database License (ODbL)",
        file_name="osm_traffic_signals_taiwan.json",
        request_method="POST",
        request_body=OVERPASS_QUERY,
        note="非官方補充來源，只用於補足官方點位涵蓋不足；不可視為官方清冊。",
    ),
]

