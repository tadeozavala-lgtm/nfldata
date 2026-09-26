# Pizarra NFL

Interactive HTML dashboard (Spanish UI) built from the CSVs in `data/` plus nflverse player data:

- **Clasificación**: division standings per season with W-L-T, points for/against, record against the spread (ATS) and over/under, playoff seed and result.
- **Apuestas**: favorite win %, favorite cover %, home cover %, over % and mean spread error per season, plus trend charts for favorites and home-field advantage (actual margin vs. the market's spread).
- **Equipo**: per-franchise win % and ATS by season and a game log with line and betting outcome.

The season filter also offers **Histórico**: every completed season combined (the season in progress is left out), with franchise totals, playoff appearances and titles in the standings, the full-period betting rates with their per-season range, and a season-by-season table for the selected team. In that mode two more filters appear: **Ventana** limits the period to the last 5, 10, 15 or 20 completed seasons (applies to the whole page), and **Rival** isolates the selected team's head-to-head record against another franchise, with the margin of every meeting and the full game log.

Covers 2002 (divisional realignment) onward. Relocated teams are grouped by franchise (OAK → LV, SD → LAC, STL → LA). Playoff results are derived from `games.csv`, because `standings.csv` leaves some finalists blank.

- **Jugadores del equipo** and **Defensa contra posición** (team section): per-game averages of the team's QB/RB/WR/TE in the current scope (season, histórico window, or only games against the chosen rival), and what the team's defense allows to each position with its league rank.
- **Props de jugadores**: a list of player props whose lines you enter by hand (one at a time or pasted as `jugador; mercado; línea`, kept in the browser), with over rates for the last 5 and 10 games and the current and previous seasons, the next opponent and its rank against the position over its last 10 games, and injury tags from the latest report. Selecting a prop opens the player detail: game-by-game chart against the line, home/away, favorite/underdog and game-total splits, usage (targets, target share, air-yards share, carries) and the last 10 games.

Player data (2021 on) comes from nflverse releases through [nflreadpy](https://github.com/nflverse/nflreadpy).

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
