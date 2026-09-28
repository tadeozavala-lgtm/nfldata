#!/usr/bin/env python3
"""Build the "Pizarra NFL" dashboard: a single self-contained HTML page with
standings, betting trends, per-team detail and player props from 2002 on.

Games (scores, lines, weather, referees) come from nflverse's schedule release
through nflreadpy, which is the same table as this repo's data/games.csv but
does not depend on this fork being synced; data/games.csv is the fallback.
Standings, team names and colors come from data/standings.csv, data/teams.csv
and data/teamcolors.csv.

Player data (weekly stats for QB/RB/WR/TE with snap share and expected
production, the latest injury report and depth charts), per-game team
passing/rushing yards and play-by-play efficiency (EPA, success rate, red
zone, third downs, pass rate over expected) are downloaded from nflverse
releases with nflreadpy (`pip install nflreadpy`). Without nflreadpy, or with
--no-players, the page is built offline from the local CSVs only, without
those sections.

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


def schedule_rows(remote):
    """Games from nflverse's schedule release (the same table as data/games.csv, but it
    doesn't wait for this fork to sync); falls back to the local CSV when unavailable."""
    if remote:
        try:
            import nflreadpy as nfl
            sched = nfl.load_schedules(True)
            # same shape as csv.DictReader rows: every value a string, missing values ""
            out = [{k: "" if v is None else str(v) for k, v in r.items()} for r in sched.iter_rows(named=True)]
            last = max((r["gameday"] for r in out if r["home_score"]), default="?")
            print(f"Schedule: {len(out)} games from nflverse (last played {last})")
            return out
        except Exception as e:  # ImportError, network, ...
            print(f"Schedule from nflverse unavailable ({e}); using data/games.csv")
    return rows("games.csv")


