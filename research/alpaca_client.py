"""Minimal Alpaca REST client for historical daily data.

Keys are read from the project's .env (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY)
and are never printed or logged.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATA_URL = "https://data.alpaca.markets"
TRADING_URLS = ["https://paper-api.alpaca.markets", "https://api.alpaca.markets"]


def _headers() -> dict:
    key = os.getenv("ALPACA_API_KEY_ID", "").strip()
    secret = os.getenv("ALPACA_API_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY missing in .env")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def get(url: str, params: dict | None = None, retries: int = 5) -> requests.Response:
    for attempt in range(retries):
        r = requests.get(url, headers=_headers(), params=params, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return r
    return r


def trading_get(path: str, params: dict | None = None) -> requests.Response:
    """Try paper then live trading endpoints (whichever the key belongs to)."""
    last = None
    for base in TRADING_URLS:
        r = get(base + path, params)
        if r.status_code != 401 and r.status_code != 403:
            return r
        last = r
    return last
