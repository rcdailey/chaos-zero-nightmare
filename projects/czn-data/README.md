# czn-data

Scrapes the Chaos Zero Nightmare pages on [prydwen.gg](https://www.prydwen.gg/chaos-zero-nightmare)
into Postgres and serves the data to Claude (or any MCP client) through an MCP server.

One container image (`ghcr.io/rcdailey/czn-data`) runs both workloads:

- `czn-data scrape`: run on a schedule (a daily Kubernetes CronJob). A run takes about 20 minutes
  because it waits 10 seconds between requests, as Prydwen's robots.txt asks.
- `czn-data enrich`: derives data from what the scrape stored (character gender), without
  contacting Prydwen. Run it on its own schedule (a second CronJob), such as an hour after the
  scrape. A run that finds nothing new exits in seconds.
- `czn-data serve`: the MCP server (a Deployment), listening on port 8000.
- `czn-data migrate`: upgrades the database schema, then exits. `scrape`, `reparse`, and
  `enrich` run it first on their own. Run it as an init container of the server Deployment,
  with the scraper's database credentials, so a new image never serves an old schema.

## What gets stored

The scraper finds every page through robots.txt and the sitemaps, saves its HTML, and parses it:

- Character pages: rarity, element, class, cards and their Epiphany variants (I to V) with
  Prydwen's star ratings and per-variant reviews, Neutral and Divine advice, potentials,
  Manifest Ego levels (E1 to E6), and the guide: teams, decks, equipment and seasonal powers per
  season tab, fragment and partner rankings, synergies, and the prose sections (pros, cons,
  introduction, stats, and so on).
- Each character's card art (about 30 KB per image), fetched once per image URL.
- Character gender, female or male, set by `enrich`. Prydwen does not state it, so `enrich` runs
  the WD14 anime image tagger (`SmilingWolf/wd-swinv2-tagger-v3`) on each stored card art: a
  `1girl` score of at least 0.5 is female, anything else male. Each image is tagged once. Only a
  run that finds untagged art uses the model: it downloads the newest revision of the 467 MB
  model from Hugging Face into `CZN_TAGGER_DIR` when the stored copy is missing or outdated,
  then loads it (about 1 GB of memory). If Hugging Face is unreachable, the stored copy is used.
- `neutral-cards`, `equipment` (stats per level, drop source), `memory-fragments`, `partners`
  (passive, EGO skill, base stats), and `guides/effects-dictionary` (keyword and status
  definitions).
- Every page's full text, for search.

The partners page renders only the passive tab as HTML; the EGO skill and stats come from the
Next.js flight payload embedded in the same page. Prydwen does not list Epiphanies that a partner
unlocks for its character's cards. Fates, events, bosses, and shops are not on Prydwen at all.

## How failures are handled

A page's rows are replaced only when its new HTML parses cleanly. If parsing fails, or a page
suddenly yields more than 20% fewer rows of a kind, the previous rows stay and the run fails. The
new HTML is saved anyway, so after a parser fix `czn-data reparse` rebuilds the rows without
fetching the site. If Prydwen really removed content, run `czn-data reparse --allow-shrink`.

If Cloudflare challenges the scraper, the run stops at once, because every later request would be
blocked too.

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
- `czn_data_rows{table}`: rows per table after the run.

The enrich job sends its own series the same way. It exits non-zero when any row fails, and the
failed rows are tried again on the next run:

- `czn_enrich_success`, `czn_enrich_last_success_timestamp_seconds`,
  `czn_enrich_last_run_timestamp_seconds`, `czn_enrich_duration_seconds`: as for the scrape.
- `czn_enrich_failures{step}`: rows that failed per step (`gender`).

## Configuration

Environment variables:

- `DATABASE_URL` (all): Postgres URL. `migrate`, `scrape`, and `enrich` need the database owner;
  the server needs `SELECT` only.
- `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` (scrape, enrich): OTLP/HTTP
  receiver, such as `http://otel-gateway-collector.observability:4318`. With neither set, no
  metrics are sent. The other standard variables also apply: `OTEL_SERVICE_NAME`,
  `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TIMEOUT`, and
  `OTEL_SDK_DISABLED` or `OTEL_METRICS_EXPORTER=none` to turn the export off. Only the
  `http/protobuf` protocol is supported.
- `MCP_PUBLIC_URL` (serve): public URL of the MCP endpoint, such as
  `https://czn.example.com/mcp`.
- `OIDC_ISSUER` (serve): issuer URL of the OIDC provider that issues access tokens.
- `OIDC_AUDIENCE` (serve): expected `aud` claim. Defaults to `MCP_PUBLIC_URL`.
- `OIDC_REQUIRED_SCOPES` (serve): space-separated scopes every token must carry. Defaults to none.
- `MCP_ALLOW_UNAUTHENTICATED` (serve): `true` runs without auth, for local use.
- `CZN_TAGGER_DIR` (enrich): writable directory, kept across runs (such as a PVC of at least
  1 GB), where the portrait tagger stores its model. Without it, new characters get no gender
  and the run fails. The scrape job does not need it.
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
