# czn-companion

Sideloaded app for Android 16+ (Lenovo Legion Y700 Gen 5 on a China ROM, Nothing Phone 3).
`README.md` lists the mise tasks.

- Toolchain: install the JDK, Android command-line tools, and SDK packages only through
  `mise.toml` and its tasks. MUST NOT use system package managers or Android Studio. Pin Gradle
  with the wrapper; Kotlin comes from the Gradle plugin. Install the Android CLI (`android`)
  through mise too; use `android sdk` for SDK packages (`sdkmanager` is deprecated).
- Versions: current stable releases, pinned only in `gradle/libs.versions.toml`; no alpha, beta,
  or RC artifacts.
- Stack: Kotlin (K2, current stable language features), Jetpack Compose with Material 3,
  coroutines and `StateFlow`, kotlinx.serialization, Ktor client for HTTP and SSE.
- Structure: one Gradle module and constructor injection without a DI framework. Split modules
  only for reuse or visibility; add Hilt when manual wiring outgrows one file.
- Architecture: follow the developer.android.com architecture recommendations: unidirectional
  data flow, one immutable UI state per state holder, repositories own data, no `Context` in
  state holders.
- Checks: Android Lint with warnings as errors, and ktlint through Spotless.
- Capture and overlay belong to one AccessibilityService: `takeScreenshot` needs no per-session
  consent, unlike MediaProjection. The game exposes no accessibility nodes; screenshots are the
  only input. Screenshot calls are rate-limited (`ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT`).
- Compose inside a service-owned window MUST have a `LifecycleOwner` and
  `SavedStateRegistryOwner` set on its view tree, or it crashes on attach.
- Install with `adb install` (exempt from developer verification). Enabling the service needs
  Settings > Apps > the app > Allow restricted settings once. Android 17 Advanced Protection
  blocks non-accessibility-tool services; keep it off on test devices.
- Claude access: Anthropic account OAuth as opencode and pi do it: PKCE copy-code flow, then a
  Bearer token with the `oauth-2025-04-20` and `claude-code-20250219` betas. The first system
  block MUST be exactly the Claude Code identity line, with the app's prompt in a later block;
  any other first block returns a bare 429. Anthropic does not document this flow and has
  changed it; keep it in one class. Images, tools, and the MCP connector work with these tokens
  (verified 2026-11-20).

## Feedback loop

Verify each change at the cheapest level that exercises it:

1. `mise run check` (format, lint, JVM unit tests) after every change; `mise run format` fixes
   formatting.
2. Compose UI: `mise run preview` renders every `@Preview` to PNGs under `build/previews` without
   a device; read the PNG to check layout.
3. Device for anything involving the overlay, screenshots, sign-in redirects, or the game, which
   runs only on the physical devices. `mise run install` (every attached device, or
   `ANDROID_SERIAL`), reproduce, then read `adb logcat` filtered to the app and an
   `adb exec-out screencap -p` capture. If no device is attached, ask the user to connect one;
   do not substitute the emulator.
4. Emulator (KVM is available; `android emulator`, headless) only for the app's own screens.

- `android layout` and `android input` selectors read accessibility nodes: they work on the app's
  screens, not the game's. In the game, drive input by screen coordinates from a capture.