def build_data(remote=True):
    game_rows = [r for r in schedule_rows(remote) if int(r["season"]) >= FIRST_SEASON]
    games = [
        [
            int(r["season"]), r["game_type"], int(r["week"]), r["gameday"],
            r["away_team"], num(r["away_score"]), r["home_team"], num(r["home_score"]),
            num(r["spread_line"]), num(r["total_line"]),
            1 if r["location"] == "Neutral" else 0, int(r["overtime"] or 0),
            # game context: stadium roof, weather (°F, mph), referee, days of rest, divisional
            r["roof"], num(r["temp"]), num(r["wind"]), r["referee"],
            num(r["away_rest"]), num(r["home_rest"]), int(r["div_game"] or 0),
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
    seasons = list(range(PLAYER_FIRST_SEASON, current + 1))
    stats = (
        nfl.load_player_stats(seasons, summary_level="week")
        .filter(pl.col("position").is_in(POSITIONS) & pl.col("game_id").is_in(list(game_index)))
        .sort(["season", "week"])
    )
    snaps = load_snap_pct(nfl, pl, seasons)
    expected, xcols = load_expected(nfl, seasons)
    players, pidx, out = [], {}, []
    for r in stats.iter_rows(named=True):
        pid = r["player_id"]
        if pid not in pidx:
            pidx[pid] = len(players)
            players.append([pid, r["player_display_name"] or r["player_name"], r["position"]])
        key = (r["game_id"], pid)
        out.append(
            [pidx[pid], game_index[r["game_id"]], r["team"], r["opponent_team"]]
            + [int(r[c] or 0) for c in STAT_COLS]
            + [round(r["target_share"] or 0, 3), round(r["air_yards_share"] or 0, 3)]
            + [snaps.get(key), expected.get(key)]
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

    depth = load_depth(nfl, pl, current, pidx)
    print(f"Player data: {len(players)} players, {len(out)} player-games, {len(injuries)} on the injury report, "
          f"{sum(1 for r in out if r[-2] is not None)} with snaps, {sum(1 for r in out if r[-1] is not None)} with "
          f"expected stats, {len(depth)} on current depth charts")
    return {"cols": STAT_COLS, "xcols": xcols, "pl": players, "r": out, "inj": injuries, "depth": depth, "cur": current}


def load_snap_pct(nfl, pl, seasons):
    """Offensive snap share (0-100) by (game_id, gsis_id). PFR ids are mapped to gsis via load_players."""
    try:
        sc = nfl.load_snap_counts([s for s in seasons if s >= 2012])
        ids = nfl.load_players()
    except Exception as e:
        print(f"No snap counts: {e}")
        return {}
    pfr_col = next((c for c in ("pfr_id", "pfr_player_id") if c in ids.columns), None)
    if pfr_col is None or "gsis_id" not in ids.columns:
        print("No PFR-to-gsis id mapping: building without snap counts")
        return {}
    to_gsis = dict(ids.select([pfr_col, "gsis_id"]).drop_nulls().iter_rows())
    out = {}
    for game_id, pfr_id, pct in sc.select(["game_id", "pfr_player_id", "offense_pct"]).iter_rows():
        gsis = to_gsis.get(pfr_id)
        if gsis and pct is not None:
            out[(game_id, gsis)] = round(pct * 100 if pct <= 1 else pct)
    return out


# expected production from ffverse's opportunity model (what the player's usage "should" yield)
EXP_COLS = [
    "pass_yards_gained_exp", "pass_touchdown_exp", "pass_completions_exp", "receptions_exp",
    "rec_yards_gained_exp", "rush_yards_gained_exp", "rush_touchdown_exp", "rec_touchdown_exp",
]


def load_expected(nfl, seasons):
    frames = []
    for s in seasons:  # the current season's file appears a few weeks in; skip what is missing
        try:
            frames.append(nfl.load_ff_opportunity(s))
        except Exception as e:
            print(f"No expected-opportunity data for {s}: {e}")
    frames = [f for f in frames if {"game_id", "player_id"} <= set(f.columns)]
    if not frames:
        return {}, []
    cols = [c for c in EXP_COLS if all(c in f.columns for f in frames)]
    out = {}
    for f in frames:
        for r in f.select(["game_id", "player_id"] + cols).iter_rows():
            out[(r[0], r[1])] = [None if v is None else round(float(v), 1) for v in r[2:]]
    return out, cols


def load_depth(nfl, pl, current, pidx):
    """Position and rank on each team's most recent depth chart, e.g. ["WR", 1]."""
    try:
        dc = nfl.load_depth_charts(current)
    except Exception as e:
        print(f"No depth charts for {current}: {e}")
        return {}
    if not {"dt", "team", "gsis_id", "pos_abb", "pos_rank"} <= set(dc.columns):
        print("Unexpected depth chart format: building without depth charts")
        return {}
    dc = dc.filter(pl.col("pos_abb").is_in(POSITIONS) & pl.col("gsis_id").is_not_null())
    latest = dc.group_by("team").agg(pl.col("dt").max().alias("last"))
    dc = dc.join(latest, on="team").filter(pl.col("dt") == pl.col("last"))
    out = {}
    for gsis, pos, rank in dc.select(["gsis_id", "pos_abb", "pos_rank"]).iter_rows():
        i = pidx.get(gsis)
        if i is not None and rank is not None and (i not in out or int(rank) < out[i][1]):
            out[i] = [pos, int(rank)]
    return out


def build_team_stats(game_index):
    """Net passing and rushing yards of every team in every game, via nflreadpy."""
    try:
        import nflreadpy as nfl
        import polars as pl
    except ImportError:
        print("nflreadpy not installed: building without team offense/defense rankings")
        return None
    current = nfl.get_current_season()
    ts = nfl.load_team_stats(list(range(FIRST_SEASON, current + 1)), summary_level="week").filter(
        pl.col("game_id").is_in(list(game_index))
    )
    # sack_yards_lost is stored as a negative number; net passing = gross passing minus sack yards
    out = [
        [game_index[r["game_id"]], r["team"],
         int((r["passing_yards"] or 0) - abs(r["sack_yards_lost"] or 0)), int(r["rushing_yards"] or 0)]
        for r in ts.iter_rows(named=True)
    ]
    print(f"Team stats: {len(out)} team-games")
    return out


def build_efficiency(game_index):
    """Per team-game efficiency from play-by-play: EPA and success on dropbacks and designed
    runs, third-down conversions, red-zone trips and touchdowns, and early-down neutral
    pass rate over expected. Seasons are loaded one at a time to keep memory low."""
    try:
        import nflreadpy as nfl
        import polars as pl
    except ImportError:
        print("nflreadpy not installed: building without efficiency (EPA) data")
        return None
    current = nfl.get_current_season()
    ids = list(game_index)
    out = []
    for season in range(FIRST_SEASON, current + 1):
        try:
            pbp = nfl.load_pbp(season)
        except Exception as e:
            print(f"No play-by-play for {season}: {e}")
            continue
        p = pbp.filter(pl.col("game_id").is_in(ids) & pl.col("posteam").is_not_null())
        plays = p.filter(pl.col("play_type").is_in(["pass", "run"]) & pl.col("epa").is_not_null())
        if "two_point_attempt" in plays.columns:
            plays = plays.filter(pl.col("two_point_attempt").fill_null(0) == 0)
        dropback, run = pl.col("pass") == 1, pl.col("rush") == 1
        third = pl.col("down") == 3
        neutral = pl.col("down").is_in([1, 2]) & pl.col("wp").is_between(0.2, 0.8) & pl.col("pass_oe").is_not_null()
        agg = plays.group_by(["game_id", "posteam"]).agg(
            dropback.sum().alias("pn"), pl.col("epa").filter(dropback).sum().alias("pe"),
            pl.col("success").filter(dropback).sum().alias("ps"),
            run.sum().alias("rn"), pl.col("epa").filter(run).sum().alias("re"),
            pl.col("success").filter(run).sum().alias("rs"),
            third.sum().alias("a3"),
            (third & ((pl.col("first_down") == 1) | (pl.col("touchdown") == 1))).sum().alias("c3"),
            neutral.sum().alias("on"), pl.col("pass_oe").filter(neutral).sum().alias("oe"),
        )
        # a red-zone trip is a drive with a snap at or inside the opponent's 20
        drives = p.filter(pl.col("fixed_drive").is_not_null()).group_by(["game_id", "posteam", "fixed_drive"]).agg(
            (pl.col("yardline_100").min() <= 20).alias("rz"),
            (pl.col("fixed_drive_result").first() == "Touchdown").alias("td"),
        )
        rz = drives.group_by(["game_id", "posteam"]).agg(
            pl.col("rz").sum().alias("rzt"), (pl.col("rz") & pl.col("td")).sum().alias("rztd"))
        agg = agg.join(rz, on=["game_id", "posteam"], how="left")
        for r in agg.iter_rows(named=True):
            out.append([
                game_index[r["game_id"]], r["posteam"],
                r["pn"], round(r["pe"] or 0, 1), int(r["ps"] or 0),
                r["rn"], round(r["re"] or 0, 1), int(r["rs"] or 0),
                r["a3"], r["c3"], r["on"], round(r["oe"] or 0, 1), r["rzt"] or 0, r["rztd"] or 0,
            ])
        del pbp, p, plays
    print(f"Efficiency: {len(out)} team-games")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", type=Path, default=HERE / "pizarra-nfl.html")
    ap.add_argument("--no-players", action="store_true",
                    help="skip all nflverse downloads: local CSVs only, no player/ranking/EPA sections")
    args = ap.parse_args()

    data, game_index = build_data(remote=not args.no_players)
    data["p"] = None if args.no_players else build_players(game_index)
    data["t"] = None if args.no_players else build_team_stats(game_index)
    data["e"] = None if args.no_players else build_efficiency(game_index)
    template = (HERE / "pizarra_template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    args.output.write_text(template.replace("__DATA__", payload), encoding="utf-8")
    print(f"Wrote {args.output} ({args.output.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
