# Repository Rules

- Work in the current checkout on `main`. MUST NOT create worktrees or feature branches.
- Pushing `main` deploys: `projects/czn-tools` to GitHub Pages, and `projects/czn-data` as the
  `latest` image the cluster runs.

## Layout

Two independent projects, each with its own `mise.toml` toolchain; run commands from the project
directory.

- `projects/czn-data`: Python (uv) scrapers (e7bot game data, Prydwen guides), merge build, and
  MCP server. `README.md` there owns behavior, configuration, and deployment.
- `projects/czn-tools`: Vite/React static site. Check with `bun run lint` and `bun run build`
  (`build` runs the `tsc` typecheck); it has no tests.
- `docs/game-data-investigation.md`: findings on reading CZN's own game files; nothing implemented.

## czn-data

- Run tests with `mise run test [pytest args]`; it starts throwaway Postgres and VictoriaMetrics
  containers. Plain `uv run pytest` silently skips every database, metrics, and tagger test.
- Tagger tests download a 467 MB model into `CZN_TAGGER_DIR` on first run.
- `czn-data scrape` fetches live Prydwen at one request per 10 seconds (about 20 minutes). After a
  parser change, run `czn-data reparse` against stored HTML (`mise run db` for the local database)
  instead.
- `czn-data scrape-e7bot` fetches live e7bot (seconds to 2 minutes; about 8 on an empty database).
  After a change to `czn_data.build`, run `czn-data build` against stored data instead.
- Migrations are hand-written SQL under `src/czn_data/migrations/versions`, forward only. A
  migration that changes tables built from page HTML MUST call `request_rebuild()`; one that
  changes the merged tables in `public` MUST call `request_build()`.
- After changing MCP tools or the data they serve, run an acceptance audit as a usability
  test: give it 10+ real player questions to answer using only the tools against the local
  database, and ask for findings on wrong or missing data, tool granularity and design,
  result size, and name matching.
