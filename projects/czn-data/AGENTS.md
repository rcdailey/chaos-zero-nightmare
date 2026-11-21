# czn-data

- Run tests with `mise run test [pytest args]`; it starts a throwaway Postgres container. Plain
  `uv run pytest` silently skips every database test.
- Durable tests cover this app's own behavior only. A real Postgres is the one allowed external
  system, because the app's logic lives in SQL. pytest-socket blocks every other connection.
  Replace a model, site, or other service with a fake at the app's boundary to it. Do not test
  how well a model performs, how a third-party service behaves, or cluster configuration (metric
  storage, alert rules, deployment); check those by hand. A test that takes over a second needs
  a reason.
- Scrapes fetch live sites: `scrape` (Prydwen, one request per 10 seconds; a few minutes, about 20
  when every page is due: empty database or weekly refresh), `scrape-e7bot` (seconds to 2 minutes;
  about 8 on an empty database), `scrape-stove` (seconds; about 2 minutes on an empty database). To
  verify a change against stored data instead (`mise run db` for the local database): `reparse`
  after a Prydwen parser change, `build` after a change to `czn_data.build` (merging and Stove post
  sections).
- Migrations are hand-written SQL under `src/czn_data/migrations/versions`, forward only. A
  migration that changes tables built from page HTML MUST call `request_rebuild()`; one that
  changes the merged tables in `public` MUST call `request_build()`.
- After changing MCP tools or the data they serve, run an acceptance audit as a usability
  test: give it 10+ real player questions to answer using only the tools against the local
  database, and ask for findings on wrong or missing data, tool granularity and design,
  result size, and name matching. Ask it to report every problem it meets anywhere in the app,
  including outside the changed area; being out of scope is no reason to leave a real problem
  unfixed.
