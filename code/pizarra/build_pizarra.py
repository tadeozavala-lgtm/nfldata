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


def build_matchup_validation(games, pdata):
    """Backtest of the matchup grade. For every player-game since 2021 the opponent is graded
    with only its previous MATCH_WINDOW games (no look-ahead), the line is the dashboard's
    suggested line (median of the player's previous 10 games, rounded to .5; 0.5 for TDs), and
    we count how often the player went over, by grade, by market and by implied-total modifier."""
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
    return {"res": res, "mod": mods, "from": PLAYER_FIRST_SEASON, "to": last, "window": MATCH_WINDOW}


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
    data["cov"] = None if args.no_players else build_coverage(game_index)
    data["logo"] = None if args.no_players else build_logos()
    data["mv"] = build_matchup_validation(data["g"], data["p"]) if data["p"] else None
    template = (HERE / "pizarra_template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    args.output.write_text(template.replace("__DATA__", payload), encoding="utf-8")
    print(f"Wrote {args.output} ({args.output.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
