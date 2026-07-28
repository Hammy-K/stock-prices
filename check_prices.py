#!/usr/bin/env python3
"""
URTH / EQQQ dip-alert checker — runs hourly via GitHub Actions.
Fetches prices from Yahoo Finance, applies dip logic, and on a dip opens
a GitHub issue — GitHub then notifies by email/mobile push. No issue,
no noise.
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


def fetch_yahoo(symbol, range_="3mo"):
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


def send_alert(title, body):
    """Open a GitHub issue; GitHub emails/pushes the notification."""
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    mention = os.environ.get("MENTION_USER", "").strip()
    if mention:
        body += f"\n\ncc @{mention}"
    payload = json.dumps({"title": title, "body": body}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues", data=payload,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "price-dip-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status in (200, 201)


def evaluate_signals(price, history, cfg):
    """Return a list of human-readable reasons for every signal that fired.

    Three independent dip signals, each tunable per ticker in config.json:
    - rolling_low: price at/near the N-day low (catches absolute lows)
    - drawdown:    price fell >= X% from the N-day high (catches pullbacks
                   in uptrends that never make a new absolute low)
    - zscore:      price is K standard deviations below the N-day mean
                   (volatility-adjusted: a small drop in a calm market can
                   be more unusual than a big drop in a choppy one)
    """
    sig = cfg.get("signals", {})
    today = now_utc().date()
    past = [(d, c) for d, c in history if d < today]  # exclude partial bar
    fired = []

    def closes_within(days):
        cutoff = today - timedelta(days=days)
        return [(d, c) for d, c in past if d >= cutoff]

    rl = sig.get("rolling_low", {})
    if rl.get("enabled", True):
        days = rl.get("lookback_days", 7)
        w = closes_within(days)
        if len(w) >= rl.get("min_data_days", 4):
            low_date, low = min(w, key=lambda x: x[1])
            hi = max(c for _, c in w)
            prox = rl.get("proximity_pct", 0.5)
            # Range guard: in a flat market everything is "near the low",
            # so only fire if the window actually saw a meaningful drop.
            min_range = rl.get("min_range_pct", 1.5)
            if (hi >= low * (1 + min_range / 100)
                    and price <= low * (1 + prox / 100)):
                fired.append(f"rolling low — within {prox}% of the "
                             f"{days}-day low (${low:.2f} on {low_date})")

    dd = sig.get("drawdown", {})
    if dd.get("enabled", True):
        days = dd.get("lookback_days", 30)
        w = [c for _, c in closes_within(days)]
        if len(w) >= dd.get("min_data_days", 10):
            hi = max(w)
            drop = (hi - price) / hi * 100
            if drop >= dd.get("drop_pct", 3.0):
                fired.append(f"drawdown — {drop:.1f}% below the "
                             f"{days}-day high (${hi:.2f})")

    zs = sig.get("zscore", {})
    if zs.get("enabled", True):
        days = zs.get("lookback_days", 20)
        w = [c for _, c in closes_within(days)]
        if len(w) >= zs.get("min_data_days", 15):
            mean = sum(w) / len(w)
            std = (sum((x - mean) ** 2 for x in w) / len(w)) ** 0.5
            if std > 0:
                z = (price - mean) / std
                if z <= -zs.get("threshold", 2.0):
                    fired.append(f"z-score — {abs(z):.1f}σ below the "
                                 f"{days}-day average (${mean:.2f})")

    return fired


def evaluate_rebound(price, history, cfg, st):
    """Detect the turn off a trough during a decline episode.

    An episode starts when price falls >= min_drop_pct from the N-day peak.
    While it runs we track the lowest observed price (hourly granularity).
    When price bounces >= rebound_pct off that trough, fire ONCE — unless a
    new, deeper trough forms, which re-arms the signal. The episode ends
    when price recovers to within episode_end_recovery_pct of the peak.

    Returns a reason string when the rebound fires, else None. Mutates
    st["episode"].
    """
    rb = cfg.get("signals", {}).get("rebound", {})
    if not rb.get("enabled", True):
        return None
    lookback = rb.get("lookback_days", 30)
    min_drop = rb.get("min_drop_pct", 4.0)
    bounce = rb.get("rebound_pct", 1.5)
    recover = rb.get("episode_end_recovery_pct", 1.0)

    today = now_utc().date()
    cutoff = today - timedelta(days=lookback)
    past = [c for d, c in history if cutoff <= d < today]
    if len(past) < rb.get("min_data_days", 10):
        return None
    peak = max(past + [price])

    ep = st.get("episode") or {}
    if not ep.get("active"):
        drop = (peak - price) / peak * 100
        if drop >= min_drop:
            st["episode"] = {
                "active": True, "peak": peak, "trough": price,
                "trough_at": now_utc().isoformat(timespec="seconds"),
                "rebound_alerted": False}
        return None

    if price < ep["trough"]:
        ep["trough"] = price
        ep["trough_at"] = now_utc().isoformat(timespec="seconds")
        ep["rebound_alerted"] = False  # deeper trough re-arms the signal
        return None

    if price >= ep["peak"] * (1 - recover / 100):
        st["episode"] = {"active": False}  # recovered — episode over
        return None

    if (not ep.get("rebound_alerted")
            and price >= ep["trough"] * (1 + bounce / 100)):
        ep["rebound_alerted"] = True
        depth = (ep["peak"] - ep["trough"]) / ep["peak"] * 100
        up = (price - ep["trough"]) / ep["trough"] * 100
        return (f"price turned up {up:.1f}% from the trough of "
                f"${ep['trough']:.2f} ({depth:.1f}% below the "
                f"${ep['peak']:.2f} peak, trough seen {ep['trough_at']})")
    return None


def buy_context(price, history):
    """One reusable block that helps judge entry quality at a glance."""
    today = now_utc().date()
    closes = [c for d, c in history if d < today]
    if len(closes) < 10:
        return "_Not enough history for context._"
    hi, lo = max(closes), min(closes)
    span = hi - lo
    pos = (price - lo) / span * 100 if span else 50.0
    drop = (hi - price) / hi * 100
    mean = sum(closes) / len(closes)
    vs_avg = (price - mean) / mean * 100
    return (f"**Buy context (3-month window):** "
            f"{drop:.1f}% below the high (${hi:.2f}) · "
            f"sitting at the {pos:.0f}th percentile of the "
            f"${lo:.2f}–${hi:.2f} range · "
            f"{vs_avg:+.1f}% vs the 3-month average (${mean:.2f})")


def append_log(ticker, price, currency, source):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "ticker", "price", "currency", "source"])
        w.writerow([now_utc().isoformat(timespec="seconds"),
                    ticker, price, currency, source])


def main():
    if not os.environ.get("GITHUB_TOKEN") or not os.environ.get(
            "GITHUB_REPOSITORY"):
        print("GITHUB_TOKEN / GITHUB_REPOSITORY not set", file=sys.stderr)
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

        st = state.setdefault(ticker, {})

        # --- rebound (turn off the trough) — independent of dip alerts ---
        if mode == "auto":
            reb = evaluate_rebound(price, history, cfg, st)
            if reb:
                title = f"🟢 {ticker} rebound signal — ${price:.2f}"
                body = (f"**Possible bottom:** {reb}\n\n"
                        f"{buy_context(price, history)}\n\n"
                        f"A rebound signal means the decline paused and "
                        f"turned, not that it can't resume. If a new low "
                        f"forms, this signal re-arms and fires again on "
                        f"the next turn.")
                try:
                    if send_alert(title, body):
                        print(f"{ticker}: REBOUND ALERT SENT at ${price}")
                except Exception as e:  # noqa: BLE001
                    print(f"{ticker}: rebound issue failed: {e}",
                          file=sys.stderr)

        # --- trigger logic ---
        triggered, fired = False, []
        if mode == "manual":
            target = cfg.get("manual_target")
            if target is not None and price <= target:
                triggered = True
                fired = [f"manual target ${target}"]
        else:  # auto: evaluate independent signals; any one triggers
            fired = evaluate_signals(price, history, cfg)
            triggered = bool(fired)

        if not triggered:
            print(f"{ticker}: ${price} — no trigger")
            continue

        # --- dedup: no repeat within 24h unless dip deepened > 0.5% ---
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

        strength = f" ({len(fired)}/3 signals)" if mode == "auto" else ""
        title = f"📉 {ticker} dip alert — ${price:.2f}{strength}"
        reasons = "\n".join(f"- {r}" for r in fired)
        body = (f"**Signals fired:**\n\n{reasons}\n\n"
                f"{buy_context(price, history)}\n\n"
                f"**Price:** ${price:.2f} "
                f"(quote time {mkt_time:%Y-%m-%d %H:%M} UTC)\n\n"
                f"To change thresholds, edit `config.json` "
                f"(`mode: manual` + `manual_target`, or `mode: auto`). "
                f"Close this issue whenever — it has no effect on the bot.")
        try:
            sent = send_alert(title, body)
        except Exception as e:  # noqa: BLE001
            print(f"{ticker}: issue creation failed: {e}", file=sys.stderr)
            sent = False
        if sent:
            print(f"{ticker}: ALERT SENT at ${price}")
            st["last_alert_at"] = now_utc().isoformat(timespec="seconds")
            st["last_alert_price"] = price

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
