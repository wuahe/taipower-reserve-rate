"""抓取台電即時電力資料、解析、計算備轉容量率。

資料來源:
  https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/loadpara.json
需帶瀏覽器標頭,否則 CloudFront 回 403。
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
from zoneinfo import ZoneInfo

import httpx

from . import db

logger = logging.getLogger(__name__)

# 開放資料主機(service.taipower.com.tw)不做地理封鎖,東京等雲端 IP 可直接抓,
# 格式與 www 的 loadpara.json 完全相同。優先使用此端點。
_OPENDATA_URL = "https://service.taipower.com.tw/data/opendata/apply/file/d006020/001.json"
_WWW_URL = "https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/loadpara.json"
LOADPARA_URL = os.environ.get("TAIPOWER_PROXY_URL", _OPENDATA_URL)

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
    """依台電供電警戒標準推估燈號。

    注意:reserve_mw 為台電原始單位「萬瓩」(1 萬瓩 = 10 MW)。
    台電門檻:紅燈 備轉容量 <50萬瓩;橘燈 <90萬瓩;
    黃燈 備轉容量率 6%~10%;綠燈 >=10%。MW 門檻優先於百分比。
    """
    if reserve_mw < 50:    # <50 萬瓩 = <500 MW
        return "R"
    if reserve_mw < 90:    # <90 萬瓩 = <900 MW
        return "O"
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

    # 供電能力(萬瓩):
    #   real_hr_maxi_sply_capacity 實為台電「即時估算供電能力」(隨機組/再生能源
    #   即時更新),與台電算「使用率」用的分母一致(非「今日最大供電能力」)。
    #   經實測:凌晨機組未全開時此值(如 3724)遠低於今日最大(如 4351),且
    #   ≈ 用電量÷使用率,故為即時值而非尖峰最大值,優先採用。
    #   法B(用電量÷使用率)作為備援,但因使用率為整數會有誤差。
    supply_a = None
    try:
        supply_a = float(flat["real_hr_maxi_sply_capacity"])
    except (KeyError, ValueError):
        pass
    supply_b = load / (util / 100) if util else None

    # 主供電能力:優先用台電即時估算值,缺則退回用電率反推
    supply = supply_a if supply_a is not None else supply_b
    if supply is None:
        logger.warning("兩種供電能力皆無法取得")
        return None

    reserve_mw = supply - load
    reserve_rate = reserve_mw / load * 100 if load else 0.0

    # 燈號:依即時備轉率計算(與卡片數字一致)
    light = _light_from_rate(reserve_rate, reserve_mw)

    # 台電官方今日預估尖峰備轉容量率(另存供前端顯示用)
    try:
        fore_peak_resv_rate = float(flat["fore_peak_resv_rate"])
    except (KeyError, ValueError):
        fore_peak_resv_rate = None

    return {
        "ts": ts,
        "date": ts[:10],
        "reserve_rate": round(reserve_rate, 2),
        "load_mw": round(load, 1),
        "supply_mw": round(supply, 1),
        "supply_mw_alt": round(supply_b, 1) if supply_b is not None else None,
        "util_rate": util,
        "light": light,
        "fore_peak_resv_rate": fore_peak_resv_rate,
        "raw": json.dumps(data, ensure_ascii=False),
    }


def fetch_and_store() -> bool:
    """抓取一次並寫入 DB。回傳是否成功寫入新資料(供日誌)。

    預設抓開放資料主機(不擋國外 IP),東京 server 可直接抓。
    若指定 www 主機則改用 session 暖身繞過 CloudFront。
    一律帶非嚴格 SSL context 解決台電憑證缺 SKI 的問題。
    """
    try:
        if LOADPARA_URL == _WWW_URL:
            # www 主機會擋國外 IP:先訪首頁建立 session
            with httpx.Client(
                verify=_SSL_CTX, follow_redirects=True, timeout=25, headers=_HEADERS,
            ) as client:
                client.get("https://www.taipower.com.tw/", timeout=15)
                resp = client.get(LOADPARA_URL)
        else:
            # 開放資料 / proxy:直接抓(仍用非嚴格 SSL)
            resp = httpx.get(LOADPARA_URL, headers=_HEADERS, verify=_SSL_CTX,
                             timeout=20, follow_redirects=True)
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
