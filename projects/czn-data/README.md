# czn-data

Scrapes the Chaos Zero Nightmare pages on [prydwen.gg](https://www.prydwen.gg/chaos-zero-nightmare)
into Postgres and serves the data to Claude (or any MCP client) through an MCP server.

One container image (`ghcr.io/rcdailey/czn-data`) runs both workloads:

- `czn-data scrape`: run on a schedule (a daily Kubernetes CronJob). A run takes about 20 minutes
  because it waits 10 seconds between requests, as Prydwen's robots.txt asks.
- `czn-data serve`: the MCP server (a Deployment), listening on port 8000.

## What gets stored

The scraper finds every page through robots.txt and the sitemaps, saves its HTML, and parses it:

- Character pages: rarity, element, class, cards and their Epiphany variants (I to V) with
  Prydwen's star ratings, potentials, Manifest Ego levels (E1 to E6), and the written review.
- `neutral-cards`, `equipment` (stats per level, drop source), `memory-fragments`, `partners`
  (passive only), and `guides/effects-dictionary` (keyword and status definitions).
- Every page's full text, for search.

Prydwen loads partner EGO skills and stats only in the browser, so they are missing. Fates,
events, bosses, and shops are not on Prydwen at all.

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
- `czn_scrape_pages{result}`: pages per result (`ok`, `fetch_error`, `parse_error`).
- `czn_scrape_validation_failures{kind,field}`: page kind and field that stopped parsing.
- `czn_data_rows{table}`: rows per table after the run.

## Configuration

Environment variables:

- `DATABASE_URL` (both): Postgres URL. The scraper creates tables; the server needs `SELECT` only.
- `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` (scrape): OTLP/HTTP
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
- `LOG_LEVEL` (both): defaults to `INFO`.

The server refuses to start unless `OIDC_ISSUER` or `MCP_ALLOW_UNAUTHENTICATED=true` is set.

The scraper creates the schema itself, including the `pg_trgm` extension. `pg_trgm` is a trusted
extension, so the database owner that CloudNativePG creates can install it without superuser
rights.

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
mise run test   # starts a throwaway Postgres container
```
