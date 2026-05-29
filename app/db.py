"""SQLite 儲存層:建表、去重寫入、按日查詢。

資料庫路徑由環境變數 DB_PATH 決定(Zeabur 上掛 volume 到 /data,
設 DB_PATH=/data/taipower.db)。若該目錄不可寫,fallback 到專案本地檔
並印出警告,確保本地開發與線上都能運作。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL = Path(__file__).resolve().parent.parent / "taipower.db"


def _resolve_db_path() -> str:
    """決定 DB 檔案路徑,確認目錄可寫,否則 fallback 到本地檔。"""
    env_path = os.environ.get("DB_PATH")
    if env_path:
        candidate = Path(env_path)
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            # 測試可寫
            probe = candidate.parent / ".write_probe"
            probe.touch()
            probe.unlink()
            return str(candidate)
        except OSError as exc:
            logger.warning(
                "DB_PATH=%s 不可寫 (%s),fallback 到本地檔 %s",
                env_path, exc, _DEFAULT_LOCAL,
            )
    return str(_DEFAULT_LOCAL)


DB_PATH = _resolve_db_path()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建立資料表與索引(若不存在)。"""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                ts                  TEXT PRIMARY KEY,  -- ISO 時間,例 2026-05-29T16:00
                date                TEXT NOT NULL,     -- YYYY-MM-DD
                reserve_rate        REAL,              -- 即時備轉容量率 %
                load_mw             REAL,              -- 即時負載 MW
                supply_mw           REAL,              -- 供電能力 MW(主值)
                supply_mw_alt       REAL,              -- 另一算法供電能力(備查)
                util_rate           REAL,              -- 即時用電率 %
                light               TEXT,              -- 燈號 G/Y/O/R(依即時備轉率)
                fore_peak_resv_rate REAL,              -- 台電官方今日預估尖峰備轉率 %
                raw                 TEXT               -- 原始 JSON
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_readings_date ON readings(date)"
        )
        # 向前相容：舊 DB 缺少新欄位時自動補上
        try:
            conn.execute("ALTER TABLE readings ADD COLUMN fore_peak_resv_rate REAL")
            logger.info("DB 遷移：新增 fore_peak_resv_rate 欄位")
        except Exception:
            pass  # 欄位已存在，忽略
    logger.info("DB 已初始化:%s", DB_PATH)


def insert_reading(reading: dict) -> bool:
    """寫入一筆(同 ts 自動去重)。回傳是否實際新增。

    reading 需含 keys: ts, date, reserve_rate, load_mw, supply_mw,
    supply_mw_alt, util_rate, light, fore_peak_resv_rate, raw
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO readings
                (ts, date, reserve_rate, load_mw, supply_mw,
                 supply_mw_alt, util_rate, light, fore_peak_resv_rate, raw)
            VALUES
                (:ts, :date, :reserve_rate, :load_mw, :supply_mw,
                 :supply_mw_alt, :util_rate, :light, :fore_peak_resv_rate, :raw)
            """,
            reading,
        )
        return cur.rowcount > 0


def get_history(date: str) -> list[dict]:
    """回傳某日(YYYY-MM-DD)所有資料點,依時間排序。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, reserve_rate, load_mw, supply_mw, supply_mw_alt, "
            "util_rate, light, fore_peak_resv_rate "
            "FROM readings WHERE date = ? ORDER BY ts",
            (date,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_dates() -> list[str]:
    """回傳有資料的日期清單(新到舊)。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM readings ORDER BY date DESC"
        ).fetchall()
    return [r["date"] for r in rows]


def get_latest() -> Optional[dict]:
    """回傳最新一筆資料點(無資料則 None)。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT ts, date, reserve_rate, load_mw, supply_mw, "
            "supply_mw_alt, util_rate, light, fore_peak_resv_rate "
            "FROM readings ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
