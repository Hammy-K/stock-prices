# URTH / EQQQ Dip Alerts (GitHub Actions)

Hourly price checker that runs in the cloud — no laptop needed. When URTH or EQQQ (SIX) dips near its rolling low (or below a manual target), the workflow **opens a GitHub issue** in this repo, and GitHub notifies you by email / mobile push. No dip → no issue → no noise.

## How you get notified

- GitHub emails you when the bot opens an issue (it @mentions you, so notification is guaranteed).
- Optional: install the GitHub mobile app for push notifications.
- Anti-spam is built in: alerts fire only on an actual trigger, and each ticker is capped at one alert per 24h unless the dip deepens by >0.5%.

## Setup

Nothing to configure — no secrets needed. The workflow uses the repo's built-in token. Just check it runs: **Actions** tab → *Price dip check* → **Run workflow**, then look for per-ticker lines like `URTH: $202.88 — no trigger` in the log.

## Changing thresholds (from your phone or browser)

Edit `config.json` on github.com (pencil icon → commit). Takes effect next hourly run.

- Fixed target: `"mode": "manual", "manual_target": 197`
- Back to auto dip detection: `"mode": "auto"`
- Stop one ticker: `"mode": "paused"`

## Auto mode: three independent dip signals

Any enabled signal firing = one alert (the issue lists which ones fired — more signals agreeing = stronger dip):

| Signal | Fires when | Catches |
|---|---|---|
| `rolling_low` | price within `proximity_pct` of the N-day low (URTH 30d, EQQQ 7d) — only if the window actually dropped ≥ `min_range_pct` | absolute lows |
| `drawdown` | price ≥ `drop_pct` (3%) below the N-day high | pullbacks in uptrends that never make a new low |
| `zscore` | price ≥ `threshold` (2σ) below the N-day mean | statistically unusual drops — volatility-aware, so a small drop in a calm market can fire while the same drop in a choppy market stays silent |

Tune or disable each per ticker in `config.json` (`"enabled": false`).

## 🟢 Rebound signal ("has the dip bottomed?")

Separate from the 📉 dip alerts. Once a decline of ≥ `min_drop_pct` (4%) from the 30-day peak begins, the bot tracks the lowest price it observes (hourly, so it sees intraday troughs). When price turns up ≥ `rebound_pct` (1.5%) off that trough, it opens a 🟢 issue — the closest thing to a "bottom is in" alert. If a **new** low forms afterwards, the signal re-arms and fires again at the next turn. The episode closes when price recovers to within 1% of the peak.

The trade-off is explicit: you never buy the exact bottom (you pay ~1.5% for confirmation), but you stop catching falling knives. Both alert types include a **Buy context** line (drawdown from 3-month high, position in the 3-month range, distance from the 3-month average) to help judge entry quality at a glance.

## Notes

- Schedule: hourly 10:00–23:00 KSA time, Mon–Fri (GitHub cron can lag 5–15 min).
- Dip alerts land as issues — close them whenever, it doesn't affect the bot. They double as a dip history log.
- GitHub pauses schedules after 60 days of repo inactivity — the bot's own state commits count as activity, so this shouldn't happen while it's running.
- `price_log.csv` accumulates every reading — free audit trail.
