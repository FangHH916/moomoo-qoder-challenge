# Demonstration and evidence

The video is a narrated walkthrough of saved paper-account evidence, not a live recording. It does not demonstrate a deployed QCA workflow or a Slack control round trip.

- `demo_v1.mp4`: 1920 x 1080, English narration generated locally from the entrant's own voice model, burned-in English captions.
- `demo_v1.en.srt` and `demo_v1.zh.srt`: English and Chinese captions.
- `dashboard-review_v1.html`: self-contained interactive dashboard. Download and open locally; no server or login needed.
- `dashboard_v2.png`: screenshot of the local dashboard.
- `dashboard-snapshot_v1.json`: normalized, saved paper-account data used by the dashboard.

## Evidence boundaries

Snapshot: **23 November 2026, 15:28:46 UTC** (23:28:46 Singapore/Shanghai). Account 5832812. Equity $100,240.07; 69 positions; 68 submitted orders filled; no missing or working order at reconciliation. The account may have changed since this snapshot.

The saved reconciliation recorded five low-price entry rejections and five fills with adverse slippage beyond 25 basis points relative to arrival quotes. Estimated fees were $67.32; actual broker fees were unavailable. The displayed drawdown uses supplied observations, not a complete intraday maximum-drawdown series. No competition Sharpe is inferred from two observations.

The separate realistic historical backtest Sharpe is 1.17. It is not a competition result. Cloud role deployment, scheduled execution and Slack PAUSE/RESUME remain unverified in the local release lock. Actual total Qoder usage is not supplied.

## Rebuild after new evidence

Run `python -B -m dashboard.from_golive --account 5832812 --out <new snapshot path>` then `python -B -m dashboard.capture --snapshot <path> --out <new screenshot path>`. Use a new version suffix; preserve the delivered version. The converter requires local saved golive artifacts. It does not call a broker.

The updated local presentation is `docs/submission/site/index_v2.html`; it still needs publishing to the existing Qoder Sites URL. The form remains the entrant's responsibility.
