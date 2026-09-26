#!/usr/bin/env python3
"""Build the "Pizarra NFL" dashboard: a single self-contained HTML page with
standings, betting trends, per-team detail and player props from 2002 on.

Team and game data come from this repo's data/games.csv, data/standings.csv,
data/teams.csv and data/teamcolors.csv (standard library only).

Player data (weekly stats for QB/RB/WR/TE and the latest injury report) is
downloaded from nflverse releases with nflreadpy (`pip install nflreadpy`).
Without nflreadpy, or with --no-players, the page is built without the
player sections.

Usage:
    python3 code/pizarra/build_pizarra.py [-o OUTPUT] [--no-players]
"""
import argparse
import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent.parent / "data"
FIRST_SEASON = 2002  # divisions as realigned in 2002
PLAYER_FIRST_SEASON = 2021  # 17-game era
POSITIONS = ["QB", "RB", "WR", "TE"]
# weekly stat columns kept per player-game, in this order
STAT_COLS = [
    "completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions",
    "carries", "rushing_yards", "rushing_tds",
    "receptions", "targets", "receiving_yards", "receiving_tds",
]


def num(x):
    if x == "":
        return None
    f = float(x)
    return int(f) if f == int(f) else f


def rows(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_data():
    game_rows = [r for r in rows("games.csv") if int(r["season"]) >= FIRST_SEASON]
    games = [
        [
            int(r["season"]), r["game_type"], int(r["week"]), r["gameday"],
            r["away_team"], num(r["away_score"]), r["home_team"], num(r["home_score"]),
            num(r["spread_line"]), num(r["total_line"]),
            1 if r["location"] == "Neutral" else 0, int(r["overtime"] or 0),
        ]
        for r in game_rows
    ]
    standings = [
        [int(r["season"]), r["division"], r["team"], num(r["seed"]), r["playoff"]]
        for r in rows("standings.csv")
        if int(r["season"]) >= FIRST_SEASON
    ]
    names = {}
    for r in rows("teams.csv"):  # later seasons overwrite earlier ones
        names[r["team"]] = r["full"]
    colors = {r["team"]: r["color"] for r in rows("teamcolors.csv")}
    game_index = {r["game_id"]: i for i, r in enumerate(game_rows)}
    return {"g": games, "s": standings, "n": names, "c": colors}, game_index


def build_players(game_index):
    """Weekly QB/RB/WR/TE stats and the latest injury report, via nflreadpy."""
    try:
        import nflreadpy as nfl
        import polars as pl
    except ImportError:
        print("nflreadpy not installed: building without player data")
        return None

    current = nfl.get_current_season()
    stats = (
        nfl.load_player_stats(list(range(PLAYER_FIRST_SEASON, current + 1)), summary_level="week")
        .filter(pl.col("position").is_in(POSITIONS) & pl.col("game_id").is_in(list(game_index)))
        .sort(["season", "week"])
    )
    players, pidx, out = [], {}, []
    for r in stats.iter_rows(named=True):
        pid = r["player_id"]
        if pid not in pidx:
            pidx[pid] = len(players)
            players.append([pid, r["player_display_name"] or r["player_name"], r["position"]])
        out.append(
            [pidx[pid], game_index[r["game_id"]], r["team"], r["opponent_team"]]
            + [int(r[c] or 0) for c in STAT_COLS]
            + [round(r["target_share"] or 0, 3), round(r["air_yards_share"] or 0, 3)]
        )

    injuries = {}
    try:
        inj = nfl.load_injuries(current)
    except Exception as e:  # the file only exists once the season's reports start
        print(f"No injury report for {current}: {e}")
        inj = None
    if inj is not None and inj.height:
        # latest report of each team; older entries describe weeks already played
        latest = inj.group_by("team").agg(pl.col("week").max().alias("last"))
        inj = inj.join(latest, on="team").filter(pl.col("week") == pl.col("last"))
        for r in inj.iter_rows(named=True):
            i = pidx.get(r["gsis_id"])
            # skip players who are listed but practiced fully with no game status
            if i is None or (r["report_status"] is None and (r["practice_status"] or "Full").startswith("Full")):
                continue
            injuries[i] = [r["report_status"], r["report_primary_injury"], r["practice_status"], r["week"]]

    print(f"Player data: {len(players)} players, {len(out)} player-games, {len(injuries)} on the injury report")
    return {"cols": STAT_COLS, "pl": players, "r": out, "inj": injuries, "cur": current}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", type=Path, default=HERE / "pizarra-nfl.html")
    ap.add_argument("--no-players", action="store_true", help="skip the player data download")
    args = ap.parse_args()

    data, game_index = build_data()
    data["p"] = None if args.no_players else build_players(game_index)
    template = (HERE / "pizarra_template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    args.output.write_text(template.replace("__DATA__", payload), encoding="utf-8")
    print(f"Wrote {args.output} ({args.output.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
