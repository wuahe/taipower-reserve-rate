"""FastAPI 入口:提供圖表頁與資料 API,並掛載每 10 分鐘的抓取排程。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, fetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TPE = ZoneInfo("Asia/Taipei")
STATIC_DIR = Path(__file__).resolve().parent / "static"

scheduler = BackgroundScheduler(timezone=TPE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 啟動先立即抓一次,不必等第一個 interval
    fetcher.fetch_and_store()
    scheduler.add_job(
        fetcher.fetch_and_store,
        "interval",
        minutes=10,
        id="fetch_taipower",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("排程已啟動:每 10 分鐘抓取台電開放資料")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="台電備轉容量率", lifespan=lifespan)


@app.get("/api/history")
def api_history(date: str | None = None) -> JSONResponse:
    """回傳某日所有資料點;未給 date 則用台灣今日。"""
    if date is None:
        date = datetime.now(TPE).strftime("%Y-%m-%d")
    return JSONResponse({"date": date, "points": db.get_history(date)})


@app.get("/api/dates")
def api_dates() -> JSONResponse:
    return JSONResponse({"dates": db.get_dates()})


@app.post("/api/ingest")
async def api_ingest(request: Request) -> JSONResponse:
    """接受外部推送的台電原始 JSON,解析後存入 DB。

    用於 Tokyo 伺服器因 IP 封鎖無法直接抓台電時,
    由台灣節點(GitHub Actions / n8n / Mac cron)推送資料。
    資料格式與 loadpara.json 相同。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    reading = fetcher.parse_loadpara(body)
    if reading is None:
        raise HTTPException(status_code=422, detail="cannot parse loadpara data")

    inserted = db.insert_reading(reading)
    return JSONResponse({"ok": True, "inserted": inserted, "ts": reading["ts"]})


@app.get("/api/latest")
def api_latest() -> JSONResponse:
    return JSONResponse({"latest": db.get_latest()})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# 其餘靜態資源(若日後新增)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
