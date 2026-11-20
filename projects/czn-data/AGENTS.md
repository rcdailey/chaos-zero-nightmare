# czn-data

- Run tests with `mise run test [pytest args]`; it starts throwaway Postgres and VictoriaMetrics
  containers. Plain `uv run pytest` silently skips every database, metrics, and tagger test.
- Tagger tests download a 467 MB model into `CZN_TAGGER_DIR` on first run; post search tests
  download a 67 MB model into `FASTEMBED_CACHE_PATH`.
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
  result size, and name matching. Ask it to report every problem it meets anywhere in the app,
  including outside the changed area; being out of scope is no reason to leave a real problem
  unfixed.
