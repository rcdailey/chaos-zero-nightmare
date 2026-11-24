# czn-data

Collects Chaos Zero Nightmare data from five sources into Postgres and serves it to Claude (or
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
- [Game8](https://game8.co/games/Chaos-Zero-Nightmare) explains how the game works: systems
  (Save Data, Distortion Adaptation, stress), Chaos modes and areas, boss modes, Ark City,
  events, and seasons.
- The game's own data files, from the CDN the game downloads them from, supply the in-game Help
  text: the rules its Help menus and tutorials explain.

Each scrape only collects its source, in that source's own shape (schemas `e7bot`, `prydwen`,
`stove`, `game8`, and `gamefiles`). A build step then merges e7bot and Prydwen into the tables the
MCP server reads (schema `public`), splits the Stove posts and Game8 pages into searchable
sections, and turns the game files into Help topics. Where
both sources describe the same fact, e7bot's value wins; Prydwen fills only the fields and records
e7bot lacks, such as a character Prydwen covers first. Every merged row has `source` (`e7bot` or
`prydwen`), `url`, and `data_fetched_at`; Prydwen opinions on it link to `prydwen_url`.

One container image (`ghcr.io/rcdailey/czn-data`) runs every workload:

- `czn-data daily`: the scheduled run (a daily Kubernetes CronJob). It runs the five scrapes below
  at once, then `enrich`, then one build, so a run takes about as long as the Prydwen scrape. A
  failed step fails the run but never stops a later one; enrich and the build then use the last good
  data of that source. Each scrape and enrich also runs on its own (for a manual rerun after a
  failure), and then builds. Only one of `daily`, the scrapes, `reparse`, and `enrich` runs at a
  time (a Postgres advisory lock): one started while another runs, such as a deploy's scrape during
  the CronJob, logs `another run is in progress, skipping` and exits 0 without migrating.
- `czn-data scrape-e7bot`: the e7bot scrape. It makes 4 requests at a time, each worker pausing one
  second between requests. A run fetches the 11 list and diff documents; the 445 character and event
  details only when e7bot's diff timestamp shows new game data, or once a week (about 2 minutes
  then). Leaderboard saves never change, so each is fetched once; the first run fetches about 1,300
  (about 6 minutes). The icons of wearable equipment and the rarity backgrounds slots draw behind
  them are image files, fetched one at a time and once each (about 280, 5 minutes, on an empty
  database).
- `czn-data scrape`: the Prydwen scrape. It waits 10 seconds between requests, as Prydwen's
  robots.txt asks, so it downloads only pages that may have changed (see below) and then parses
  the stored pages whose HTML or parser changed. A run takes a few minutes; one that downloads
  every page (the first, and about once a week) takes about 20.
- `czn-data scrape-stove`: the Stove scrape. It pauses one second between requests. Board lists are
  read every run; a post is fetched only when its edit time changed (about 120 posts, 2 minutes,
  on an empty database; seconds after).
- `czn-data scrape-game8`: the Game8 scrape. It pauses one second between requests. The game page
  and sitemap are read every run; a page is fetched only when its sitemap date changed or its copy
  is over 7 days old (about 90 pages, 2 minutes, on an empty database; seconds after).
- `czn-data scrape-gamefiles`: the game files scrape. It reads the game's manifest (3 MB) every
  run and downloads a file only when its manifest entry or the build changed (about 16 MB, 10
  seconds, on an empty database or after a new build; seconds after).
