# Game data investigation

Investigation from November 2026 into adding data from CZN's own game files to `czn-data`, next to
the Prydwen scrape. Nothing from it is implemented. `czn-data` now takes game facts from
wiki.e7bot.top instead, which serves the same game tables as JSON (see `projects/czn-data`);
reading the game files directly would remove that dependency. The probe scripts and downloads
lived in a temp directory and are not in the repository; the formats they relied on are recorded
below.

## Goal and requirements

- The MCP server stays self-sufficient: it scrapes Prydwen, downloads only changed game data, keeps
  game files in a mounted location, and extracts them into the database.
- No game data comes from the user's tablet. Each new game version must be fetchable without
  plugging a device in.
- Prefer existing tools over writing new ones.

## Status

Downloading and decoding game data works without the game, without a login, and without the
tablet. One piece is missing: discovering the current build folder on the download server after a
patch. Every static source in the app has been checked and none contains the address of the
service that hands out that folder.

Options still open:

1. Capture the game's network traffic on the tablet. The app has no network security config, so it
   trusts only system certificates. A proxy needs a rooted device or a repackaged app, and the
   anti-cheat library may reject a repackaged app.
2. Keep the build folder in the server's config and update it by hand after each patch, with an
   alert when downloads start failing. The folder name is in the tablet's `patch-meta.json` after
   the game updates.

## What each source provides

Game files are the better source for facts: names, element, class, stats, cards and costs, card
types, Epiphany variants, potentials (Haru has 72 nodes), keywords, partners, memory fragments,
equipment, and all text. Prydwen is the only source for opinions: ratings, teams, decks, gear and
fragment rankings, synergies, and guide text.

Prydwen already covers most of the facts, so game data adds three things:

- Epiphanies a partner unlocks for its character's cards. Prydwen does not list them.
- Exact numbers where Prydwen's text rounds or omits them.
- New content on patch day, before Prydwen writes it up. Ratings for it still wait for Prydwen.

For choosing cards, Epiphanies, and gear during a chaos run, game data matters mainly when a
partner that unlocks extra Epiphanies is equipped, and for close calls between similar options.
The game files may also hold run tables (events, item pools, drop odds) that Prydwen does not
cover. Those tables were not inspected.

## How the game gets its data

- The Android app (APK v1.0.853) ships no game data. After the first launch it downloads data
  into `/sdcard/Android/data/com.smilegate.chaoszero.stove.google/files/gameres/` (about 8.4 GB of
  chunks plus `manifest.ssra`, `manifest.ssra.etag`, `patch-meta.json`, `patch-complete.json`).
- The PC launcher's install list is public at
  `https://chaoszero-dl.game.onstove.com/game/dpms_STOVE_CHAOSZERO/STOVE_CHAOSZERO_<n>.json`.
  Versions 54 to 57 return 200; 58 and later return 403. Version 57 lists 44 entries totalling
  136 MB, which is the launcher only. The PC game downloads its data the same way the app does.
- At startup the game asks a version service for the current data location. The answer names a
  build folder on the download server, for example `1.0.85302/5P340UKEBKQV`. The folder is not in
  the app: app version 1.0.853 was sent folder `1.0.85302`. This lets the publisher ship data and
  hotfixes without an app release.

The tablet's `patch-meta.json` records the result of that lookup:

```txt
server-env  https://czn-live-down.game.playstove.com/patch/1.0.85302/5P340UKEBKQV
            ssra.context=pack-166-a248701680af ssra.version=166 · fn=buildConfig
layout      .../patch/1.0.85302/5P340UKEBKQV/pack-166-a248701680af/main
            subdir=main · fn=resolveRemoteArchiveFromServer
engine      ssra  ssra.url=https://czn-live-down.game.playstove.com/patch/1.0.85302/5P340UKEBKQV/
            ssra.context=pack-166-a248701680af ssra.version=166 archive.ssra.enable=1
complete-marker  pack-166-a248701680af  build_number=166
```

## Download server

Base for build 166:

```txt
https://czn-live-down.game.playstove.com/patch/1.0.85302/5P340UKEBKQV/pack-166-a248701680af/main/
```

- Requests need `User-Agent: Yuna WebSocket/0.1 Supercreative inc.`; without it the server
  (CloudFront over S3) returns 403. Guessed folders also return 403.
- Range requests return 206, so single files can be read out of large chunks.
- Files: `manifest.ssra.zst` and `chunks/<group>_b<NN>_0.ssrc`. Hotfix chunks are
  `chunks/hotfix_<NNNN>.ssrc` (group `0xFFFF`). The `.zst` chunk variants were not tested.

## File formats

### Compression

