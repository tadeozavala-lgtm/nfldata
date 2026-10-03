#!/usr/bin/env python3
"""Download the coming NFL week's player prop lines from The Odds API
(https://the-odds-api.com) for the Pizarra's "Top 5" tab.

The key is read from the ODDS_API_KEY environment variable (a GitHub Actions
secret); it is never written to the output. The free plan has 500 credits a
month and each event costs one credit per prop market returned (US region),
so the script spends at most --budget credits per run and stops early when the
account's remaining credits would drop below --reserve.

The raw API responses are saved as they come (plus the fetch time and the
credits left), so the build can parse them without spending credits again.

Usage:
    ODDS_API_KEY=... python3 code/pizarra/fetch_odds.py -o nfl_odds.json
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("ODDS_API_BASE", "https://api.the-odds-api.com") + "/v4/sports/americanfootball_nfl"
DEFAULT_MARKETS = ("player_pass_yds,player_pass_tds,player_rush_yds,"
                   "player_receptions,player_reception_yds,player_anytime_td")


def get(path, **params):
    """GET an API path; returns (parsed JSON, remaining credits or None)."""
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "pizarra-nfl"})
    with urllib.request.urlopen(req, timeout=30) as r:
        left = r.headers.get("x-requests-remaining")
        return json.load(r), None if left is None else int(float(left))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--markets", default=os.environ.get("ODDS_MARKETS") or DEFAULT_MARKETS,
                    help="comma-separated prop market keys (default: %(default)s)")
    ap.add_argument("--days", type=int, default=8, help="events starting within this many days")
    ap.add_argument("--budget", type=int, default=120, help="most credits to spend in this run")
    ap.add_argument("--reserve", type=int, default=20, help="credits to leave in the account")
    args = ap.parse_args()
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        sys.exit("ODDS_API_KEY is not set")
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    fmt = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        events, left = get("/events", apiKey=key, commenceTimeFrom=fmt(now),
                           commenceTimeTo=fmt(now + dt.timedelta(days=args.days)))
    except urllib.error.HTTPError as e:  # 401: bad key; never print the URL, it holds the key
        sys.exit(f"The Odds API refused the events request: HTTP {e.code}")
    events.sort(key=lambda e: e["commence_time"])
    out, spent = [], 0
    for ev in events:
        if spent + len(markets) > args.budget or (left is not None and left - len(markets) < args.reserve):
            print(f"Stopping: {spent} credits spent, {left} left (budget {args.budget}, reserve {args.reserve})")
            break
        try:
            odds, left = get(f"/events/{ev['id']}/odds", apiKey=key, regions="us",
                             markets=",".join(markets), oddsFormat="american")
        except urllib.error.HTTPError as e:
            print(f"Skipping {ev['away_team']} @ {ev['home_team']}: HTTP {e.code}")
            continue
        spent += len({m["key"] for b in odds.get("bookmakers", []) for m in b.get("markets", [])})
        out.append(odds)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({"fetched": fmt(now), "remaining": left, "markets": markets, "events": out}, f, separators=(",", ":"))
    print(f"Saved {len(out)} of {len(events)} events to {args.output}; about {spent} credits spent, {left} left")


if __name__ == "__main__":
    main()
