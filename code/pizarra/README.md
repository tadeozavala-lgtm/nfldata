# Pizarra NFL

Interactive HTML dashboard (Spanish UI) built from nflverse data plus the CSVs in `data/`:

- **Clasificación**: division standings per season with W-L-T, points for/against, record against the spread (ATS) and over/under, playoff seed and result.
- **Apuestas**: favorite win %, favorite cover %, home cover %, over % and mean spread error per season, plus trend charts for favorites and home-field advantage (actual margin vs. the market's spread).
- **Equipo**: the selected team's next game (opponent, week, date and Eastern kickoff time, home/away/neutral site, stadium, roof and surface, line and rest days), per-franchise win % and ATS by season and a game log with line and betting outcome.

The season filter also offers **Histórico**: every completed season combined (the season in progress is left out), with franchise totals, playoff appearances and titles in the standings, the full-period betting rates with their per-season range, and a season-by-season table for the selected team. In that mode two more filters appear: **Ventana** limits the period to the last 5, 10, 15 or 20 completed seasons (applies to the whole page), and **Rival** isolates the selected team's head-to-head record against another franchise, with the margin of every meeting and the full game log.

Covers 2002 (divisional realignment) onward. Relocated teams are grouped by franchise (OAK → LV, SD → LAC, STL → LA). Playoff results are derived from `games.csv`, because `standings.csv` leaves some finalists blank.

- **Jugadores del equipo** and **Defensa contra posición** (team section): per-game averages of the team's QB/RB/WR/TE in the current scope (season, histórico window, or only games against the chosen rival), and what the team's defense allows to each position with its league rank.
- **Ofensivas y defensivas** (standings section) and **Ranking ofensivo y defensivo** (team section): two views with league ranks for the current scope, sortable by any column; a toggle narrows the table to the selected team and its rival (the histórico rival when one is chosen, else the next game's opponent) while keeping league-wide ranks. *Volumen*: points, total, passing (net of sacks) and rushing yards per game, produced and allowed (nflverse team stats). *Eficiencia*: EPA per play, per dropback and per designed run, success rate, red-zone TD rate and third-down conversion, produced and allowed, plus early-down neutral pass rate over expected (aggregated from nflverse play-by-play at build time).
- **Líneas del partido** (team section): enter the home spread and the over/under; the page applies them to the team's last 5, 10 and 20 games (only head-to-head games when a rival is chosen, all its games otherwise) and shows how often it would have covered and gone over, next to its real ATS record as favorite, underdog, home and away. The fields start from the next game's line in `games.csv` when there is one. **Contexto del partido** below it compares the game's conditions with how similar games went against the market since 2006: stadium roof, forecast wind and temperature and the referee (entered by hand, since the schedule doesn't carry them before kickoff), rest difference and divisional games.
- **Props de jugadores**: a list of player props whose lines you enter by hand (one at a time or pasted as `jugador; mercado; línea`, kept in the browser), with over rates for the last 5 and 10 games and the current and previous seasons, actual minus expected production over the last 10 games, the next opponent and its rank against the position over its last 10 games, depth-chart position and injury tags from the latest report. Selecting a prop opens the player detail: game-by-game chart against the line (click a bar to see that opponent's defensive ranks that season: against the pass or the run depending on the market, EPA allowed, points allowed and against the player's position), home/away, favorite/underdog and game-total splits, usage (snap share, targets, target share, air-yards share, carries), actual vs expected production (ffverse opportunity model) and the last 10 games.

Games (scores, lines, weather, referees) are read from nflverse's schedule release, the same table as `data/games.csv` but without waiting for this fork to sync; `data/games.csv` is used when nflreadpy or the network is unavailable (and always with `--no-players`). **Matchup** (props list and player detail): a letter for the next opponent in the prop's market. The opponent's production allowed to the player's position in that stat per game over its last 10 games is ranked among all defenses: **A** = #21–32 (allows the most), **B** = #11–20, **C** = #1–10. A **+/−** modifier sums two signals: the team's implied total from the next game's line (≥ 25 pts +, ≤ 18 pts −) and, for pass-catching and QB passing markets, the opposing starters' coverage (depth-chart CBs for WR/QB, safeties and linebackers for TE, linebackers for RB) from PFR coverage stats: a unit in the league's bottom or top 8 by yards allowed per target, and starters listed out or doubtful. The player detail lists the probable coverage defenders (a probable matchup: the data doesn't say who lines up on whom) with their coverage numbers and the defense's man-coverage rate. The build backtests the letter since 2021 (opponent graded only on its previous 10 games, line = the dashboard's suggested line) and the page shows over rates by letter and market, and by implied-total modifier.

Team logos come from the ESPN URLs in nflverse's teams table (current and historic franchises), downloaded at build time at 64×64 and embedded as data URIs, since the published page can't load images from other sites; a team without a logo falls back to its color swatch. Player data (2021 on), team stats and play-by-play efficiency (2002 on), snap counts (PFR), depth charts and expected production (ffverse) come from nflverse releases through [nflreadpy](https://github.com/nflverse/nflreadpy). A full build downloads one play-by-play file per season, so it takes a few minutes.

## Build

Python 3. Team data needs only the standard library; player data needs nflreadpy:

``` sh
pip install nflreadpy
python3 code/pizarra/build_pizarra.py               # writes code/pizarra/pizarra-nfl.html
python3 code/pizarra/build_pizarra.py -o out.html
python3 code/pizarra/build_pizarra.py --no-players  # offline, team data only
```

The output is a single self-contained page (~2.3 MB with player data, ~0.5 MB without) that opens in any browser. It is git-ignored; rebuild it after each data update.

The `Build Pizarra NFL dashboard` workflow (`.github/workflows/build_pizarra.yml`) rebuilds it every day at 13:17 UTC (player stats and injury reports) and whenever `games.csv`, `standings.csv`, `teams.csv`, `teamcolors.csv` or this folder change, and can also be run by hand. Download the page from the run's `pizarra-nfl` artifact (kept 30 days).
