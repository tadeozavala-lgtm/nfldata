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
passing/rushing yards, play-by-play efficiency (EPA, success rate, red zone,
third downs, pass rate over expected), targets and carries by field zone and
targeted routes are downloaded from nflverse releases with nflreadpy
(`pip install nflreadpy`). Without nflreadpy, or with
--no-players, the page is built offline from the local CSVs only, without
those sections.

Usage:
    python3 code/pizarra/build_pizarra.py [-o OUTPUT] [--no-players]
"""
import argparse
import bisect
import csv
import datetime as dt
import json
import math
import unicodedata
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
    last_season = max(r["season"] for r in game_rows)
    games = [
        [
            int(r["season"]), r["game_type"], int(r["week"]), r["gameday"],
            r["away_team"], num(r["away_score"]), r["home_team"], num(r["home_score"]),
            num(r["spread_line"]), num(r["total_line"]),
            1 if r["location"] == "Neutral" else 0, int(r["overtime"] or 0),
            # game context: stadium roof, weather (°F, mph), referee, days of rest, divisional
            r["roof"], num(r["temp"]), num(r["wind"]), r["referee"],
            num(r["away_rest"]), num(r["home_rest"]), int(r["div_game"] or 0),
            # kickoff (Eastern time) and venue for the next-game card: only the latest season needs them
            *((r["gametime"], r["stadium"], r["surface"]) if r["season"] == last_season else ()),
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


_PFR_TO_GSIS = None


def pfr_to_gsis(nfl):
    """Pro Football Reference player id -> NFL gsis id, from nflverse's players table (cached)."""
    global _PFR_TO_GSIS
    if _PFR_TO_GSIS is None:
        _PFR_TO_GSIS = {}
        try:
            ids = nfl.load_players()
            pfr_col = next((c for c in ("pfr_id", "pfr_player_id") if c in ids.columns), None)
            if pfr_col and "gsis_id" in ids.columns:
                _PFR_TO_GSIS = dict(ids.select([pfr_col, "gsis_id"]).drop_nulls().iter_rows())
        except Exception as e:
            print(f"No PFR-to-gsis id mapping: {e}")
    return _PFR_TO_GSIS


def load_snap_pct(nfl, pl, seasons):
    """Offensive snap share (0-100) by (game_id, gsis_id). PFR ids are mapped to gsis via load_players."""
    try:
        sc = nfl.load_snap_counts([s for s in seasons if s >= 2012])
    except Exception as e:
        print(f"No snap counts: {e}")
        return {}
    to_gsis = pfr_to_gsis(nfl)
    if not to_gsis:
        print("No PFR-to-gsis id mapping: building without snap counts")
        return {}
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


def build_efficiency(game_index, on_season=None):
    """Per team-game efficiency from play-by-play: EPA and success on dropbacks and designed
    runs, third-down conversions, red-zone trips and touchdowns, and early-down neutral
    pass rate over expected. Seasons are loaded one at a time to keep memory low; each
    season's plays are also handed to on_season(season, plays), if given."""
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
        if on_season:
            on_season(season, p)
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


# prop markets (same definitions as MK in the template): eligible positions and per-game value
MARKETS = {
    "passing_yards": (("QB",), lambda s: s["passing_yards"]),
    "passing_tds": (("QB",), lambda s: s["passing_tds"]),
    "completions": (("QB",), lambda s: s["completions"]),
    "attempts": (("QB",), lambda s: s["attempts"]),
    "interceptions": (("QB",), lambda s: s["passing_interceptions"]),
    "pass_rush": (("QB",), lambda s: s["passing_yards"] + s["rushing_yards"]),
    "rushing_yards": (("QB", "RB", "WR"), lambda s: s["rushing_yards"]),
    "carries": (("QB", "RB"), lambda s: s["carries"]),
    "receptions": (("RB", "WR", "TE"), lambda s: s["receptions"]),
    "receiving_yards": (("RB", "WR", "TE"), lambda s: s["receiving_yards"]),
    "rush_rec": (("RB", "WR", "TE"), lambda s: s["rushing_yards"] + s["receiving_yards"]),
    "anytime_td": (("QB", "RB", "WR", "TE"), lambda s: s["rushing_tds"] + s["receiving_tds"]),
}
MATCH_WINDOW, MATCH_MIN = 10, 4  # defense's previous games used for the grade, and the minimum needed