`manifest.ssra.zst` and compressed file entries are zstd. Some frames start with a legacy first
byte of `0x25` to `0x27` before the `b5 2f fd` magic; set it to `0x28` before decompressing.

### Manifest (`manifest.ssra`)

Little-endian. Header: `<4sIIIIIQQQQQ>` = magic, version, unknown, chunk count, file count, flags,
string table offset, string table size, chunk table offset, file table offset, unknown.

- Group names: a `GRPS` block right after the string table. Count and section size (`<II>` at +4),
  then 24-byte records from +16 (`u16` group index at +0, `u32` name offset at +4), then the name
  section.
- Chunk table: 32-byte records `<IHHQQQ>` = chunk id, group, unknown, uncompressed size,
  compressed size, unknown. A chunk's offset within its group is the running sum of uncompressed
  sizes, ordered by chunk id.
- File table: 40-byte records `<IIQIIIIBBHI>`; the used fields are offset within group, compressed
  size, uncompressed size, name offset, compressed flag, encrypted flag, group, flags. Skip entries
  with flag bit 1 set. Names have a leading `/`.
- Build 166 lists 1,798 `db/*.db` tables and 9 `text/<lang>/text.db` tables.

To read a file: walk the group's chunks in offset order, range-request the bytes that overlap the
file, then zstd-decompress if compressed. Encrypted entries were not needed and not decoded.

### Data tables (`.db`)

- Encrypted with a 256-byte repeating XOR key. Find the rotation of the key that turns the first 5
  bytes into `PLPcK`, then XOR the whole file. The key (from the `cznrip` reference code):

```txt
91AE4ED4644F585162EC1BD5EF24ADDBAF838242AEF51E97804B134FFD8CE5BB4F6E3E6451147CDF56C318E5E964C999
C0D95CC860822E6B418BE465D79A036DBF67AB3DA72AB1023A4561F444E5CE858D23EA10FEB4899151AD7E43FF3E2419
A97B4DD3AF4EF5C829E5AF4ACE9436F6B6B6382E9DFD26642099011A4899089C9D4B9F80BBB00A4CC73255CE1F78646E
91C9C12313F5D840DC51457010D37D19615BB69888B42B19E749F993C00337E9332F89B320C173A5653848788798A771
739E72DBC84C7946597149BDDAE4E3BD1A17856C85A555CFA24F6352D005933B50042BE0BA4C708DE8EBB52059B2059C
9BFE90D8923DF74B43911BBC00BB6BFA
```

- Decrypted layout is a `PLPcK` hash-table container (header byte `0x26` at offset 5). `u32` bucket
  count at offset 21; 40-bit bucket table pointer at offset 25 (one byte high, then `u32` low),
  buckets start 5 bytes after it. Each bucket holds a 40-bit pointer to a chain of entries:
  `<IBBI>` (name length at +6, data size at +7), 40-bit next pointer at +10, name at +15, data
  right after the name.
- Table entries: `\trows` and `\tcols` (`u32`), `\t<n>` column names, `\t\t<r>` row pointers. A row
  pointer names another entry whose data is the row's values separated by `\0`.
- Text tables (`text/<lang>/text.db`) have columns `id` and `text`. Other tables reference text by
  key, for example `char_base@name@1003`.
- File names follow `db/<source>@<table>.db`, for example `db/char_base@char_combatant.db`,
  `db/partner_base@char_partner.db`, `db/potential_node@potential_node.db`,
  `db/item_piece@item.db`, `db/keyword_list@keyword_list.db`, `db/card(camille1009)@card.db`.
- Verified on Haru (character 1062) and her Anchor card (`c_1062_srt1`).

## Version service lookup

The address of the version service is the missing piece. What the app shows about the request:

- The client script (`title.js`, `queryEntry`) reads the setting `verinfo.api`, fails with "invalid
  verinfo api" when it is empty, then calls the native `_async_load_remote_environment`. The result
  is stored as JSON in the setting `verinfo.data`.
- Native code (`libssr.so`) builds the request from `verinfo.api` plus `app.id`, `app.package`,
  `build.number`, `platform`, `user.lang`, `os.lang`, and optional `&lang=`, `&force_review=1`,
  `&gcs=`. It sends headers `X-App-Id`, `X-App-NS` (setting `app.ns`), `User-Client-Alias`
  (setting `user.client.alias`), and `If-None-Match`. A setting `env.encrypt.key` is read next to
  this code. Log line: `[app] verinfo url :` (around `0x1cb4dec`).
- The same code reads `app.api` and `develoment.app.api` and logs `[app] Change Api .. `.
- Response shape, from a dev stub in the client script: `_appid`, `_entry_timestamp`,
  `maintenance`, and `world` -> `asia`/`global` -> `live`/`review` with `app.api`, `build.version`,
  `cdn.url`, `cdn.context`, `ssra.url`, `ssra.context`, `ssra.version`, `title_movie_cdn`,
  `verinfo.status`. The stub's `cdn.url` is
  `https://devpatch11.supercreative.kr:3043/ssrdev_patch/`.

