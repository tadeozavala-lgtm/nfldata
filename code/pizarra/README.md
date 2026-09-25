# Pizarra NFL

Interactive HTML dashboard (Spanish UI) built from the CSVs in `data/`:

- **Clasificación**: division standings per season with W-L-T, points for/against, record against the spread (ATS) and over/under, playoff seed and result.
- **Apuestas**: favorite win %, favorite cover %, home cover %, over % and mean spread error per season, plus trend charts for favorites and home-field advantage (actual margin vs. the market's spread).
- **Equipo**: per-franchise win % and ATS by season and a game log with line and betting outcome.

The season filter also offers **Histórico**: every completed season combined (the season in progress is left out), with franchise totals, playoff appearances and titles in the standings, the full-period betting rates with their per-season range, and a season-by-season table for the selected team.

Covers 2002 (divisional realignment) onward. Relocated teams are grouped by franchise (OAK → LV, SD → LAC, STL → LA). Playoff results are derived from `games.csv`, because `standings.csv` leaves some finalists blank.

## Build

Python 3, standard library only:

``` sh
python3 code/pizarra/build_pizarra.py            # writes code/pizarra/pizarra-nfl.html
python3 code/pizarra/build_pizarra.py -o out.html
```

The output is a single self-contained page (~450 KB, data embedded) that opens in any browser. It is git-ignored; rebuild it after each data update.

The `Build Pizarra NFL dashboard` workflow (`.github/workflows/build_pizarra.yml`) rebuilds it whenever `games.csv`, `standings.csv`, `teams.csv`, `teamcolors.csv` or this folder change, and can also be run by hand. Download the page from the run's `pizarra-nfl` artifact (kept 30 days).
