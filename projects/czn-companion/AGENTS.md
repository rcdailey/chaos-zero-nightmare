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
  coroutines and `StateFlow`, kotlinx.serialization, Ktor client for HTTP and SSE, Room 3
  (`androidx.room3`, KSP) for run state.
- Room: commit the exported schemas under `app/schemas`. A schema change bumps the database
  version with a migration (`AutoMigration` when it suffices); never a destructive fallback,
  which wipes the user's runs.
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
  changed it; keep sign-in in `auth/AnthropicAuth.kt` and the identity line and betas in
  `claude/MessagesRequest.kt`, nowhere else. Images, tools, and the MCP connector (beta
  `mcp-client-2025-11-20`) work with these tokens (verified 2026-11-20).

## UI design

Follow Material 3 Expressive as the Compose Material 3 guide on developer.android.com describes
it.

- Wrap every window and `@Preview` in the app theme (`CompanionTheme`, `MainActivity.kt`), not a
  bare `MaterialTheme`, so previews match the device. Take color, type, shape, and motion from
  `MaterialTheme` roles, pairing each container with its `on` role; no literal colors, text sizes,
  or shapes.
- Use stock Material 3 components before custom ones, including the Expressive ones (toolbars,
  button groups, flexible app bars). Opt into an experimental Material API only when no stable
  component fits; the material3 release notes list which remain experimental.
- Icons: Material Symbols vector drawables from fonts.google.com/icons in `res/drawable`;
  `material-icons` is no longer published. Replace `?attr/` tints with `?android:attr/` (no
  AppCompat).
- Accessibility: touch targets at least 48dp; `contentDescription` on every non-text element that
  conveys meaning or acts, `null` on decorative ones; layouts stay usable at 200% font scale;
  never convey state by color alone.
- Size layouts by window width, not device: the phone is compact (<600dp), the tablet medium or
  expanded.
- Activity screens are edge-to-edge (enforced at API 36): inset content with `Scaffold` padding or
  `WindowInsets.safeDrawing`. Predictive back no longer calls `onBackPressed`; use `BackHandler`.
- Annotate screen previews with `@PreviewLightDark` and `@PreviewFontScale` so `mise run preview`
  renders each variant.

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

- Device waits: poll for the expected state (window focus, logcat line, capture) every second or
  less with a timeout; never a fixed sleep longer than 2 seconds. To wait for advice, clear logcat
  (`adb logcat -c`) before sending, then poll for `CznAdvice: reply done` and capture at once.
- Tap in device pixels (`adb shell wm size`); a capture shown to you may be scaled down. The
  overlay's touchable region is in `adb shell dumpsys window windows`.
- `android layout` and `android input` selectors read accessibility nodes: they work on the app's
  screens, not the game's. In the game, drive input by screen coordinates from a capture.
