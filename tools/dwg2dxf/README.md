# DWG → DXF 轉檔工具（LibreDWG 路線）

實測日期 2026-09-30，樣本：測試用 DWG 19 個（AC1027），結果 19/19 成功。

## 需要的東西

1. **LibreDWG 0.14 Windows 執行檔**（GNU 官方，GPL-3.0）
   - 下載：https://github.com/LibreDWG/libredwg/releases/download/0.14/libredwg-0.14-win64.zip （12.1 MB，同頁有 `.sig` GPG 簽章）
   - 解壓後用到 `dwg2dxf.exe`；同包還有 `dwg2SVG.exe`（出 SVG）、`dwglayers.exe`（列圖層）、`dwgbmp.exe`（抽內嵌縮圖）。
   - Linux 版：Fedora／openSUSE 有 `libredwg-tools` 套件；Debian／Alpine 沒有，要自行編譯。
2. Python 3.12+ 與 `ezdxf`（`pip install ezdxf`）。

## 三步固定流程（不分檔案，一律跑完）

```
dwg2dxf.exe -y -o 輸出.dxf 來源.dwg          # 步驟 1：轉檔（stderr 的 ERROR/Warning 是警告，可忽略）
python repair_dxf.py 輸出.dxf 輸出.repaired.dxf   # 步驟 2：修補 APPID 表壞名稱與斷行
ezdxf.recover.readfile("輸出.repaired.dxf")    # 步驟 3：用修復模式讀
```

為什麼要步驟 2：這批圖的 APPID 表（曾處理此圖的外掛清單）有 16 筆名稱含中文與換行，LibreDWG 原樣寫出會打斷 DXF 結構，ezdxf 讀到約 18 萬行會報 `Invalid group code`。修補只動表頭，不動圖面。

## 批次實測

```
python test_dwg2dxf.py <dwg2dxf.exe> <DWG資料夾> <DXF輸出資料夾> <報告.md>
```
會對每個 DWG 記錄退出碼、秒數、DXF 大小、實體／圖塊／文字／圖層統計與中文抽樣。

## 正式上線的兩條規矩

- `dwg2dxf` 必須在沙箱容器跑（無網路、限時、限記憶體）：LibreDWG 0.14.x 仍有記憶體安全漏洞修補紀錄，而輸入是外部上傳的檔案。
- 外部設計單位的圖要另外抽樣測；這 19 檔來自同一環境，不代表全部。

## 不用 AutoCAD 看圖

```
python render_png.py 輸入.dxf 輸出.png 1800 [中文字型檔]
```
腳本會自動把所有文字樣式換成同一個中文 TrueType 字型再畫（預設 `msjh.ttc`；Linux 伺服器傳 `NotoSansCJK-Regular.ttc`）。原因：圖內常混用 SHX 與 kaiu.ttf 等字型，其中有的字形資料會讓渲染報錯。LibreDWG 內建的 `dwg2SVG` 會漏中文，不要用。

## 資源需求（實測）

轉檔單核心、尖峰記憶體 140 MB；ezdxf 讀取尖峰 145 MB；19 檔全流程 33 秒。量測腳本：`measure_resources.py`。
