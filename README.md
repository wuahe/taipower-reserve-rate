# 台電備轉容量率 · 全日曲線觀測

每 5 分鐘抓取台電即時電力資料,儲存時間序列,以網頁畫出整天的**備轉容量率**變化曲線(同時顯示即時負載、供電能力與供電燈號)。

## 為什麼需要自己存資料?

台電官網只提供**當下快照**(約每 10 分鐘更新),沒有「整天歷史曲線」的 API。因此本服務自己定時抓取並把每個時間點存進 SQLite,再由網頁讀出畫成曲線。

## 資料來源

```
https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/loadpara.json
```

需帶瀏覽器標頭(`User-Agent` + `Referer`),否則 CloudFront 回 403。

| 欄位 | 意義 |
|------|------|
| `curr_load` | 即時負載 MW |
| `curr_util_rate` | 即時用電率 %(負載 / 供電能力) |
| `real_hr_maxi_sply_capacity` | 即時供電能力 MW |
| `fore_peak_resv_indicator` | 當日燈號 G/Y/O/R |
| `publish_time` | 民國時間戳,如 `115.05.29(五)16:00` |

**備轉容量率 % = (供電能力 − 負載) / 負載 × 100**

## 架構

```
APScheduler(每5分鐘) ── fetch_and_store() ── SQLite (readings 表)
FastAPI
  GET /                       圖表頁(ECharts)
  GET /api/history?date=…     某日所有資料點
  GET /api/dates              有資料的日期清單
  GET /api/latest             最新一筆
```

- `app/db.py` — SQLite 儲存層,`ts` 為主鍵天然去重
- `app/fetcher.py` — 抓取、民國時間解析、備轉率計算、錯誤容忍
- `app/main.py` — FastAPI 路由 + 排程
- `app/static/index.html` — 單檔前端(ECharts CDN,繁中)

## 本地執行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
# 開 http://localhost:8080/
```

啟動時會立即抓一次,之後每 5 分鐘抓一次(台電 10 分鐘才更新,重複的時間點以 `INSERT OR IGNORE` 去重)。

資料庫預設寫在專案目錄 `taipower.db`;設環境變數 `DB_PATH` 可改路徑。

## 部署到 Zeabur

1. 推到 Git repo,Zeabur 以 **Dockerfile** 建置。
2. 掛一顆 **Volume** 到 `/data`,並設環境變數:
   ```
   DB_PATH=/data/taipower.db
   ```
   確保重啟 / 重新部署後歷史資料不流失。
3. Zeabur 會注入 `PORT`,容器已監聽該埠;開啟 domain 即可使用。
4. 部署後等待 ≥2 個 10 分鐘週期,確認資料點持續累積。

## 備註

- 時區固定 `Asia/Taipei`,避免跨日 `date` 錯置。
- `supply_mw` 主算法採台電「即時最高供電能力」;另存 `supply_mw_alt`(由用電率反推)備查,可日後比對官網數字調整。
