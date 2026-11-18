"""Connectivity check: prints only status codes and small samples, never keys."""
from alpaca_client import DATA_URL, get, trading_get

r = get(f"{DATA_URL}/v2/stocks/bars", {
    "symbols": "AAPL", "timeframe": "1Day", "start": "2021-11-01",
    "limit": 3, "adjustment": "all", "feed": "sip",
})
print("bars sip:", r.status_code, r.text[:300] if r.status_code != 200 else str(r.json()["bars"]["AAPL"][:2]))

r = trading_get("/v2/assets", {"status": "active", "asset_class": "us_equity"})
print("assets:", r.status_code, (len(r.json()) if r.status_code == 200 else r.text[:200]))
