# CZN Companion

Android app that puts a tab on the right edge of Chaos Zero Nightmare, shown only while the game
is in front. A tap or a pull opens a panel. A double tap saves a screenshot (up to 5) without
opening it; a hold sends the saved screenshots, or takes one when none is saved, and shows
Claude's advice there, using the CZN data server for card and gear lookups.

## Setup

Every tool comes from `mise.toml`; nothing else needs installing.

```sh
mise install
mise run setup    # Android SDK packages
mise run check    # format check, lint, unit tests
mise run install  # build, install on the attached device
```

`mise run preview` renders every Compose `@Preview` to PNGs under `build/previews` without a
device. `mise run format` applies formatting.

## Device setup

Installs come from `adb`, which Android's developer verification does not cover.
