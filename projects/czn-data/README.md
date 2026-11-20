# czn-data

Collects Chaos Zero Nightmare data from three sources into Postgres and serves it to Claude (or
any MCP client) through an MCP server:

- [wiki.e7bot.top](https://wiki.e7bot.top/#/czn/cards) serves the game's own data: characters,
  cards and every Epiphany version, potentials, partners, equipment, memory fragment sets,
  keywords, Chaos events (question-mark nodes) with every answer, Divine Epiphanies, and the
  current Chaos leaderboard with each ranked character's saved deck.
- [prydwen.gg](https://www.prydwen.gg/chaos-zero-nightmare) supplies the opinions: ratings,
  reviews, per-character guides (teams, decks with deck codes, equipment, fragment and partner
  rankings, synergies), the banner schedule, and guide pages.
- The game's official [Stove community](https://page.onstove.com/chaoszeronightmare/en)
  supplies update notices (season content, restrictions, shops), news and event overviews, and
  current community events.

Each scrape only collects its source, in that source's own shape (schemas `e7bot`, `prydwen`,
and `stove`). A build step then merges e7bot and Prydwen into the tables the MCP server reads
(schema `public`), and splits the Stove posts into searchable sections. Where both sources
describe the same fact, e7bot's value wins; Prydwen fills only the fields and records e7bot
lacks, such as a character Prydwen covers first. Every merged row has `source` (`e7bot` or
`prydwen`), `url`, and `data_fetched_at`; Prydwen opinions on it link to `prydwen_url`.

One container image (`ghcr.io/rcdailey/czn-data`) runs every workload:

- `czn-data daily`: the scheduled run (a daily Kubernetes CronJob). It runs the three scrapes
  below at once, then `enrich`, then one build, so a run takes about as long as the Prydwen
  scrape. A failed step fails the run but never stops a later one; enrich and the build then use
  the last good data of that source. Each scrape and enrich also runs on its own (for a manual
  rerun after a failure), and then builds.
- `czn-data scrape-e7bot`: the e7bot scrape. It makes 4 requests at a time, each worker pausing one second between requests. A run fetches
  the 11 list and diff documents; the 445 character and event details only when e7bot's diff
  timestamp shows new game data, or once a week (about 2 minutes then). Leaderboard saves never
  change, so each is fetched once; the first run fetches about 1,300 (about 6 minutes).
- `czn-data scrape`: the Prydwen scrape. A run takes about 20 minutes because it waits 10
  seconds between requests, as Prydwen's robots.txt asks.
- `czn-data scrape-stove`: the Stove scrape. It pauses one second between requests. Board lists are
  read every run; a post is fetched only when its edit time changed (about 120 posts, 2 minutes,
  on an empty database; seconds after).
- `czn-data build`: merges the stored sources again without fetching anything.
- `czn-data enrich`: derives data from what the Prydwen scrape stored (character gender),
  without contacting Prydwen. A run that finds nothing new exits in seconds.
- `czn-data serve`: the MCP server (a Deployment), listening on port 8000.
- `czn-data migrate`: upgrades the database schema, then exits. Every other command except
  `serve` runs it first on its own. Run it as an init container of the server Deployment, with
  the scraper's database credentials, so a new image never serves an old schema.

## What gets stored from e7bot

e7bot's API is undocumented. Its responses are AES-encrypted with a key in the site's public
JavaScript; `czn_data.e7bot` decrypts them and stores each response's data as received. Game
text keeps the game's markup until the build turns it into plain text. Player names and IDs on
the leaderboard are not stored. e7bot shows no Epiphanies that partners unlock, so those are in
neither source.

## What gets stored from Prydwen

The scraper finds every page through robots.txt and the sitemaps, saves its HTML, and parses it:

- Character pages: rarity, element, class, cards and their Epiphany variants (I to V) with
  Prydwen's star ratings and per-variant reviews, Neutral and Divine advice, potentials,
  Manifest Ego levels (E1 to E6), and the guide: teams, decks (with the deck code players paste
  into the game, and the Chaos the deck farms, both from the flight payload), equipment and
  seasonal powers per season tab, fragment and partner rankings, synergies, and the prose
  sections (pros, cons, introduction, stats, and so on).
- Each character's card art (about 30 KB per image), fetched once per image URL.
- Character gender, female or male, set by `enrich` for characters e7bot lacks (e7bot states
  every other character's gender). Prydwen does not state it, so `enrich` runs
  the WD14 anime image tagger (`SmilingWolf/wd-swinv2-tagger-v3`) on each stored card art: a
  `1girl` score of at least 0.5 is female, anything else male. Each image is tagged once. Only a
  run that finds untagged art uses the model: it downloads the newest revision of the 467 MB
  model from Hugging Face into `CZN_TAGGER_DIR` when the stored copy is missing or outdated,
  then loads it (about 1 GB of memory). If Hugging Face is unreachable, the stored copy is used.
- `neutral-cards`, `equipment` (stats per level, drop source), `memory-fragments`, `partners`
  (passive, EGO skill, base stats), and `guides/effects-dictionary` (keyword and status
  definitions).
- `banners`, which the sitemaps leave out, so the scrape adds it: each live and upcoming
  banner with its rate-up characters and exact Global start and end times.
- Every page's full text, for search.

The partners page renders only the passive tab as HTML; the EGO skill and stats come from the
Next.js flight payload embedded in the same page. Prydwen does not list Epiphanies that a partner
unlocks for its character's cards. Fates, events, bosses, and shops are not on Prydwen at all.

## What gets stored from Stove

Stove's community site loads its boards from an undocumented JSON API that needs no login.
`czn_data.stove` reads three boards: Update Notice, News, and Current Events (Announcements and
Known Issues hold maintenance and bug notices, so they are skipped). Each post is stored as
received: title, editor HTML, and its post and edit times.

The build splits each post into sections (`czn_data.posts`) using the team's writing habits:
"■" opens a section, "◆" a subsection, and "①" to "⑳" one event each. Long sections and posts
without markers become overlapping chunks. Struck-out text (an extended event's old end date) is
dropped; tables become one line per row; images, including some event calendars, are lost. A
section's period (a date range in its heading, or after "Period", "Schedule", or "Duration")
becomes start and end dates; a section without one takes the period of its "■" section, so an
event's details carry the event's dates. A date without a year takes the post's year, and text
that does not parse keeps no dates.

Search merges two rankings: sections matching the query's words (plus a few player words the
posts never use, such as "banned" for "restriction"), and sections close in meaning. Meaning
comes from a local embedding model (BAAI/bge-small-en-v1.5 through fastembed, 67 MB, CPU) run
on each section's title, heading, and first 300 characters. The build embeds only new or
changed sections; the first build embeds all of them (about 15 seconds on a desktop CPU).

## How failures are handled

An e7bot run replaces the stored responses only when every path fetched, decrypted, and kept
its expected fields, and no list shrank by more than 20%. Otherwise the previous responses stay,
the MCP keeps serving them, and the run fails; `czn-data scrape-e7bot --allow-shrink` accepts a
real removal. A leaderboard save that fails is tried again next run.

The build fails, and the previous merged tables stay, when e7bot holds a value it cannot map
(such as a new element). It matches Prydwen records to e7bot records by name (case, quote
characters, and close spellings are tolerated). A Prydwen card row on a character e7bot knows
that matches no e7bot card is still served, marked `prydwen`, and counted in
`czn_build_unmatched{entity="card"}`.

A Prydwen page's rows are replaced only when its new HTML parses cleanly. If parsing fails, or a
page suddenly yields more than 20% fewer rows of a kind, the previous rows stay and the run
fails. The new HTML is saved anyway, so after a parser fix `czn-data reparse` rebuilds the rows
without fetching the site. If Prydwen really removed content, run `czn-data reparse --allow-shrink`.

If Cloudflare challenges the scraper, the run stops at once, because every later request would be
blocked too.

A Stove post that fails to fetch, or comes back without content, keeps its stored version, and
the run fails. A board whose list fails, changes shape, or shrinks by more than 20% keeps all its
stored posts; `czn-data scrape-stove --allow-shrink` accepts a real removal. Posts no longer
listed are deleted.

The scraper exits non-zero on any failure, including a failed metrics export. It writes JSON logs
(`time`, `level`, `msg`, plus fields such as `url` and `error`) that vlagent maps to VictoriaLogs
fields. A scrape logs `run started` first, then `reading robots.txt` and each `reading sitemap`,
then a `fetch progress` line about once a minute (`done`, `total`, `ok`, `fetch_error`,
`parse_error`), and finally `run finished` and the metrics export outcome. Successful pages get
no line of their own. Alert rules for vmalert are in [deploy/alerts.yaml](deploy/alerts.yaml).

At the end of each run the scraper sends its metrics once over OTLP/HTTP and waits for the
receiver to accept them before it exits. The names below are the ones VictoriaMetrics stores
with `-opentelemetry.usePrometheusNaming` (the OTLP names use dots, such as `czn.scrape.pages`).
Every series also carries the OTLP resource attributes as labels, such as `service_name`,
`service_instance_id`, and whatever the collector adds.

- `czn_scrape_success`: 1 when the run had no failures.
- `czn_scrape_last_success_timestamp_seconds`: sent only by fully successful runs.
- `czn_scrape_last_run_timestamp_seconds`, `czn_scrape_duration_seconds`: end and length of the
  run.
- `czn_scrape_blocked`, `czn_scrape_discovery_failed`: 1 when the run stopped early.
- `czn_scrape_pages{result}`: pages per result (`ok`, `fetch_error`, `parse_error`). Card
  images that fail to download also count as `fetch_error`.
- `czn_scrape_validation_failures{kind,field}`: page kind and field that stopped parsing.
- `czn_data_rows{table}`: rows per table after the run (Prydwen tables from the scrape, merged
  tables from the build).

The e7bot scrape sends the same kind of series under `czn_e7bot_`: `success`,
`last_success_timestamp_seconds`, `last_run_timestamp_seconds`, `duration_seconds`, `blocked`,
`requests{result}` (`ok`, `fetch_error`, `invalid`), and `validation_failures{field}`. The Stove
scrape sends the same series under `czn_stove_`, plus `articles{result}` (`fetched`,
`unchanged`, `removed`).

Every command that builds also sends `czn_build_success`, `czn_build_last_run_timestamp_seconds`,
and `czn_build_unmatched{entity}`.

Enrich sends its own series the same way. It exits non-zero when any row fails, and the
failed rows are tried again on the next run:

- `czn_enrich_success`, `czn_enrich_last_success_timestamp_seconds`,
  `czn_enrich_last_run_timestamp_seconds`, `czn_enrich_duration_seconds`: as for the scrape.
- `czn_enrich_failures{step}`: rows that failed per step (`gender`).

## Configuration

Environment variables:

- `DATABASE_URL` (all): Postgres URL. Every command except `serve` needs the database owner;
  the server needs `SELECT` only.
- `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` (all but `serve` and
  `migrate`):
  OTLP/HTTP receiver, such as `http://otel-gateway-collector.observability:4318`. With neither
  set, no metrics are sent. The other standard variables also apply: `OTEL_SERVICE_NAME`,
  `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TIMEOUT`, and
  `OTEL_SDK_DISABLED` or `OTEL_METRICS_EXPORTER=none` to turn the export off. Only the
  `http/protobuf` protocol is supported.
- `MCP_PUBLIC_URL` (serve): public URL of the MCP endpoint, such as
  `https://czn.example.com/mcp`.
- `OIDC_ISSUER` (serve): issuer URL of the OIDC provider that issues access tokens.
- `OIDC_AUDIENCE` (serve): expected `aud` claim. Defaults to `MCP_PUBLIC_URL`.
- `OIDC_REQUIRED_SCOPES` (serve): space-separated scopes every token must carry. Defaults to none.
- `MCP_ALLOW_UNAUTHENTICATED` (serve): `true` runs without auth, for local use.
- `CZN_TAGGER_DIR` (daily, enrich): writable directory, kept across runs (such as a PVC of at
  least 1 GB), where the portrait tagger stores its model. Without it, new characters get no
  gender and the run fails. The scrape commands do not need it.
- `FASTEMBED_CACHE_PATH` (build, serve): where the search embedding model is kept. The image
  ships the model there and sets `HF_HUB_OFFLINE=1`; outside it, the model downloads on first
  use.
- `LOG_LEVEL` (all): defaults to `INFO`.

The server refuses to start unless `OIDC_ISSUER` or `MCP_ALLOW_UNAUTHENTICATED=true` is set.

Migrations create the schema, including the `pg_trgm` extension. `pg_trgm` is a trusted
extension, so the database owner that CloudNativePG creates can install it without superuser
rights. A Postgres advisory lock lets only one process migrate at a time, so the CronJob and the
server's init container can start together. When a migration changes the tables built from page
HTML, `migrate` parses the stored HTML again in the same transaction; it fetches nothing.

## Connecting claude.ai

The server is an OAuth resource server. It accepts JWT access tokens signed by `OIDC_ISSUER` and
publishes `/.well-known/oauth-protected-resource/mcp`, which tells claude.ai where to log in. Add
it in claude.ai as a custom connector using `MCP_PUBLIC_URL`. The provider must either allow
dynamic client registration, or have a client created for claude.ai whose ID and secret you enter
in the connector's advanced settings. Its access tokens must be JWTs whose `aud` matches
`OIDC_AUDIENCE`.

`/healthz` answers without a token, for probes.

## Development

```sh
mise run lint
mise run test   # starts throwaway Postgres and VictoriaMetrics containers
mise run db     # starts the development Postgres from docker-compose.yml
```

The development database keeps its data in the `czn-data-dev` volume across
`docker compose down`, so a scraped copy stays available for testing migrations and lookups.
`docker compose down -v` deletes it.

Migrations are hand-written SQL in `src/czn_data/migrations/versions`, one file per revision,
forward only. A migration that changes tables built from page HTML calls `request_rebuild()`.