Places checked for the value of `verinfo.api`, none of which contain it:

- Java code (jadx): `Cocos2dxActivity.setenv` is called only for device info (platform version,
  mcc, device id). Nothing writes `verinfo.api`.
- `AndroidManifest.xml` and `res/values/strings.xml`: Stove SDK settings only
  (`com.stove.environment` and similar).
- Stove SDK config endpoint: public, returns 200, and fills `getStoveConstant` with auth, auth-ui,
  base, iap, log, and view settings only. URL:
  `https://api.onstove.com/ssg/access/clients/sdk/<ver>/com.smilegate.chaoszero.stove.google`
  with query `policy_grp=mobilesdk&client_lang=en&device_nation=US&game_version=<ver>`.
- Stove DPMS `https://api.onstove.com/dpms/game/v3.1/live_version`: needs a login token, and it
  versions the PC launcher anyway.
- Startup scripts (`assets/pre/bin/<arch>/init.jbin`): an unencrypted `PLPcK` container of compiled
  V8 scripts (`init.js`, `title.js`, `entry_util.js`, `publisher_stove.js`, and others). They read
  `verinfo.api` but never set it, and contain no URL and no hex or base64 blobs.
- Native setter `__app_setenv` (`0x1beb420`): no direct calls and no relocations point to it, so
  only the script engine calls it. `decrypt_value` sits next to it, so scripts may pass encrypted
  values.
- `setLaunchCommandLineEnv` (native, `0x1c08da0`): no Java code calls it.
- `assets/sdata/*` (15 high-entropy files, 1 KB to 8 MB): named only in `librhcore.so` and
  `libGvt.so`, Stove's anti-cheat ("game_security_dev"). Not game settings.
- logcat at launch: 1,394 lines from the game process (Android, video, audio, Firebase, Google Play
  Games). The release build sends no engine log lines, so `[app] verinfo url` never appears.
- Tablet's readable game folder: `data.indices`, `device_os.pw`, `gameres`, `temp`. No copy of
  `verinfo.api` or `app.ns`. `temp/inet/` holds cached HTTP downloads (news images only).

Other hosts seen in the binaries: `q3-czn-pc2sckgame.game.playstove.com`,
`czn-qa-down.game.playstove.com`, `czn.cdn.net.cn` (Chinese publisher build), and
`api-main-common.gate8.com` (Stove sandbox).

Unchecked: native code that builds the address without a plain string (the scanner below only
follows `adrp` + `add` pairs to string data), and anything delivered at runtime after the Stove
login.

## Existing tools

A GitHub search found no public project that asks the version service or downloads CZN or Epic
Seven data from the server. Every tool reads files a game install already downloaded. Commits are
the ones inspected.

- `cznrip/ChaosZeroNightmare-AssetRipper` (`1c4df9e9`): local `manifest.ssra` reader.
  `archive/SSRArchive.cpp` and `parsers/DBParser.cpp` are the references for the formats above.
- `CeciliaBot/EpicSevenAssetRipper` (`40e73aea`): Epic Seven and CZN local extractor (`SSRA`
  class). No network code.
- `NineS11942/ChaosZero-Toolkit` (`46498080`): decompiled client scripts (`javascript/title.js`,
  `init.js`) and local manifest edits.
- `yporoc/ChaosZeroNightmare-Toolkit-Extra` and `-Toolkit-Android`: local manifest and etag edits.
- `iRyzor/EmberLauncher` (`230b2012`): Linux PC launcher. Uses the DPMS endpoints above, so it
  covers the launcher, not game data.
- `atom-tr/yuna-unpack`: archived Epic Seven extractor, no network code.

## Reproducing the setup

- APK download: `mise exec cargo:apkeep@1.1.0 -- apkeep -a com.smilegate.chaoszero.stove.google
  -d apk-pure -o 'acknowledge_dangers=true,arch=arm64-v8a' <dir>`.
- Decompile: `mise exec github:skylot/jadx@1.5.3 java@21 -- jadx -q -j 4 --no-res -d <out> <apk>`;
  use `--no-src` instead of `--no-res` to get resources and the manifest.
- Native string references: disassemble `libssr.so` (arm64) with capstone, track `adrp` page
  loads, and match `add` immediates against the address of a string in `.rodata`. This finds the
  code that loads a setting name and lists nearby strings. It misses strings built other ways.
- Device: Lenovo TB323FU. The installed package is `com.smilegate.chaoszero.stove.google`.
