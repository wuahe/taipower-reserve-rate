"""抓取台電即時電力資料、解析、計算備轉容量率。

資料來源:
  https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/loadpara.json
需帶瀏覽器標頭,否則 CloudFront 回 403。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
from zoneinfo import ZoneInfo

import httpx

from . import db

logger = logging.getLogger(__name__)

LOADPARA_URL = (
    "https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/loadpara.json"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.taipower.com.tw/",
    "Origin": "https://www.taipower.com.tw",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# 台電憑證鏈缺少 Subject Key Identifier,OpenSSL 3 嚴格檢查會擋下(curl 則放行)。
# 仍保留憑證驗證,僅關閉過嚴的 X509_STRICT 旗標。
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT

TPE = ZoneInfo("Asia/Taipei")

# 民國時間格式: 115.05.29(五)16:00
_ROC_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)\([^)]*\)(\d+):(\d+)")


def parse_roc_time(s: str) -> str | None:
    """民國時間字串轉 ISO,例 '115.05.29(五)16:00' -> '2026-05-29T16:00'。"""
    m = _ROC_RE.search(s or "")
    if not m:
        return None
    roc_y, mo, d, hh, mm = (int(x) for x in m.groups())
    return f"{roc_y + 1911:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}"


def _light_from_rate(reserve_rate: float, reserve_mw: float) -> str:
    """依台電供電警戒標準推估燈號(輔助用)。

    綠燈 >=10%;黃燈 6%~10%;橘燈 備轉容量 <90萬瓩(900MW);
    紅燈 備轉容量 <50萬瓩(500MW)。MW 門檻優先於百分比。
    """
    if reserve_mw < 500:
        return "R"
    if reserve_mw < 900:
        return "O"
    if reserve_rate < 6:
        return "Y"
    if reserve_rate < 10:
        return "Y"
    return "G"


def parse_loadpara(data: dict) -> dict | None:
    """把 loadpara.json 解析成一筆 reading(失敗回 None)。"""
    flat: dict = {}
    for rec in data.get("records", []):
        flat.update(rec)

    publish_time = flat.get("publish_time", "")
    ts = parse_roc_time(publish_time)
    if ts is None:
        logger.warning("無法解析 publish_time: %r", publish_time)
        return None

    try:
        load = float(flat["curr_load"])
        util = float(flat["curr_util_rate"])
    except (KeyError, ValueError) as exc:
        logger.warning("缺少/無法解析負載或用電率: %s", exc)
        return None

    # 供電能力:法A 用 real_hr_maxi_sply_capacity;法B 由用電率反推
    try:
        supply_a = float(flat["real_hr_maxi_sply_capacity"])
    except (KeyError, ValueError):
        supply_a = None
    supply_b = load / (util / 100) if util else None

    # 主供電能力:優先用法A(官方即時最高供電能力),缺則用法B
    supply = supply_a if supply_a is not None else supply_b
    if supply is None:
        logger.warning("兩種供電能力皆無法取得")
        return None

    reserve_mw = supply - load
    reserve_rate = reserve_mw / load * 100 if load else 0.0

    # 燈號:優先採台電當日 indicator,否則用備轉率推估
    light = flat.get("fore_peak_resv_indicator") or _light_from_rate(
        reserve_rate, reserve_mw
    )

    return {
        "ts": ts,
        "date": ts[:10],
        "reserve_rate": round(reserve_rate, 2),
        "load_mw": round(load, 1),
        "supply_mw": round(supply, 1),
        "supply_mw_alt": round(supply_b, 1) if supply_b is not None else None,
        "util_rate": util,
        "light": light,
        "raw": json.dumps(data, ensure_ascii=False),
    }


def fetch_and_store() -> bool:
    """抓取一次並寫入 DB。回傳是否成功寫入新資料(供日誌)。

    使用持久 session:先 GET 首頁讓 CloudFront Bot Management 設置 cookie,
    再用同一 client 帶 cookie 請求 JSON,模擬真實瀏覽器行為繞過 IP 封鎖。
    """
    try:
        with httpx.Client(
            verify=_SSL_CTX,
            follow_redirects=True,
            timeout=25,
            headers=_HEADERS,
        ) as client:
            # 先訪問主頁取得 CloudFront cookie
            client.get("https://www.taipower.com.tw/", timeout=15)
            # 用同一 session 帶 cookie 請求資料
            resp = client.get(LOADPARA_URL)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        logger.warning("抓取台電資料失敗,略過此次: %s", exc)
        return False

    reading = parse_loadpara(data)
    if reading is None:
        return False

    try:
        inserted = db.insert_reading(reading)
    except Exception as exc:  # noqa: BLE001 - DB 任何錯誤都不該中斷排程
        logger.warning("寫入 DB 失敗: %s", exc)
        return False

    if inserted:
        logger.info(
            "新增資料點 ts=%s 備轉率=%.2f%% 負載=%.0fMW 供電=%.0fMW 燈號=%s",
            reading["ts"], reading["reserve_rate"], reading["load_mw"],
            reading["supply_mw"], reading["light"],
        )
    else:
        logger.debug("資料點 ts=%s 已存在,略過", reading["ts"])
    return inserted
