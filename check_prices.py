#!/usr/bin/env python3
"""
URTH / EQQQ dip-alert checker — runs hourly via GitHub Actions.
Fetches prices from Yahoo Finance, applies dip logic, posts to Slack webhook.
State persists via git commits (state.json, price_log.csv).
"""
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
LOG_FILE = "price_log.csv"

YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) price-dip-bot"}


def now_utc():
    return datetime.now(timezone.utc)


def fetch_yahoo(symbol, range_="2mo"):
    """Return (current_price, market_time_utc, [(date, close), ...]) or None."""
    for host in YAHOO_HOSTS:
        url = (f"https://{host}/v8/finance/chart/{symbol}"
               f"?range={range_}&interval=1d")
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            result = data["chart"]["result"][0]
            meta = result["meta"]
            price = meta["regularMarketPrice"]
            mkt_time = datetime.fromtimestamp(meta["regularMarketTime"],
                                              tz=timezone.utc)
            ts = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
            history = [
                (datetime.fromtimestamp(t, tz=timezone.utc).date(), c)
                for t, c in zip(ts, closes) if c is not None
            ]
            return price, mkt_time, history
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {host} failed for {symbol}: {e}", file=sys.stderr)
    return None


def send_slack(webhook_url, text):
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url, data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status == 200


def append_log(ticker, price, currency, source):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "ticker", "price", "currency", "source"])
        w.writerow([now_utc().isoformat(timespec="seconds"),
                    ticker, price, currency, source])


def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("SLACK_WEBHOOK_URL not set", file=sys.stderr)
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        config = json.load(f)
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)

    for ticker, cfg in config["tickers"].items():
        mode = cfg.get("mode", "auto")
        if mode == "paused":
            print(f"{ticker}: paused, skipping")
            continue

        res = fetch_yahoo(cfg["symbol"])
        if res is None:
            print(f"{ticker}: all price sources failed")
            continue
        price, mkt_time, history = res

        # Skip if the quote is stale (market closed for > staleness window)
        max_age_h = cfg.get("max_quote_age_hours", 3)
        age_h = (now_utc() - mkt_time).total_seconds() / 3600
        fresh = age_h <= max_age_h

        append_log(ticker, price, cfg.get("currency", "USD"),
                   f"yahoo:{cfg['symbol']}")

        if not fresh:
            print(f"{ticker}: quote is {age_h:.1f}h old (market closed) — "
                  f"logged, no alert check")
            continue

        # --- trigger logic ---
        triggered, reason = False, ""
        if mode == "manual":
            target = cfg.get("manual_target")
            if target is not None and price <= target:
                triggered = True
                reason = f"manual target ${target}"
        else:  # auto
            lookback = cfg.get("auto_lookback_days", 7)
            prox = cfg.get("auto_proximity_pct", 0.5)
            cutoff = now_utc().date() - timedelta(days=lookback)
            window = [(d, c) for d, c in history if d >= cutoff]
            # exclude today's (partial) bar from the reference low
            ref = [(d, c) for d, c in window if d < now_utc().date()]
            if len(ref) < cfg.get("min_data_days", 4):
                print(f"{ticker}: only {len(ref)} days of history in window "
                      f"— skipping auto check")
                continue
            low_date, low = min(ref, key=lambda x: x[1])
            if price <= low * (1 + prox / 100):
                triggered = True
                avg = sum(c for _, c in ref) / len(ref)
                below_avg = (avg - price) / avg * 100
                reason = (f"within {prox}% of the {lookback}-day low "
                          f"(${low:.2f} on {low_date}) — "
                          f"{below_avg:.1f}% below the {lookback}-day average")

        if not triggered:
            print(f"{ticker}: ${price} — no trigger")
            continue

        # --- dedup: no repeat within 24h unless dip deepened > 0.5% ---
        st = state.setdefault(ticker, {})
        last_at = st.get("last_alert_at")
        last_price = st.get("last_alert_price")
        if last_at:
            last_dt = datetime.fromisoformat(last_at)
            within_24h = now_utc() - last_dt < timedelta(hours=24)
            deeper = (last_price is not None
                      and price < last_price * 0.995)
            if within_24h and not deeper:
                print(f"{ticker}: trigger suppressed (alerted "
                      f"{last_at}, price not >0.5% lower)")
                continue

        msg = (f":chart_with_downwards_trend: *{ticker} dip alert — "
               f"${price:.2f}*\n"
               f"Trigger: {reason}\n"
               f"_To change: edit config.json in the repo "
               f"(mode: manual + manual_target, or mode: auto)._")
        if send_slack(webhook, msg):
            print(f"{ticker}: ALERT SENT at ${price}")
            st["last_alert_at"] = now_utc().isoformat(timespec="seconds")
            st["last_alert_price"] = price
        else:
            print(f"{ticker}: Slack webhook failed", file=sys.stderr)

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
