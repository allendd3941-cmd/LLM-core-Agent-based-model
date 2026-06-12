# 臺灣紅綠燈點位與時相資料整理

本資料夾保存本任務的原始下載檔、處理腳本、標準化 CSV、Esri Shapefile、metadata 與 log。所有成果均限定在此資料夾內。

## 重現流程

在專案根目錄執行：

```powershell
python data\traffic_light\scripts\discover_sources.py
python data\traffic_light\scripts\download_raw_data.py
python data\traffic_light\scripts\build_shapefiles.py
python data\traffic_light\scripts\validate_outputs.py
```

## 輸出檔案

- `processed/traffic_light_points_full.csv`：完整欄位點位表。
- `processed/traffic_light_phase_records_full.csv`：完整欄位時相/時段表。
- `shapefile/traffic_light_points.*`：去重後點位 Shapefile，EPSG:4326。
- `shapefile/traffic_light_phase_records.*`：可定位時相/時段 Shapefile，EPSG:4326。
- `metadata/sources_catalog.csv`：來源目錄與下載狀態。
- `metadata/source_candidates.csv`：政府資料開放平臺全量目錄掃描出的候選來源。
- `metadata/coverage_report.csv`：22 縣市涵蓋情形。
- `metadata/data_dictionary.csv`：Shapefile 短欄位與完整欄位對照。
- `metadata/quality_report.md`：品質、缺漏、限制與模擬可用性說明。
- `logs/download_log.txt`：下載來源、HTTP 狀態、時間、檔案大小、錯誤原因。

## 來源限制

本成果不宣稱全臺完整號誌時相。多數縣市未公開一般號誌完整相位順序、綠燈秒數、黃燈秒數、全紅秒數與感應式邏輯。OpenStreetMap 只作為非官方補充點位來源，已在欄位中標示為「非官方補充」。

## 欄位與座標

Shapefile 欄位使用 10 字元以內短欄位名；完整欄位與說明見 `metadata/data_dictionary.csv`。點位與時相 Shapefile 均輸出為 WGS84 / EPSG:4326，並附 `.prj` 與 `.cpg`。

