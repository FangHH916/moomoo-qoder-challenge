"""Download adjusted daily bars for the extra cross-asset ETFs used by round 10."""
import pandas as pd

import download_bars as db

EXTRA = ["EFA", "EEM", "EWJ", "FXI", "HYG", "LQD", "TIP", "AGG", "DBC", "DBA", "UNG", "VNQ"]
df = db.fetch_batch(EXTRA)
df.to_parquet(db.DATA / "bars_extra_etfs.parquet")
print(df.groupby("symbol")["date"].agg(["min", "max", "count"]).to_string())