- `czn-data build`: merges the stored sources again without fetching anything (see "What a run
  processes again").
- `czn-data eval-search`: measures the search tools on their known questions (see "How search
  ranks"). The daily run does this after its build when the passages, the questions, or the
  search code changed since the last measurement; this command always measures.
- `czn-data enrich`: derives data from what the scrapes stored (character gender, identity prints,
  and gear prints), without contacting a source site. A run that finds nothing new exits in seconds.
- `czn-data serve`: the MCP server (a Deployment), listening on port 8000.
- `czn-data migrate`: upgrades the database schema, then exits. Every other command except
  `serve` runs it first on its own. Run it as an init container of the server Deployment, with
  the scraper's database credentials, so a new image never serves an old schema.

## What a run processes again

Each step processes an item again only when the item's input or the step's code changed since
the item last processed cleanly, so a deploy or a day with one changed page costs little more
than a day with none. The `processed` table holds a fingerprint per step and item
(`czn_data.digest`): a hash of the item's input and of the step's source code, which covers the
step's module, every `czn_data` module it imports, and the versions of the packages they import.
Any edit to that code, even a comment, processes every item of the step once.

- Prydwen parsing: a stored page is parsed again when its HTML or the parser changed. A page that
  fails to parse is parsed again every run, so it keeps failing the run until a fix. `reparse`
  and a migration that requests a rebuild parse every page.
- Build: a Stove post or Game8 page is split into sections again when its content or the
  splitting code changed. Matching records across sources reads every source each build (about a
  second). Each merged table then receives only the rows that differ from the stored ones, and the
  embedding model loads only when a passage's text is new.
- Character images: downloaded again when their character page downloads again (see below).
- Character and gear prints (`enrich`): each print table keys its rows by the fingerprint of the
  code that made them (`czn_data.identity` or `czn_data.gear` and what it imports), not the
  `processed` table, because the server must find the prints of its own code. After a change,
  enrich prints every image again and drops the old prints; until it has run, the server names
  no characters or gear. A deploy that changes this code needs `czn-data enrich` (about a minute).
- Equipment icons: fetched once each and never again, since e7bot names them after the game's
  asset files and new art gets a new name.
- Search measurement: see `czn-data eval-search` above.

## What gets stored from e7bot

e7bot's API is undocumented. Its responses are AES-encrypted with a key in the site's public
JavaScript; `czn_data.e7bot` decrypts them and stores each response's data as received. Game
text keeps the game's markup until the build turns it into plain text. Player names and IDs on
the leaderboard are not stored. e7bot shows no Epiphanies that partners unlock, so those are in
neither source.

## What gets stored from Prydwen

The scraper finds every page through robots.txt and the sitemaps. Prydwen keeps each game in its
own sitemap, so a run reads only the sitemap that listed the game's pages last time, and reads all
of them when robots.txt no longer names it or it lists no game pages. A page is downloaded when
there is no stored copy, the sitemap gives it no date, its sitemap date differs from the stored
copy's, or the copy is more than 7 days old (which bounds how long a change the date misses, such
as a listing page gaining a character, goes unnoticed). A page that fails to download keeps its
copy and date and is tried again next run. The scraper saves each downloaded page's HTML, then
parses the stored pages whose HTML or parser changed, so a parser change applies on the next run
without downloading:

- Character pages: rarity, element, class, cards and their Epiphany variants (I to V) with
  Prydwen's star ratings and per-variant reviews, Neutral and Divine advice, potentials,
  Manifest Ego levels (E1 to E6), and the guide: teams, decks (with the deck code players paste
  into the game, and the Chaos the deck farms, both from the flight payload), equipment and
  seasonal powers per season tab, fragment and partner rankings, synergies, and the prose
  sections (pros, cons, introduction, stats, and so on).
- Each character's card art (about 30 KB per image). Prydwen keeps the URL when it replaces the
  art, so an image is downloaded again whenever its character page downloads again (a changed
  page, or the 7-day refresh); new art replaces the stored image and is tagged again.
- Character gender, female or male, set by `enrich` for characters e7bot lacks (e7bot states
  every other character's gender). Prydwen does not state it, so `enrich` runs
  the WD14 anime image tagger (`SmilingWolf/wd-swinv2-tagger-v3`) on each stored card art: a
  `1girl` score of at least 0.5 is female, anything else male. Each image is tagged once. Only a
  run that finds untagged art uses the model: it downloads the newest revision of the 467 MB
  model from Hugging Face into `CZN_TAGGER_DIR` when the stored copy is missing or outdated,
  then loads it (about 1 GB of memory). If Hugging Face is unreachable, the stored copy is used.
- Character identity prints, for every character with card art, set by `enrich`: the CCIP
  model's 768-number print of each card art (see "Naming characters on a screenshot"). Each
  image is printed once per print code (see "What a run processes again").
- Gear prints, for every stored e7bot equipment icon, set by `enrich`: DINOv2 small's print of
  the icon drawn on each of the 4 rarity backgrounds (see "Naming gear on a screenshot"). Each
  icon is printed once per print code, about 30 seconds for all 280.
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

Sections are searched as described in "How search ranks". A search covering announcements adds
the post words for a few player words the posts never use, such as "restriction" for "banned".

## What gets stored from Game8

Game8 covers every part of the game, but e7bot, Prydwen, and the official posts already serve
characters, cards, equipment, partners, banners, and patch notes. `czn_data.game8` keeps only the
pages Game8's game page links under its headings for Chaos modes and areas, Simulation,
Challenges, Ark City, Tips and Tricks, the glossary, seasons, events, and missions, plus the
current season's guide. Pages missing from Game8's sitemap (its premium opinion articles) are
skipped. Each page is stored as received, with its date from Game8's sitemap.
Game8's robots.txt blocks AI crawlers (GPTBot, Google-Extended) but not ordinary ones; this
project uses Game8 by the owner's choice and keeps the load small.

The build splits each page at its headings (h2 to h4) into sections whose heading names their
parents ("Best Teams > Aavarck Best Teams"). Tables become one line per row, the table of
contents and the related-guide link lists are dropped, images are lost, and long sections become
overlapping pieces as the posts do. Some pages are out of date; each result carries Game8's
update date, and newer pages win close calls.

## What gets stored from the game files

The game downloads its data from a public CDN: a manifest that lists every file, and chunk files
that hold the files back to back. `czn_data.gamefiles` reads the manifest, range-requests only
the bytes of the files it needs, then decompresses and decrypts them (formats in
[the investigation notes](../../docs/game-data-investigation.md)). It stores the rows of the
English text table (every string in the game) and of the two Help menus with their pages. The
files themselves are not kept: their rows in the database are the cache, and each file's
manifest entry decides whether it is downloaded again.

The build joins each Help menu entry to its pages and their English text, and serves one topic
per entry (about 130 topics). Pages that only the tutorial shows become one topic each. The
game's markup becomes plain text. A topic with several pages lists them in order, each under
its own title unless that title repeats the topic's.

The build also turns other English text that explains the game into passages: each tutorial
script's lines in order, each event's description, and the game's messages of at least eight
words (popups and confirmations, not labels and buttons) grouped by the feature their key names
(`ui_savedata_...`) into pieces of about 1,200 characters. Story scenes, item names, and combat
tooltips are left out.

The `search` tool serves the Help topics and these passages as the `rules` kind. The game's own
text counts as the newest source, so it wins close calls with guides.

The game asks a version service, whose address is unknown, for its current build folder on the
CDN. So `BUILD` in `czn_data.gamefiles` names one folder and is updated by hand after a patch
(the folder is in `patch-meta.json` in the game's folder on a device). Until then the scrape
keeps reading the old build, which stays correct as long as the CDN keeps serving it.

## How search ranks

The build writes every searchable piece of text (post sections, Game8 and Prydwen sections, Help
pages, and the game text passages) to one `passage` table, with an embedding of its title,
heading, and first 300 characters from a local model (BAAI/bge-small-en-v1.5 through fastembed,
67 MB, CPU). The build embeds only new or changed passages; embedding all of them (about 5,000)
takes under a minute on a desktop CPU.

The `search` tool takes kinds (`rules`: Help and game text; `announcements`: posts; `guides`:
Prydwen and Game8 sections) and ranks the passages of every kind asked for together, in two
stages (`czn_data.search`):

1. Three searches each nominate up to 30 candidates: BM25 over the passages' words (the
   `pg_textsearch` extension, which weighs rare words above common ones), the passages closest
   in meaning to the query's embedding, and passages whose title or heading the query spells
   closely, which catches typos.
2. A cross-encoder (Xenova/ms-marco-MiniLM-L-6-v2 through fastembed, 80 MB, CPU) reads the
   query with each candidate and scores how well it answers it. That score orders the results,
   plus up to 1 point for the candidate's source being newer than the others and up to 2 for a
   close spelling of its title. A search takes about a second.

Each Help page ranks on its own, but a Help topic appears once, at its best page's rank, with
all its pages in order; when it has several, `best_match` marks the best one. Paging counts
topics, not pages.

`total` in a reply counts the candidates, so it says nothing about how many passages are
relevant; the order does.

`search_eval.toml` holds player questions with the passages that answer them. `czn-data
eval-search` runs them against the database and reports how many find an answer in the top 5
(recall) and how high the first answer ranks (mean reciprocal rank); the daily run does the same
and exports both. Change the ranking only with these numbers before and after, and add a
question whenever a player's question went unanswered.

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

A Game8 page that fails to fetch, or comes back without its guide content, keeps its stored copy
and date, and the run fails. A game page or sitemap that fails, a game page without the kept
headings, or a selection that shrinks by more than 20% keeps every stored page;
`czn-data scrape-game8 --allow-shrink` accepts a real removal. Pages no longer selected are
deleted.

A game files run stores nothing unless every file due downloads and decodes, so the stored
files always come from one manifest. A manifest without a needed file, or a table that shrinks
by more than 20%, keeps every stored file; `czn-data scrape-gamefiles --allow-shrink` accepts a
real removal. A 403 from the CDN usually means it dropped the build folder: update `BUILD`.

The scraper exits non-zero on any failure, including a failed metrics export. It writes JSON logs
(`time`, `level`, `msg`, plus fields such as `url` and `error`) that vlagent maps to VictoriaLogs
fields. A scrape logs `run started` first, then `reading robots.txt` and each `reading sitemap`,
then `discovered pages` (`count`, and `due` to download), a `fetch progress` line about once a
minute (`done`, `total`, `fetched`, `fetch_error`), and finally `run finished` (with `seconds`)
and the metrics export outcome. Successful pages get no line of their own. A build logs `build
finished` (`ok`, `seconds`). The server logs one `tool call` line per MCP tool call (`tool`,
`arguments`, `seconds`, `reply_chars`), or `tool call failed` with `error`. The pod label
`observability.home-ops/log-format: json` makes the collector read these fields. Alert rules for
vmalert are in [deploy/alerts.yaml](deploy/alerts.yaml).

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
- `czn_scrape_pages{result}`: pages per result. `ok` and `parse_error` count the stored pages
  parsed in the run, downloaded or not; `unchanged` counts pages not parsed again because their
  HTML and the parser did not change; `fetch_error` counts failed downloads, card images included.
- `czn_scrape_fetches{result}`: pages `fetched` and pages skipped as `unchanged`.
- `czn_scrape_validation_failures{kind,field}`: page kind and field that stopped parsing.
- `czn_data_rows{table}`: rows per table after the run (Prydwen tables from the scrape, merged
  tables from the build).

The e7bot scrape sends the same kind of series under `czn_e7bot_`: `success`,
`last_success_timestamp_seconds`, `last_run_timestamp_seconds`, `duration_seconds`, `blocked`,
`requests{result}` (`ok`, `fetch_error`, `invalid`), and `validation_failures{field}`. The Stove
scrape sends the same series under `czn_stove_`, plus `articles{result}` (`fetched`, `unchanged`,
`removed`). The Game8 scrape sends the Stove series under `czn_game8_`, with `pages{result}`
(`fetched`, `unchanged`, `removed`) in place of `articles`. The game files scrape sends the
e7bot series under `czn_gamefiles_`, plus `files{result}` (`fetched`, `unchanged`); `blocked` is
1 when the CDN refused the build.

Every command that builds also sends `czn_build_success`, `czn_build_last_run_timestamp_seconds`,
and `czn_build_unmatched{entity}`.

Enrich sends its own series the same way. It exits non-zero when any row fails, and the
failed rows are tried again on the next run:

- `czn_enrich_success`, `czn_enrich_last_success_timestamp_seconds`,
  `czn_enrich_last_run_timestamp_seconds`, `czn_enrich_duration_seconds`: as for the scrape.
- `czn_enrich_failures{step}`: rows that failed per step (`gender`, `identity`, `gear`).

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
- `CZN_IDENTITY_DIR` (daily, enrich, serve): where the screenshot models for characters and
  gear are kept (about 340 MB). The image ships them there, pinned to one revision; outside it, they
  download on first use.
- `FASTEMBED_CACHE_PATH` (build, serve, daily): where the search models (embedding and
  cross-encoder) are kept. The image ships them there and sets `HF_HUB_OFFLINE=1`; outside it,
  they download on first use.
- `LOG_LEVEL` (all): defaults to `INFO`.

The server refuses to start unless `OIDC_ISSUER` or `MCP_ALLOW_UNAUTHENTICATED=true` is set.

Migrations create the schema, including the `pg_trgm` extension. `pg_trgm` is a trusted
extension, so the database owner that CloudNativePG creates can install it without superuser
rights.

Search needs the `pg_textsearch` extension (BM25), which no stock Postgres image has and which is
not trusted, so a superuser must create it and Postgres must preload it
(`shared_preload_libraries`). `postgres/Dockerfile` builds it from the PGDG package in two forms:
`server`, a Postgres 18 image for `mise run db`, the tests, and CI; and `extension`, only its
files, which CI publishes as `ghcr.io/rcdailey/czn-data-pg-textsearch`. The cluster's
CloudNativePG `Cluster` mounts that image as an extension (`spec.postgresql.extensions`), preloads
it, and runs a Debian 13 Postgres image to match the library; a CloudNativePG `Database` resource
creates the extension. The migration's own `CREATE EXTENSION IF NOT EXISTS` then does nothing.

A Postgres advisory lock lets only one process migrate at a time, so the CronJob and the
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

## MCP tools and REST routes

MCP tools are for Claude, and REST routes are for programs. Tools reply in plain text: one
`key: value` line per field, one `-` line per list item with its name first, and fields that every
item shares stated once on an `every item:` line. Replies leave out fields that only programs use,
such as ids and slugs, and anything naming where data was scraped: URLs, site names, and fetch
times. No tool takes a URL. Guide opinions are labeled with a `guide_` prefix (`guide_rating`,
`guide_review`, `guide_tier`). The build removes links and scraped site names from the text it
stores for search. Programs MUST NOT call MCP tools; add a REST route that replies in JSON
instead.

The tools are `lookup` (several names at once), `match_card_text`, `character_guide` (several
parts of one character at once), `get_encounter`, `search`, `list` (one kind of item with that
kind's filters), and `save_data_cost`.

REST routes take the same Bearer token the MCP endpoint takes and answer 401 without a valid one.
`GET /data-status` gives each source's freshness for maintainers: items stored, newest fetch,
and the last attempt with its error.
`GET /characters` lists every character's name and card art URL, for the companion app's
character picker:

```json
{"characters": [{"name": "Mei Lin", "image_url": "https://cdn.prydwen.gg/images/chaos/characters/mei-lin_card.webp"}]}
```

`POST /save-data-cost` gives the Faint Memory points of one character's Save Data, as the
`save_data_cost` tool does. The JSON body takes `character`, `cards` (each with `card`, and
optionally `version`, `divine`, and `copies`), and `removed` (card names). It answers 400 for a
body it cannot read and 404 when no character matches:

```json
{"character": "Rossweisse", "total": 40, "lines": [
  {"card": "Sword Resonance V", "copies": 1, "points": 20,
   "rules": ["unique card: free", "Divine Epiphany x1"]},
  {"removed": "Sword Slash", "points": 20, "rules": ["removed starting card"]}
], "unknown": null, "warnings": null}
```

Points follow the in-game Help topic "Save Rules for Faint Memories". Season 5's Memory cards
are free: the Help names no rule for them, and the game's counter charged nothing for one.

## Naming characters on a screenshot

`POST /identify` names the characters on a game screenshot, for the companion app. Send the
screenshot as the body (`Content-Type: image/jpeg` or `image/png`, at most 10 MB) with the same
Bearer token the MCP endpoint takes. It answers 401 without a valid token and 400 for a body that
is not a JPEG or PNG image.

The answer lists each character figure found, top row first and left to right within a row:

```json
{"model": "ccip-caformer-24-randaug-pruned", "characters": [
  {"box": [42, 165, 278, 647], "result": "match", "names": ["Rossweisse"],
   "candidates": ["Rossweisse", "Tressa", "Chizuru"], "differences": [0.012, 0.156, 0.194]}
]}
```

- `match`: one character's card art matches; `names` holds it.
- `close`: the best two are within 0.05 of each other; `names` holds both.
- `unknown`: nothing matches (difference 0.1785 or more, CCIP's published threshold), such as a
  monster icon; `names` is empty.

An anime person detector (`deepghs/anime_person_detection`) finds the figures; CCIP
(`deepghs/ccip_onnx`), a model trained to tell anime characters apart, compares each figure with the
prints `enrich` stored. Characters added since the last `enrich` cannot be named. The server handles
one screenshot at a time: about 0.5 seconds per figure on 8 free cores, and about 400 MB of memory
with the models loaded (about 700 MB while it reads a 6-figure screenshot). Art unlike the card art
(battle screens) is untested.

## Naming gear on a screenshot

`POST /identify-gear` names the equipment a game screenshot shows as icons in square slots, such
as the gear each character wears on the Equipment screen, which shows no names. The request and
its errors are those of `POST /identify`. The answer lists each slot showing a known icon, top row
first and left to right within a row, with `row` counting rows from 1:

```json
{"model": "dinov2-small", "gear": [
  {"box": [2224, 561, 2393, 730], "row": 1, "result": "match", "icons": ["relics/relic_5084"],
   "names": ["Runic Hammer"], "candidates": ["relics/relic_5084", "relics/relic_5041",
   "relics/relic_5029"], "scores": [0.94, 0.59, 0.59],
   "items": [{"name": "Runic Hammer", "rarity": "Legendary", "slot": "Weapon",
     "text": "+20% Critical Damage to ally Unique Attack Cards (cannot stack)",
     "stats": {"ATK": "28/45/62/74/82"}, "transformed": false}]}
]}
```

`items` holds each named item's rarity, slot, effect text, and stats per upgrade level (only
where the data has them; Harmonization and other forms have none), so gear can be compared
without another lookup. `transformed` marks a form a Chaos event turns worn gear into
(Harmonization, Attune, Mutation, Distortion, Bloom): one with a transform reward tag, or named
"Form: Item" after an item drawn with the same icon, since arena forms lack the tag.

- `match`: one icon is at least 0.7 similar and leads every other by 0.1 or more.
- `close`: the best two are within 0.1; `icons` and `names` hold both.
- Slots no icon reaches 0.7 for, such as empty ones, are left out, and so are slots whose icon
  names no item in `equipment` (rather than naming the next most similar gear).

Several items share one icon (a Harmonization, Mutation, or Distortion form uses its base item's
icon), so `names` lists every item drawn with the matched icon; the screen cannot tell them apart.

Slots are found from their square outlines, plus slots in the gaps of a row of equal boxes where
an icon covers an outline. Each box is compared with every icon's prints from DINOv2 small
(`onnx-community/dinov2-small-ONNX`), a general image model, on each rarity background. Gear
drawn outside a square slot (shop domes, reward cards) is not found; those screens print its
name. On the Equipment screen it takes about 0.6 seconds on 8 free cores. Icons e7bot added since
the last `enrich` cannot be named.

## Development

```sh
mise run lint
mise run test   # starts a throwaway Postgres container
mise run db     # starts the development Postgres from docker-compose.yml
```

The development database keeps its data in the `czn-data-dev` volume across
`docker compose down`, so a scraped copy stays available for testing migrations and lookups.
`docker compose down -v` deletes it.

Migrations are hand-written SQL in `src/czn_data/migrations/versions`, one file per revision,
forward only. A migration that changes tables built from page HTML calls `request_rebuild()`.
