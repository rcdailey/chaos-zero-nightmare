# CZN Companion

Android app that puts a floating button over Chaos Zero Nightmare, shown only while the game is
in front. A tap opens a panel; a long press takes a screenshot and shows Claude's advice there,
using the CZN data server for card and gear lookups.

## Setup

Every tool comes from `mise.toml`; nothing else needs installing.

```sh
mise install
mise run setup    # Android SDK packages
mise run check    # format check, lint, unit tests
mise run install  # build, install on the attached device, launch
```

`mise run preview` renders every Compose `@Preview` to PNGs under `build/previews` without a
device. `mise run format` applies formatting.

## Device setup

Installs come from `adb`, which Android's developer verification does not cover.
