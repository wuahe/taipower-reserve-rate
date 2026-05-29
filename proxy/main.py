"""輕量台電 API 中轉服務。

部署於台灣節點(無 CloudFront IP 封鎖),替 Tokyo 主服務代抓 loadpara.json。
只做一件事：帶瀏覽器標頭抓台電 JSON 並原樣回傳。
"""
import json
import logging
import os
import ssl

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.taipower.com.tw/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

LOADPARA_URL = (
    "https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/loadpara.json"
)

app = FastAPI(title="Taipower Proxy")


@app.get("/loadpara")
def get_loadpara():
    try:
        with httpx.Client(verify=_SSL_CTX, timeout=20, headers=_HEADERS,
                          follow_redirects=True) as client:
            r = client.get(LOADPARA_URL)
            r.raise_for_status()
            return JSONResponse(r.json())
    except Exception as exc:
        logger.warning("Proxy 抓取失敗: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/health")
def health():
    return {"ok": True}
