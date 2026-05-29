#!/usr/bin/env python3
"""
從 Mac（台灣 IP）每 5 分鐘抓台電資料，推送到 Zeabur Tokyo 服務。
Taiwan IP → Taipower OK → POST → Zeabur → 圖表顯示

用法：
  python3 push_from_mac.py                    # 前台執行
  python3 push_from_mac.py --once             # 執行一次後退出（測試用）
  INGEST_URL=https://... python3 push_from_mac.py  # 自訂 ingest URL

後台持續執行（推薦）：
  nohup python3 /path/to/push_from_mac.py >> /tmp/taipower_push.log 2>&1 &
"""

import json
import logging
import os
import ssl
import sys
import time

# 安裝：pip3 install httpx
try:
    import httpx
except ImportError:
    print("請先安裝：pip3 install httpx")
    sys.exit(1)

# ── 設定 ──────────────────────────────────────────────
LOADPARA_URL = (
    "https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/loadpara.json"
)
INGEST_URL = os.environ.get(
    "INGEST_URL",
    "https://taipower-reserve.zeabur.app/api/ingest",
)
INTERVAL_SEC = 10 * 60  # 10 分鐘（台電更新週期）

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT  # Taipower 憑證缺 SKI

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.taipower.com.tw/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
}
# ──────────────────────────────────────────────────────


def fetch_and_push() -> bool:
    # 抓台電
    try:
        with httpx.Client(verify=_SSL_CTX, timeout=20, headers=_HEADERS,
                          follow_redirects=True) as client:
            r = client.get(LOADPARA_URL)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning("抓取台電失敗：%s", e)
        return False

    # 推送到 Zeabur
    try:
        resp = httpx.post(INGEST_URL, json=data, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            logger.info("✅ 推送成功 ts=%s  inserted=%s",
                        result.get("ts"), result.get("inserted"))
            return True
        else:
            logger.warning("推送失敗 HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        logger.warning("推送例外：%s", e)
        return False


def main():
    once = "--once" in sys.argv
    logger.info("台電資料推送器啟動  INGEST_URL=%s", INGEST_URL)

    if once:
        ok = fetch_and_push()
        sys.exit(0 if ok else 1)

    while True:
        fetch_and_push()
        logger.info("下次推送在 %d 秒後…", INTERVAL_SEC)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
