"""Screenshot the local dashboard with headless Chrome or Edge. Offline; loopback only.

    python -m dashboard.capture --snapshot .cache/dashboard/snapshot.json --out docs/media/dashboard.png
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from dashboard.server import StateSource, make_server

ROOT = Path(__file__).resolve().parent.parent
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome", "chromium", "msedge",
]


def browser():
    for candidate in BROWSERS:
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    raise SystemExit("no Chrome or Edge found for headless capture")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--journal-dir", type=Path, default=ROOT / "journal")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--size", default="1440,2200", help="window width,height in CSS pixels")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--wait-ms", type=int, default=4000)
    args = parser.parse_args()

    server = make_server(StateSource(args.snapshot, False, args.journal_dir), args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([
            browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            f"--user-data-dir={profile}", f"--window-size={args.size}",
            f"--virtual-time-budget={args.wait_ms}", f"--screenshot={args.out.resolve()}",
            f"http://127.0.0.1:{args.port}/",
        ], check=True, timeout=120, capture_output=True)
    server.shutdown()
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
