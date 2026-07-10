# URTH / EQQQ Dip Alerts (GitHub Actions)

Hourly price checker that runs in the cloud — no laptop needed. Alerts to Slack when URTH or EQQQ (SIX) dips near its rolling low, or below a manual target.

## One-time setup (~10 min)

### 1. Create the Slack incoming webhook (~3 min)

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**.
2. Name: `price-dip-bot`, workspace: **noonerteam** → **Create App**.
3. In the left sidebar: **Incoming Webhooks** → toggle **Activate Incoming Webhooks** ON.
4. Click **Add New Webhook to Workspace** → pick where alerts go (your own DM: choose yourself, or a channel) → **Allow**.
5. Copy the webhook URL (`https://hooks.slack.com/services/T.../B.../...`).

### 2. Create the repo (~3 min)

1. Go to https://github.com/new → name: `price-dip-alerts`, visibility: **Private** → create.
2. Upload everything in this folder (drag-and-drop works: **Add file → Upload files**). Make sure `.github/workflows/price-check.yml` keeps its path — if uploading via web, create the file manually at that path with **Add file → Create new file** and paste the content.
3. Or from a terminal in this folder:
   ```bash
   git init -b main && git add -A && git commit -m "initial"
   git remote add origin git@github.com:<you>/price-dip-alerts.git
   git push -u origin main
   ```

### 3. Add the webhook secret (~1 min)

Repo → **Settings → Secrets and variables → Actions → New repository secret**
Name: `SLACK_WEBHOOK_URL` — Value: the webhook URL from step 1.

### 4. Test it

Repo → **Actions** tab → **Price dip check** → **Run workflow**. Check the run log — you should see per-ticker lines like `URTH: $202.88 — no trigger`.

## Changing thresholds (from your phone or browser)

Edit `config.json` on github.com (pencil icon → commit). Takes effect next hourly run.

- Fixed target: `"mode": "manual", "manual_target": 197`
- Back to auto dip detection: `"mode": "auto"`
- Stop one ticker: `"mode": "paused"`

Auto mode alerts when price comes within `auto_proximity_pct` (0.5%) of the rolling low (URTH: 30-day, EQQQ: 7-day).

## Notes

- Schedule: hourly 10:00–23:00 KSA time, Mon–Fri (GitHub cron can lag 5–15 min).
- Dedup: same-ticker alerts suppressed for 24h unless the dip deepens >0.5%.
- GitHub pauses schedules after 60 days of repo inactivity — the bot's own state commits count as activity, so this shouldn't happen while it's running.
- `price_log.csv` accumulates every reading — free audit trail.