def grade_of(rank, n):
    """A: ranks 21-32 (allows the most), B: 11-20, C: 1-10, scaled to a 32-team league."""
    r = -(-rank * 32 // n)
    return "A" if r >= 21 else "B" if r >= 11 else "C"


def build_matchup_validation(games, pdata, hooks=()):
    """Backtest of the matchup grade. For every player-game since 2021 the opponent is graded
    with only its previous MATCH_WINDOW games (no look-ahead), the line is the dashboard's
    suggested line (median of the player's previous 10 games, rounded to .5; 0.5 for TDs), and
    we count how often the player went over, by grade, by market and by implied-total modifier.
    Each of `hooks` (PlayDetail, PropModel) also records every graded player-game and market
    with record(player, previous rows, row, market, grade, value, line, implied total) and
    adds its summary() to the result under its `key`."""
    rows, pos_of = pdata["r"], [p[2] for p in pdata["pl"]]
    n_stats = len(STAT_COLS)
    stat = lambda r: dict(zip(STAT_COLS, r[4:4 + n_stats]))
    # production allowed by each defense in each game, by position of the opposing player
    allowed = {}
    for r in rows:
        g = games[r[1]]
        if g[7] is None:
            continue
        d = allowed.setdefault((r[1], r[3]), {}).setdefault(pos_of[r[0]], dict.fromkeys(STAT_COLS, 0))
        for c, v in zip(STAT_COLS, r[4:4 + n_stats]):
            d[c] += v
    team_games = {}
    for gi, t in allowed:
        team_games.setdefault(t, set()).add(gi)
    team_games = {t: sorted(gs, key=lambda gi: games[gi][3]) for t, gs in team_games.items()}
    # one snapshot per week: each defense's per-game production allowed over its previous games
    weeks = {}
    for gi, g in enumerate(games):
        if g[7] is not None and g[0] >= PLAYER_FIRST_SEASON:
            k = (g[0], g[1] != "REG", g[2])
            weeks[k] = min(weeks.get(k, g[3]), g[3])
    grades = {}  # (week key, defense, market, position) -> grade
    for k, as_of in weeks.items():
        per_team = {}
        for t, gs in team_games.items():
            prev = [gi for gi in gs if games[gi][3] < as_of][-MATCH_WINDOW:]
            if len(prev) >= MATCH_MIN:
                per_team[t] = prev
        if len(per_team) < 30:
            continue
        for m, (poss, f) in MARKETS.items():
            for pos in poss:
                vals = {t: sum(f(allowed[(gi, t)].get(pos, dict.fromkeys(STAT_COLS, 0))) for gi in prev) / len(prev)
                        for t, prev in per_team.items()}
                order = sorted(vals, key=vals.get)
                for i, t in enumerate(order):
                    grades[(k, t, m, pos)] = grade_of(i + 1, len(order))
    # player-games against those grades, with the suggested line from the player's own history
    by_player = {}
    for r in rows:
        if games[r[1]][7] is not None:
            by_player.setdefault(r[0], []).append(r)
    res, mods = {}, {}
    for pi, prs in by_player.items():
        prs.sort(key=lambda r: games[r[1]][3])
        pos = pos_of[pi]
        for i, r in enumerate(prs):
            if i < 5:
                continue
            g = games[r[1]]
            wk = (g[0], g[1] != "REG", g[2])
            s, prior = stat(r), [stat(x) for x in prs[max(0, i - 10):i]]
            home = g[6] == r[2]
            team_line = None if g[8] is None else (-g[8] if home else g[8])
            implied = None if team_line is None or g[9] is None else g[9] / 2 - team_line / 2
            mod = "" if implied is None else "+" if implied >= 25 else "-" if implied <= 18 else ""
            for m, (poss, f) in MARKETS.items():
                if pos not in poss:
                    continue
                grade = grades.get((wk, r[3], m, pos))
                if grade is None:
                    continue
                vals = sorted(f(x) for x in prior)
                mid = len(vals) // 2
                med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
                line = 0.5 if m == "anytime_td" else int(med) + 0.5
                v = f(s)
                for h in hooks:
                    h.record(pi, prs[:i], r, m, grade, v, line, implied)
                for key in (m, "all"):
                    c = res.setdefault(key, {}).setdefault(grade, [0, 0, 0.0])
                    c[0] += 1
                    c[1] += v > line
                    c[2] += v - line
                if m != "interceptions":
                    c = mods.setdefault(grade + mod, [0, 0])
                    c[0] += 1
                    c[1] += v > line
    for d in res.values():
        for c in d.values():
            c[2] = round(c[2], 1)
    last = max(games[r[1]][0] for r in rows)
    print(f"Matchup validation: {res.get('all')}, modifier {mods}")
    out = {"res": res, "mod": mods, "from": PLAYER_FIRST_SEASON, "to": last, "window": MATCH_WINDOW}
    for h in hooks:
        out[h.key] = h.summary()
    return out


def build_logos():
    """Team logos embedded as data URIs (the published page can't load images from other
    sites). URLs come from nflverse's teams table; ESPN serves them resized to 64x64.
    Teams whose logo can't be fetched keep the team-color swatch in the page."""
    try:
        import base64
        import nflreadpy as nfl
        import requests
        teams = nfl.load_teams()
    except Exception as e:
        print(f"No team logos: {e}")
        return None
    out = {}
    for abbr, url in teams.select(["team_abbr", "team_logo_espn"]).iter_rows():
        if not url or abbr in out:
            continue
        # ESPN's image combiner returns the same logo already resized (a few KB instead of ~40)
        src = f"https://a.espncdn.com/combiner/i?img={url.split('espncdn.com', 1)[1]}&h=64&w=64" if "espncdn.com" in url else url
        try:
            r = requests.get(src, timeout=20)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").split(";")[0]
            if not ctype.startswith("image/"):
                raise ValueError(f"not an image ({ctype})")
            out[abbr] = f"data:{ctype};base64,{base64.b64encode(r.content).decode()}"
        except Exception as e:
            print(f"No logo for {abbr}: {e}")
    print(f"Logos: {len(out)} teams, {sum(len(v) for v in out.values()) // 1024} KB")
    return out or None


# defensive depth-chart slots shown in the "Cobertura rival" block
DEF_SLOTS = ["LCB", "RCB", "NB", "FS", "SS", "MLB", "WLB", "SLB", "LILB", "RILB"]


def build_coverage(game_index):
    """Who covers for each defense and how well: current depth-chart starters in the secondary
    and at linebacker, their PFR coverage numbers (current and previous season), their injury
    status, and each defense's man-coverage rate from the latest participation season."""
    try:
        import nflreadpy as nfl
        import polars as pl
    except ImportError:
        return None
    current = nfl.get_current_season()
    # starters: rank 1 at each slot on each team's latest depth chart
    try:
        dc = nfl.load_depth_charts(current)
    except Exception as e:
        print(f"No depth charts for coverage: {e}")
        return None
    if not {"dt", "team", "gsis_id", "pos_abb", "pos_rank", "player_name"} <= set(dc.columns):
        return None
    dc = dc.filter(pl.col("pos_abb").is_in(DEF_SLOTS) & pl.col("gsis_id").is_not_null())
    latest = dc.group_by("team").agg(pl.col("dt").max().alias("last"))
    dc = dc.join(latest, on="team").filter(pl.col("dt") == pl.col("last")).sort("pos_rank")
    slots, names = {}, {}
    for team, slot, gsis, nm, rank in dc.select(["team", "pos_abb", "gsis_id", "player_name", "pos_rank"]).iter_rows():
        taken = slots.setdefault(team, {})
        if slot not in taken:
            taken[slot] = gsis
            names[gsis] = nm
    # coverage stats by defender and season (PFR weekly advanced defense, summed)
    to_gsis = pfr_to_gsis(nfl)
    stats = {}
    for season in (current - 1, current):
        try:
            d = nfl.load_pfr_advstats(season, stat_type="def", summary_level="week")
        except Exception as e:
            print(f"No PFR coverage stats for {season}: {e}")
            continue
        cols = ["def_targets", "def_completions_allowed", "def_yards_allowed", "def_receiving_td_allowed", "def_ints"]
        if not set(cols) <= set(d.columns):
            continue
        agg = d.group_by("pfr_player_id").agg(pl.len().alias("g"), *[pl.col(c).fill_null(0).sum() for c in cols])
        for r in agg.iter_rows():
            gsis = to_gsis.get(r[0])
            if gsis:
                stats.setdefault(gsis, {})[season] = [int(v) for v in r[1:]]
    # injury status of the starters (latest report of each team)
    inj = {}
    try:
        ir = nfl.load_injuries(current)
        last = ir.group_by("team").agg(pl.col("week").max().alias("last"))
        ir = ir.join(last, on="team").filter(pl.col("week") == pl.col("last"))
        for gsis, st, part in ir.select(["gsis_id", "report_status", "report_primary_injury"]).iter_rows():
            if gsis in names and st:
                inj[gsis] = [st, part]
    except Exception as e:
        print(f"No injury report for coverage: {e}")
    # man-coverage share of each defense on the latest season with participation data
    man, man_season = {}, None
    for season in (current, current - 1):
        try:
            p = nfl.load_participation(season)
        except Exception:
            continue
        p = p.filter(pl.col("defense_man_zone_type").is_in(["MAN_COVERAGE", "ZONE_COVERAGE"]))
        counts = {}
        for gid, pos, mz in p.select(["nflverse_game_id", "possession_team", "defense_man_zone_type"]).iter_rows():
            _, _, away, home = gid.split("_")
            c = counts.setdefault(home if pos == away else away, [0, 0])
            c[1] += 1
            if mz == "MAN_COVERAGE":
                c[0] += 1
        man = {t: [round(m / n, 3), n] for t, (m, n) in counts.items() if n}
        man_season = season
        break
    defenders = {g: [names[g], stats.get(g, {}), inj.get(g)] for g in names}
    print(f"Coverage: {len(slots)} defenses, {len(defenders)} starters, man/zone from {man_season}")
    return {"slots": slots, "d": defenders, "man": man, "manSeason": man_season, "seasons": [current - 1, current]}


# ---------- play detail for the field chart: pass zones, run gaps and routes ----------
# pass zones from the offense's view: depth (short < 15 air yards, deep) x side (left, middle, right)
PASS_ZONES = {("short", "left"): 0, ("short", "middle"): 1, ("short", "right"): 2,
              ("deep", "left"): 3, ("deep", "middle"): 4, ("deep", "right"): 5}
# designed-run gaps from the offense's view: outside left (end), off tackle left, inside the
# tackles (guards and middle), off tackle right, outside right
RUN_ZONES = {("left", "end"): 0, ("left", "tackle"): 1, ("left", "guard"): 2, ("middle", None): 2,
             ("right", "guard"): 2, ("right", "tackle"): 3, ("right", "end"): 4}
# kinds of opportunity: targets of a RB/WR/TE ("rec"), pass attempts of a QB ("qb"), RB carries ("run")
ZONE_N = {"rec": 6, "qb": 6, "run": 5}
ZONE_K = {"rec": 25, "qb": 40, "run": 30}  # opportunities of league average added to a defense's zone (shrinkage)
ZONE_TIERS = (0.95, 1.05)  # zone fit below / above: "−" / "+" tier in the backtest
ZONE_POS = {"rec": ("RB", "WR", "TE"), "run": ("RB",), "qb": ("QB",)}
ZONE_MARKETS = {"receptions": "rec", "receiving_yards": "rec", "rush_rec": None, "rushing_yards": "run",
                "carries": "run", "passing_yards": "qb", "completions": "qb", "attempts": "qb", "pass_rush": "qb"}


def zone_kind(market, pos):
    """Kind of opportunity a market depends on for a position, or None (no zone data)."""
    k = ZONE_MARKETS.get(market, "-")
    if k is None:
        k = "run" if pos == "RB" else "rec"
    return k if pos in ZONE_POS.get(k, ()) else None


def zone_fit(use, allowed, league, kind):
    """Yards per opportunity the defense allows in the zones the player uses, over what the
    league allows in those same zones (1.10 = 10% more). Each zone of the defense is shrunk
    toward the league's with ZONE_K opportunities. Arrays are flat [n, c, y] per zone."""
    k, num, den = ZONE_K[kind], 0.0, 0.0
    for z in range(ZONE_N[kind]):
        w, ln = use[3 * z], league[3 * z]
        if not w or not ln:
            continue
        lr = league[3 * z + 2] / ln
        num += w * (allowed[3 * z + 2] + k * lr) / (allowed[3 * z] + k)
        den += w * lr
    return num / den if den else None


class PlayDetail:
    """Collects, from each season of play-by-play that build_efficiency loads, the targets of
    every RB/WR/TE and pass attempts of every QB by pass zone, the designed runs of every RB by
    gap, and (from the participation data's FTN charting) the route of every target. Gives the
    page each player's zones over his last 10 games, what each defense allowed by zone to each
    position over its last 10, league rates and route trees; and backtests the zone fit."""

    def __init__(self, nfl, pl, games, pdata, game_index):
        self.nfl, self.pl, self.games, self.gidx, self.current = nfl, pl, games, game_index, pdata["cur"]
        self.pid = {p[0]: i for i, p in enumerate(pdata["pl"])}
        self.pos = [p[2] for p in pdata["pl"]]
        self.use = {}      # (pidx, gi, kind) -> flat [n, c, y] by zone
        self.allowed = {}  # (gi, defense, "POS.kind") -> flat [n, c, y] by zone
        self.routes = {}   # season -> {(pidx, route): [n, c, y]}
        self.tiers = {}

    def _add(self, gi, gsis, defteam, kind, z, n, c, y):
        i = self.pid.get(gsis)
        if i is None or self.pos[i] not in ZONE_POS[kind]:
            return
        for arr in (self.use.setdefault((i, gi, kind), [0] * 3 * ZONE_N[kind]),
                    self.allowed.setdefault((gi, defteam, f"{self.pos[i]}.{kind}"), [0] * 3 * ZONE_N[kind])):
            arr[3 * z] += n
            arr[3 * z + 1] += c
            arr[3 * z + 2] += y

    def __call__(self, season, p):
        if season < PLAYER_FIRST_SEASON - 1:  # one season before the backtest, for its first windows
            return
        pl, col = self.pl, self.pl.col
        p = p.filter(col("two_point_attempt").fill_null(0) == 0) if "two_point_attempt" in p.columns else p
        tg = p.filter((col("pass_attempt") == 1) & (col("sack").fill_null(0) == 0) & col("receiver_player_id").is_not_null()
                      & col("pass_length").is_not_null() & col("pass_location").is_not_null())
        keys = ["game_id", "defteam", "pass_length", "pass_location"]
        stats = [pl.len().alias("n"), col("complete_pass").fill_null(0).sum().alias("c"),
                 col("receiving_yards").fill_null(0).sum().alias("y")]
        for who, kind in (("receiver_player_id", "rec"), ("passer_player_id", "qb")):
            for r in tg.group_by(keys + [who]).agg(stats).iter_rows(named=True):
                self._add(self.gidx[r["game_id"]], r[who], r["defteam"], kind,
                          PASS_ZONES[(r["pass_length"], r["pass_location"])], r["n"], int(r["c"]), int(r["y"]))
        runs = p.filter((col("rush_attempt") == 1) & (col("qb_scramble").fill_null(0) == 0)
                        & col("rusher_player_id").is_not_null() & col("run_location").is_not_null())
        agg = runs.group_by(["game_id", "defteam", "rusher_player_id", "run_location", "run_gap"]).agg(
            pl.len().alias("n"), col("success").fill_null(0).sum().alias("c"), col("rushing_yards").fill_null(0).sum().alias("y"))
        for r in agg.iter_rows(named=True):
            z = RUN_ZONES.get((r["run_location"], None if r["run_location"] == "middle" else r["run_gap"]))
            if z is not None:
                self._add(self.gidx[r["game_id"]], r["rusher_player_id"], r["defteam"], "run", z, r["n"], int(r["c"]), int(r["y"]))
        if season >= self.current - 3:
            self._routes(season, tg)

    def _routes(self, season, tg):
        try:
            part = self.nfl.load_participation(season)
        except Exception as e:  # charting reaches nflverse after the season
            print(f"No participation (routes) for {season}: {str(e)[:80]}")
            return
        if "route" not in part.columns:
            return
        pl, col = self.pl, self.pl.col
        j = tg.join(part.select(col("nflverse_game_id").alias("game_id"), "play_id", "route"), on=["game_id", "play_id"])
        j = j.filter(col("route").is_not_null() & (col("route") != ""))
        agg = j.group_by(["receiver_player_id", "route"]).agg(
            pl.len().alias("n"), col("complete_pass").fill_null(0).sum().alias("c"), col("receiving_yards").fill_null(0).sum().alias("y"))
        out = self.routes.setdefault(season, {})
        for gsis, route, n, c, y in agg.iter_rows():
            i = self.pid.get(gsis)
            if i is not None and self.pos[i] in ("RB", "WR", "TE"):
                out[(i, route)] = [n, int(c), int(y)]

    def _prepare(self):
        """Defenses' games in date order, and league totals by position and kind."""
        if hasattr(self, "dgames"):
            return
        games, dg = self.games, {}
        for gi, t, _ in self.allowed:
            dg.setdefault(t, set()).add(gi)
        self.dgames = {t: sorted(g, key=lambda gi: games[gi][3]) for t, g in dg.items()}
        self.ddates = {t: [games[gi][3] for gi in g] for t, g in self.dgames.items()}
        self.league = {}
        for (gi, t, key), arr in self.allowed.items():
            acc = self.league.setdefault(key, [0] * len(arr))
            for j, v in enumerate(arr):
                acc[j] += v

    @staticmethod
    def _sum(arrays, size):
        acc = [0] * size
        for arr in arrays:
            if arr:
                for j, v in enumerate(arr):
                    acc[j] += v
        return acc

    def fit_before(self, pi, prior_gis, defense, as_of, kind):
        """Zone fit with only what was known before a game: the player's previous games and
        the defense's previous MATCH_WINDOW games (at least MATCH_MIN)."""
        self._prepare()
        size = 3 * ZONE_N[kind]
        use = self._sum([self.use.get((pi, gi, kind)) for gi in prior_gis], size)
        if sum(use[0::3]) < 15:
            return None
        i = bisect.bisect_left(self.ddates.get(defense, []), as_of)
        prev = self.dgames.get(defense, [])[max(0, i - MATCH_WINDOW):i]
        if len(prev) < MATCH_MIN:
            return None
        key = f"{self.pos[pi]}.{kind}"
        allowed = self._sum([self.allowed.get((gi, defense, key)) for gi in prev], size)
        return zone_fit(use, allowed, self.league[key], kind)

    key = "zone"

    def record(self, pi, prior, r, market, grade, value, line, implied):
        kind = zone_kind(market, self.pos[pi])
        if not kind:
            return
        f = self.fit_before(pi, [x[1] for x in prior[-MATCH_WINDOW:]], r[3], self.games[r[1]][3], kind)
        over = value > line
        if f is None:
            return
        tier = "-" if f <= ZONE_TIERS[0] else "+" if f >= ZONE_TIERS[1] else "0"
        for k in (kind, "all"):
            c = self.tiers.setdefault(k, {}).setdefault(grade, {}).setdefault(tier, [0, 0])
            c[0] += 1
            c[1] += over

    def summary(self):
        return {"t": self.tiers, "lo": ZONE_TIERS[0], "hi": ZONE_TIERS[1]}

    def page_data(self, pdata):
        """What the page needs for the next game: each recent player's zones over his last 10
        games, each defense's zones allowed over its last 10, league rates of the last two
        seasons, and route trees of the latest seasons with charting."""
        self._prepare()
        games, recent = self.games, self.current - 1
        played = {}
        for r in pdata["r"]:
            if games[r[1]][7] is not None:
                played.setdefault(r[0], []).append(r[1])
        players = {}
        for pi, gis in played.items():
            gis = sorted(gis, key=lambda gi: games[gi][3])[-MATCH_WINDOW:]
            if games[gis[-1]][0] < recent:
                continue
            d = {"g": len(gis)}
            for kind, n in ZONE_N.items():
                arr = self._sum([self.use.get((pi, gi, kind)) for gi in gis], 3 * n)
                if any(arr):
                    d[kind] = arr
            if len(d) > 1:
                players[pi] = d
        defenses = {}
        for t, gis in self.dgames.items():
            gis = [gi for gi in gis if games[gi][7] is not None][-MATCH_WINDOW:]
            if not gis or games[gis[-1]][0] < recent:
                continue
            d = {"g": len(gis)}
            for key in self.league:
                d[key] = self._sum([self.allowed.get((gi, t, key)) for gi in gis], len(self.league[key]))
            defenses[t] = d
        league = {}
        for (gi, t, key), arr in self.allowed.items():
            if games[gi][0] >= recent:
                acc = league.setdefault(key, [0] * len(arr))
                for j, v in enumerate(arr):
                    acc[j] += v
        # routes: the latest two seasons with charting; league rates by position
        seasons = sorted(self.routes)[-2:]
        names = sorted({rt for s in seasons for _, rt in self.routes[s]})
        rix = {rt: i for i, rt in enumerate(names)}
        rp, rl = {}, {}
        for s in seasons:
            for (pi, rt), v in self.routes[s].items():
                rp.setdefault(pi, {}).setdefault(s, []).append([rix[rt]] + v)
                acc = rl.setdefault(s, {}).setdefault(self.pos[pi], [[0, 0, 0] for _ in names])[rix[rt]]
                for j in range(3):
                    acc[j] += v[j]
        rp = {pi: v for pi, v in rp.items() if pi in players or any(s == seasons[-1] for s in v)}
        print(f"Play detail: {len(players)} players and {len(defenses)} defenses with zones; "
              f"routes for {seasons or 'no season'} ({len(rp)} players)")
        return {"pl": players, "d": defenses, "lg": league, "lgSeasons": [recent, self.current],
                "k": ZONE_K, "rt": {"s": seasons, "names": names, "pl": rp, "lg": rl}}


# ---------- "Top de la semana": game model and prop probabilities ----------
BREAK_EVEN = 0.524  # win rate needed at -110
FRANCHISE = {"OAK": "LV", "SD": "LAC", "STL": "LA"}


def _solve(A, b):
    """Solves A x = b (small dense system) by Gauss-Jordan elimination with partial pivoting."""
    k = len(b)
    M = [list(map(float, A[i])) + [float(b[i])] for i in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(k):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                for j in range(c, k + 1):
                    M[r][j] -= f * M[c][j]
    return [M[i][k] / M[i][i] for i in range(k)]


def _ols(X, y):
    k = len(X[0])
    A, b = [[0.0] * k for _ in range(k)], [0.0] * k
    for xi, yi in zip(X, y):
        for a in range(k):
            b[a] += xi[a] * yi
            for c in range(k):
                A[a][c] += xi[a] * xi[c]
    return _solve(A, b)


def _logistic(X, y, iters=8, l2=1.0):
    """Logistic regression by Newton's method with a small ridge penalty."""
    k = len(X[0])
    w = [0.0] * k
    for _ in range(iters):
        g, H = [-l2 * v for v in w], [[l2 if a == c else 0.0 for c in range(k)] for a in range(k)]
        for xi, yi in zip(X, y):
            p = 1 / (1 + math.exp(-sum(a * c for a, c in zip(xi, w))))
            q = p * (1 - p)
            for a in range(k):
                g[a] += (yi - p) * xi[a]
                qa = q * xi[a]
                for c in range(a, k):
                    H[a][c] += qa * xi[c]
        for a in range(k):
            for c in range(a):
                H[a][c] = H[c][a]
        w = [wi + d for wi, d in zip(w, _solve(H, g))]
    return w


GM_WINDOW = 10           # each team's previous games behind its rating
GM_FIRST_TEST = 2012     # first season of the walk-forward backtest
GM_BUCKETS = [0, 1.5, 3, 5]  # |model - market| in points: lower bound of each backtest bucket


def build_game_picks(games, eff):
    """Point-spread and total model for the next week's games, and its walk-forward backtest.
    A team's rating is its net EPA per play (offense minus defense) and its points for and
    against per game over its previous GM_WINDOW games. The home margin is fit by least
    squares on the rating gap, the points gap and home field; the total on both teams' EPA,
    points and plays and a dome flag. Each season since GM_FIRST_TEST is predicted with a
    model fit only on earlier seasons; the cover rate by size of the gap with the market's
    line is the probability the page shows for a pick."""
    fr = lambda t: FRANCHISE.get(t, t)
    by_game = {(r[0], fr(r[1])): r for r in eff}
    hist = {}

    def rating(t):
        h = hist.get(t, [])[-GM_WINDOW:]
        if len(h) < 4:
            return None
        tot = [sum(x[j] for x in h) for j in range(6)]
        n = len(h)
        return {"net": tot[0] / tot[1] - tot[2] / tot[3], "oe": tot[0] / tot[1], "de": tot[2] / tot[3],
                "pf": tot[4] / n, "pa": tot[5] / n, "pl": (tot[1] + tot[3]) / n}

    def features(g):
        a, h = rating(fr(g[4])), rating(fr(g[6]))
        if not a or not h:
            return None
        return {"sp": [1, h["net"] - a["net"], (h["pf"] - h["pa"]) - (a["pf"] - a["pa"]), 0 if g[10] else 1],
                "tot": [1, h["oe"] + h["de"] + a["oe"] + a["de"], h["pf"] + h["pa"] + a["pf"] + a["pa"], h["pl"] + a["pl"],
                        1 if g[12] in ("dome", "closed") else 0],
                "a": a, "h": h}

    recs = []
    for gi in sorted(range(len(games)), key=lambda gi: (games[gi][3], gi)):
        g = games[gi]
        if g[7] is None:
            continue
        f = features(g)
        if f and g[8] is not None and g[9] is not None:
            recs.append({"s": g[0], "f": f, "m": g[7] - g[5], "t": g[7] + g[5], "sp": g[8], "tot": g[9]})
        ra, rh = by_game.get((gi, fr(g[4]))), by_game.get((gi, fr(g[6])))
        if ra and rh:
            for t, me, op, pf, pa in ((fr(g[4]), ra, rh, g[5], g[7]), (fr(g[6]), rh, ra, g[7], g[5])):
                # offense EPA and plays, EPA and plays allowed, points for and against
                hist.setdefault(t, []).append((me[3] + me[6], me[2] + me[5], op[3] + op[6], op[2] + op[5], pf, pa))
    if not recs:
        return None
    seasons = sorted({r["s"] for r in recs})
    bt = {"sp": [[0, 0] for _ in GM_BUCKETS], "tot": [[0, 0] for _ in GM_BUCKETS]}
    bucket = lambda e: max(i for i, lo in enumerate(GM_BUCKETS) if abs(e) >= lo)
    for s in (x for x in seasons if x >= GM_FIRST_TEST):
        train, test = [r for r in recs if r["s"] < s], [r for r in recs if r["s"] == s]
        for k, target, line in (("sp", "m", "sp"), ("tot", "t", "tot")):
            w = _ols([r["f"][k] for r in train], [r[target] for r in train])
            for r in test:
                edge = sum(a * c for a, c in zip(r["f"][k], w)) - r[line]
                act = r[target] - r[line]
                if act and edge:
                    c = bt[k][bucket(edge)]
                    c[0] += 1
                    c[1] += (edge > 0) == (act > 0)
    w_sp = _ols([r["f"]["sp"] for r in recs], [r["m"] for r in recs])
    w_tot = _ols([r["f"]["tot"] for r in recs], [r["t"] for r in recs])
    # the next week with lines: the earliest week that still has unplayed games
    todo = [gi for gi, g in enumerate(games) if g[7] is None and g[8] is not None and g[9] is not None]
    picks = []
    if todo:
        first = games[min(todo, key=lambda gi: games[gi][3])]
        week = [gi for gi in todo if games[gi][:3] == first[:3]]
        for gi in sorted(week, key=lambda gi: games[gi][3]):
            f = features(games[gi])
            if not f:
                continue
            pm = sum(a * c for a, c in zip(f["sp"], w_sp))
            pt = sum(a * c for a, c in zip(f["tot"], w_tot))
            picks.append([gi, round(pm, 1), round(pt, 1)] +
                         [round(f[t][k], 3 if k == "net" else 1) for t in ("h", "a") for k in ("net", "pf", "pa")])
    print(f"Game model: backtest {GM_FIRST_TEST}-{seasons[-1]} spread {bt['sp']}, total {bt['tot']}; {len(picks)} upcoming games")
    return {"window": GM_WINDOW, "from": GM_FIRST_TEST, "to": seasons[-1], "b": GM_BUCKETS, "bt": bt, "be": BREAK_EVEN, "g": picks}


# prop probability: logistic model on the player's over rate at the line (last 10 and last 20
# games), the matchup letter, the team's implied total and the injury report (share of the
# team's targets or carries left by teammates who are out, main QB out, player questionable),
# one model per family of market
PROP_FAMILY = {"passing_yards": "y", "rushing_yards": "y", "receiving_yards": "y", "pass_rush": "y", "rush_rec": "y",
               "completions": "c", "attempts": "c", "carries": "c", "receptions": "c",
               "passing_tds": "t", "anytime_td": "t", "interceptions": "t"}
PROP_CAL = [0.5, 0.55, 0.6, 0.65, 0.7]
VAC_BUCKETS = [0, 0.001, 0.15, 0.3]  # share of the team's targets or carries left by teammates who are out  # lower bounds of the calibration buckets (probability of the side picked)


def prop_features(v10, v20, line, grade, implied):
    """[1, logit over rate at the line in the last 10, same for the last 20, grade A, grade C,
    (implied total - 22) / 5]; over rates use (overs + 1) / (games + 2)."""
    lg = lambda o, n: math.log((o + 1) / (n - o + 1))
    return [1.0, lg(sum(v > line for v in v10), len(v10)), lg(sum(v > line for v in v20), len(v20)),
            1.0 if grade == "A" else 0.0, 1.0 if grade == "C" else 0.0, 0.0 if implied is None else (implied - 22) / 5]


# ---------- injuries: who is out, and the volume they leave to their teammates ----------
OUT_STATUSES = {"Out", "Doubtful", "IR", "PUP", "NA", "Sus", "COV"}  # won't play (Sleeper adds IR/PUP/NA/Sus)
SLEEPER_TEAMS = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS", "OAK": "LV", "SD": "LAC", "STL": "LA"}


def load_injury_history(nfl, seasons):
    """(season, week, team) -> {"out": gsis ids listed Out/Doubtful, "q": listed Questionable},
    from nflverse's weekly injury reports."""
    out = {}
    for s in seasons:
        try:
            inj = nfl.load_injuries(s)
        except Exception as e:  # the current season's file appears with its first report
            print(f"No injury reports for {s}: {e}")
            continue
        for season, week, team, gsis, st in inj.select(["season", "week", "team", "gsis_id", "report_status"]).iter_rows():
            if gsis and st in ("Out", "Doubtful", "Questionable"):
                out.setdefault((season, week, team), {"out": set(), "q": set()})["q" if st == "Questionable" else "out"].add(gsis)
    return out


def load_sleeper(pdata, games):
    """Current injury status from Sleeper's free players endpoint (no key; updated through the
    week, including game-day downgrades and IR/PUP that weekly reports omit). Returns
    {player index: [status, body part, practice, updated (ISO date)]}; players are matched by
    gsis id, else by name and team. Sleeper asks for at most one call a day to this endpoint."""
    import urllib.request
    try:
        req = urllib.request.Request("https://api.sleeper.app/v1/players/nfl", headers={"User-Agent": "pizarra-nfl"})
        with urllib.request.urlopen(req, timeout=90) as r:
            players = json.load(r)
    except Exception as e:
        print(f"No Sleeper statuses ({e})")
        return {}
    last = {}
    for r in pdata["r"]:
        if r[0] not in last or games[r[1]][3] >= games[last[r[0]][1]][3]:
            last[r[0]] = r
    by_gsis = {p[0]: i for i, p in enumerate(pdata["pl"])}
    by_name = {}
    for i, p in enumerate(pdata["pl"]):
        if i in last:
            by_name.setdefault((norm_name(p[1]), FRANCHISE.get(last[i][2], last[i][2])), []).append(i)
    out = {}
    for p in players.values():
        st = p.get("injury_status")
        if not st or p.get("position") not in POSITIONS or not p.get("team"):
            continue
        team = SLEEPER_TEAMS.get(p["team"], p["team"])
        i = by_gsis.get(p.get("gsis_id") or "")
        if i is None:
            ids = by_name.get((norm_name(p.get("full_name") or ""), team), [])
            i = ids[0] if len(ids) == 1 else None
        if i is not None:
            upd = p.get("news_updated")
            out[i] = [st, p.get("injury_body_part"), p.get("practice_participation"),
                      dt.datetime.fromtimestamp(upd / 1000, dt.timezone.utc).date().isoformat() if upd else None]
    print(f"Sleeper: {len(out)} QB/RB/WR/TE with an injury status")
    return out


class TeamContext:
    """What a game's injury list takes from a team: the share of its targets and of its RB
    carries per game (over its previous 10 games) that belonged to teammates who won't play,
    counting only regulars (3+ of the last 5 team games, so a long absence already shows in the
    player's own games), and whether the main QB is out."""

    def __init__(self, games, pdata):
        self.games, self.pos, self.gsis = games, [p[2] for p in pdata["pl"]], [p[0] for p in pdata["pl"]]
        self.ix = {c: 4 + i for i, c in enumerate(STAT_COLS)}
        self.rows, tg = {}, {}
        for r in pdata["r"]:
            if games[r[1]][7] is not None:
                tg.setdefault(r[2], set()).add(r[1])
                self.rows.setdefault((r[2], r[1]), []).append(r)
        self.tgames = {t: sorted(v, key=lambda gi: games[gi][3]) for t, v in tg.items()}
        self.tdates = {t: [games[gi][3] for gi in v] for t, v in self.tgames.items()}
        self.cache = {}

    def mates(self, team, date):
        """Previous team games and, per teammate, [games, targets, carries, attempts, games in the last 5]."""
        key = (team, date)
        if key not in self.cache:
            i = bisect.bisect_left(self.tdates.get(team, []), date)
            prev = self.tgames.get(team, [])[max(0, i - MATCH_WINDOW):i]
            last5, ix, m = set(prev[-5:]), self.ix, {}
            tt = tc = 0
            for gi in prev:
                for r in self.rows[(team, gi)]:
                    x = m.setdefault(r[0], [0, 0, 0, 0, 0])
                    x[0] += 1
                    x[1] += r[ix["targets"]]
                    x[2] += r[ix["carries"]]
                    x[3] += r[ix["attempts"]]
                    x[4] += gi in last5
                    tt += r[ix["targets"]]
                    tc += r[ix["carries"]] if self.pos[r[0]] == "RB" else 0
            qb = max((k for k in m if self.pos[k] == "QB"), key=lambda k: (m[k][4], m[k][3]), default=None)
            self.cache[key] = (len(prev), m, tt / len(prev) if prev else 0, tc / len(prev) if prev else 0, qb)
        return self.cache[key]

    def context(self, pi, team, date, out, q):
        """[vacated target share, vacated carry share, main QB out, player questionable, who]."""
        n, m, tt, tc, qb = self.mates(team, date)
        if n < 4:
            return None
        vt = vc = 0.0
        who = []
        for k, x in m.items():
            if k == pi or self.gsis[k] not in out or x[4] < 3:
                continue
            st, sc = (x[1] / x[0] / tt if tt and self.pos[k] != "QB" else 0), (x[2] / x[0] / tc if tc and self.pos[k] == "RB" else 0)
            vt, vc = vt + st, vc + sc
            if st or sc or k == qb:
                who.append([k, round(st, 3), round(sc, 3)])
        return [round(vt, 3), round(vc, 3), 1 if qb is not None and qb != pi and self.gsis[qb] in out else 0,
                1 if self.gsis[pi] in q else 0, who]


def vacated_for(market, pos, ctx):
    """The vacated share that matters for a market: carries for rushing markets of a RB, both for
    touchdowns, targets otherwise; the QB-out flag doesn't apply to the QB himself."""
    vt, vc, qb_out, own_q = ctx[:4]
    fam = PROP_FAMILY[market]
    vac = vc if market in ("rushing_yards", "carries") or (market == "rush_rec" and pos == "RB") else vt + vc if fam == "t" else vt
    return [vac, 0 if pos == "QB" else qb_out, own_q]


class PropModel:
    """Fits P(over) for player props from every graded player-game since 2021. A player-game
    has no sportsbook line in the data, so each one is scored at three lines taken from the
    player's previous 10 games (20th, 50th and 80th percentile, rounded to .5; 0.5 and 1.5 for
    touchdowns): the model learns how far a hit rate at a line carries forward. The injury
    features come from that week's official report. Calibration is checked on the last two
    seasons with a model fit on the earlier ones."""
    key = "pm"

    def __init__(self, games, pdata, injuries):
        self.games, self.rows, self.vals = games, [], {}
        self.pos, self.inj, self.tc = [p[2] for p in pdata["pl"]], injuries, TeamContext(games, pdata)
        self.ctx = {}

    def _context(self, pi, r):
        if (pi, r[1]) not in self.ctx:
            g = self.games[r[1]]
            rep = self.inj.get((g[0], g[2], r[2]), {"out": set(), "q": set()})
            self.ctx[(pi, r[1])] = self.tc.context(pi, r[2], g[3], rep["out"], rep["q"]) or [0, 0, 0, 0, []]
        return self.ctx[(pi, r[1])]

    def _values(self, r, f, m):
        k = (id(r), m)
        if k not in self.vals:
            self.vals[k] = f(dict(zip(STAT_COLS, r[4:4 + len(STAT_COLS)])))
        return self.vals[k]

    def record(self, pi, prior, r, market, grade, value, line, implied):
        f = MARKETS[market][1]
        v20 = [self._values(x, f, market) for x in prior[-20:]]
        v10 = v20[-10:]
        fam = PROP_FAMILY[market]
        if fam == "t":
            lines = (0.5, 1.5) if market == "passing_tds" else (0.5,)
        else:
            s = sorted(v10)
            lines = sorted({int(s[int(q * (len(s) - 1))]) + 0.5 for q in (0.2, 0.5, 0.8)})
        extra = vacated_for(market, self.pos[pi], self._context(pi, r))
        for ln in lines:
            self.rows.append((fam, self.games[r[1]][0], prop_features(v10, v20, ln, grade, implied) + extra, value > ln))

    def summary(self):
        last = max(r[1] for r in self.rows)
        test_seasons = [last - 1, last]
        coef, cal = {}, [[0, 0.0, 0] for _ in PROP_CAL]
        # injury check on the test seasons: over rate by share of volume left by teammates who are
        # out, against the model with and without the injury features: [lines, overs, sum with, sum without]
        vac = [[0, 0, 0.0, 0.0] for _ in VAC_BUCKETS]
        sig = lambda x, w: 1 / (1 + math.exp(-sum(a * c for a, c in zip(x, w))))
        for fam in ("y", "c", "t"):
            rows = [r for r in self.rows if r[0] == fam]
            train = [r for r in rows if r[1] < test_seasons[0]]
            w = _logistic([r[2] for r in train], [float(r[3]) for r in train])
            w0 = _logistic([r[2][:6] for r in train], [float(r[3]) for r in train])
            for r in rows:
                if r[1] >= test_seasons[0]:
                    p = sig(r[2], w)
                    conf = max(p, 1 - p)
                    c = cal[max(i for i, lo in enumerate(PROP_CAL) if conf >= lo)]
                    c[0] += 1
                    c[1] += conf
                    c[2] += (p >= 0.5) == r[3]
                    if fam != "t":
                        v = vac[max(i for i, lo in enumerate(VAC_BUCKETS) if r[2][6] >= lo)]
                        v[0] += 1
                        v[1] += r[3]
                        v[2] += p
                        v[3] += sig(r[2][:6], w0)
            coef[fam] = [round(v, 4) for v in _logistic([r[2] for r in rows], [float(r[3]) for r in rows])]
        cal = [[n, round(sp / n, 4) if n else None, h] for n, sp, h in cal]
        vac = [[n, o, round(a / n, 4) if n else None, round(b / n, 4) if n else None] for n, o, a, b in vac]
        print(f"Prop model: {len(self.rows)} scored lines, coefficients {coef}, calibration {test_seasons}: {cal}; vacated {vac}")
        self.rows, self.vals = [], {}
        return {"coef": coef, "fam": PROP_FAMILY, "cal": cal, "calB": PROP_CAL, "test": test_seasons, "be": BREAK_EVEN,
                "vac": vac, "vacB": VAC_BUCKETS}

    def current(self, pdata, status):
        """Injury context for each player's next game from the current statuses
        ({player: [status, ...]}): {player: [vacated targets, vacated carries, QB out, questionable, who]}."""
        games, gsis = self.games, self.tc.gsis
        out = {gsis[i] for i, x in status.items() if x[0] in OUT_STATUSES}
        q = {gsis[i] for i, x in status.items() if x[0] == "Questionable"}
        nxt = {}
        for gi, g in enumerate(games):
            if g[7] is None:
                for t in (g[4], g[6]):
                    if t not in nxt or g[3] < games[nxt[t]][3]:
                        nxt[t] = gi
        last = {}
        for r in pdata["r"]:
            if r[0] not in last or games[r[1]][3] >= games[last[r[0]][1]][3]:
                last[r[0]] = r
        res = {}
        for pi, r in last.items():
            gi = nxt.get(r[2])
            if gi is None or games[r[1]][0] < self.tc.games[gi][0] - 1:
                continue
            c = self.tc.context(pi, r[2], games[gi][3], out, q)
            if c and (c[0] or c[1] or c[2] or c[3]):
                res[pi] = c
        print(f"Injury context: {len(res)} players affected for their next game")
        return res


# ---------- sportsbook prop lines (The Odds API snapshot from fetch_odds.py) ----------
ODDS_MARKETS = {"player_pass_yds": "passing_yards", "player_pass_tds": "passing_tds", "player_pass_completions": "completions",
                "player_pass_attempts": "attempts", "player_pass_interceptions": "interceptions", "player_rush_yds": "rushing_yards",
                "player_rush_attempts": "carries", "player_receptions": "receptions", "player_reception_yds": "receiving_yards",
                "player_rush_reception_yds": "rush_rec", "player_pass_rush_yds": "pass_rush", "player_anytime_td": "anytime_td"}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(s):
    """'Kenneth Walker III' -> 'kenneth walker', 'D.J. Moore' -> 'dj moore', accents dropped."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace() or ch == "-").replace("-", " ")
    return " ".join(w for w in s.split() if w not in NAME_SUFFIXES)


def build_odds(path, games, names, pdata):
    """Consensus prop lines for the coming games from a fetch_odds.py snapshot: for each player
    and market, the line most books offer and the best over and under prices at that line.
    Rows: [game, player, market, line, over price, over book, under price, under book, books]
    (book = index into "books"; anytime TD has only the "yes" side, stored as over at 0.5)."""
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError) as e:
        print(f"No odds snapshot ({e})")
        return None
    abbr = {full: t for t, full in names.items()}
    fr = lambda t: FRANCHISE.get(t, t)
    # each player's latest team, and players by normalized name
    last = {}
    for r in pdata["r"]:
        if r[0] not in last or games[r[1]][3] >= games[last[r[0]][1]][3]:
            last[r[0]] = r
    by_name = {}
    for i, p in enumerate(pdata["pl"]):
        if i in last:
            by_name.setdefault(norm_name(p[1]), []).append(i)
    books, bix, rows, unmatched = [], {}, [], set()
    for ev in snap.get("events", []):
        h, a = abbr.get(ev.get("home_team")), abbr.get(ev.get("away_team"))
        day = (ev.get("commence_time") or "")[:10]
        cand = [gi for gi, g in enumerate(games) if g[7] is None and fr(g[6]) == h and fr(g[4]) == a]
        if not cand or not day:
            continue
        gi = min(cand, key=lambda gi: abs((dt.date.fromisoformat(games[gi][3]) - dt.date.fromisoformat(day)).days))
        if abs((dt.date.fromisoformat(games[gi][3]) - dt.date.fromisoformat(day)).days) > 2:
            continue
        offers = {}  # (player, market) -> {line: {"o": [(price, book)], "u": [...]}}
        for b in ev.get("bookmakers", []):
            if b["title"] not in bix:
                bix[b["title"]] = len(books)
                books.append(b["title"])
            for m in b.get("markets", []):
                mk = ODDS_MARKETS.get(m["key"])
                if not mk:
                    continue
                for o in m.get("outcomes", []):
                    who = norm_name(o.get("description") or "")
                    ids = [i for i in by_name.get(who, []) if fr(last[i][2]) in (h, a)]
                    if len(ids) != 1:
                        unmatched.add(o.get("description"))
                        continue
                    side = {"over": "o", "yes": "o", "under": "u"}.get(str(o.get("name")).lower())
                    line = 0.5 if mk == "anytime_td" else o.get("point")
                    if side and line is not None and o.get("price") is not None:
                        offers.setdefault((ids[0], mk), {}).setdefault(line, {"o": [], "u": []})[side].append((o["price"], bix[b["title"]]))
        for (pi, mk), by_line in offers.items():
            # main line: the one most books quote (both sides counted), ties to the lower line
            line = max(by_line, key=lambda ln: (len({bk for _, bk in by_line[ln]["o"] + by_line[ln]["u"]}), -ln))
            o, u = by_line[line]["o"], by_line[line]["u"]
            best = lambda lst: max(lst, key=lambda x: x[0]) if lst else (None, None)  # highest American price pays most
            (op, ob), (up, ub) = best(o), best(u)
            rows.append([gi, pi, mk, line, op, ob, up, ub, len({bk for _, bk in o + u})])
    print(f"Odds: {len(rows)} prop lines for {len({r[0] for r in rows})} games from {len(books)} books "
          f"(snapshot {snap.get('fetched')}, {snap.get('remaining')} credits left); {len(unmatched)} names not matched")
    return {"fetched": snap.get("fetched"), "books": books, "p": rows, "remaining": snap.get("remaining")} if rows else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", type=Path, default=HERE / "pizarra-nfl.html")
    ap.add_argument("--no-players", action="store_true",
                    help="skip all nflverse downloads: local CSVs only, no player/ranking/EPA sections")
    ap.add_argument("--odds", type=Path, help="prop lines snapshot from fetch_odds.py (optional)")
    ap.add_argument("--no-sleeper", action="store_true", help="don't overlay Sleeper's current injury statuses")
    args = ap.parse_args()

    data, game_index = build_data(remote=not args.no_players)
    data["p"] = None if args.no_players else build_players(game_index)
    data["t"] = None if args.no_players else build_team_stats(game_index)
    detail = None
    if data["p"]:
        import nflreadpy as nfl
        import polars as pl
        detail = PlayDetail(nfl, pl, data["g"], data["p"], game_index)
    data["e"] = None if args.no_players else build_efficiency(game_index, detail)
    data["cov"] = None if args.no_players else build_coverage(game_index)
    data["logo"] = None if args.no_players else build_logos()
    props = None
    if data["p"]:
        import nflreadpy as nfl
        # current statuses: the latest weekly report, overridden by Sleeper's fresher ones
        if not args.no_sleeper:
            for i, x in load_sleeper(data["p"], data["g"]).items():
                data["p"]["inj"][i] = x + ["sleeper"]
        props = PropModel(data["g"], data["p"], load_injury_history(nfl, range(PLAYER_FIRST_SEASON, data["p"]["cur"] + 1)))
    hooks = [h for h in (detail, props) if h]
    data["mv"] = build_matchup_validation(data["g"], data["p"], hooks) if data["p"] else None
    if props:
        data["mv"]["pm"]["ctx"] = props.current(data["p"], data["p"]["inj"])
    data["gp"] = build_game_picks(data["g"], data["e"]) if data["e"] else None
    data["odds"] = build_odds(args.odds, data["g"], data["n"], data["p"]) if args.odds and data["p"] else None
    data["z"] = detail.page_data(data["p"]) if detail else None
    template = (HERE / "pizarra_template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    args.output.write_text(template.replace("__DATA__", payload), encoding="utf-8")
    print(f"Wrote {args.output} ({args.output.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
