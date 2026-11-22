package app.dailey.czn.companion.overlay

import android.accessibilityservice.AccessibilityService
import android.annotation.SuppressLint
import android.content.ComponentName
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.PixelFormat
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsAnimation
import android.view.WindowInsetsAnimation.Callback.DISPATCH_MODE_STOP
import android.view.WindowManager
import android.view.WindowManager.LayoutParams
import android.view.accessibility.AccessibilityEvent
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.core.content.edit
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import app.dailey.czn.companion.CompanionApp
import app.dailey.czn.companion.CompanionTheme
import app.dailey.czn.companion.claude.Screenshot
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * Accessibility service that owns the side tab, the side panel, and screenshots. It reads only
 * which app is in front, to show the tab over the game alone. The game exposes no
 * accessibility content, so the screen image is the only input about it.
 */
class OverlayService : AccessibilityService() {
    private val owner = OverlayOwner()
    private val scope = MainScope()
    private val advice by lazy {
        val app = application as CompanionApp
        AdviceController(scope, app.conversation, app.characterRoster)
    }
    private val windowManager by lazy { getSystemService(WindowManager::class.java) }
    private val prefs by lazy { getSharedPreferences("overlay", MODE_PRIVATE) }

    /** The one window that holds the side tab and the panel. */
    private var overlay: ComposeView? = null
    private var overlayParams: LayoutParams? = null
    private val panelOpen = MutableStateFlow(false)

    /** The full-screen character picker's own window, while it is open. */
    private var picker: ComposeView? = null

    /** The tab's distance from the top of the screen, in pixels. */
    private val tabY by lazy { MutableStateFlow(prefs.getInt(KEY_Y, screenHeight() / 2)) }

    private val settings by lazy { (application as CompanionApp).overlaySettings }
    private val status by lazy { (application as CompanionApp).overlayStatus }

    /**
     * Whether the game is the app in front. The service cannot read which app is already open
     * when it starts, so until the first window change it assumes what it last saw: after Android
     * kills and restarts the service over the game, the tab comes back without an app switch.
     */
    private val gameInFront by lazy {
        MutableStateFlow(prefs.getBoolean(KEY_GAME_IN_FRONT, false))
    }

    override fun onServiceConnected() {
        owner.start()
        status.setRunning(true)
        val overGame =
            combine(settings.buttonVisible, gameInFront) { visible, inGame -> visible && inGame }
        scope.launch {
            overGame.distinctUntilChanged().collect { show ->
                if (show) showOverlay() else hideOverlay()
            }
        }
        // The full-screen picker shows over the game alone; it waits while another app is in front.
        scope.launch {
            combine(overGame, advice.state) { show, state -> show && state.picker != null }
                .distinctUntilChanged()
                .collect { show -> if (show) showPicker() else hidePicker() }
        }
        // Claude's questions show in the panel, so the panel opens for them.
        scope.launch {
            advice.state
                .map { it.ask != null }
                .distinctUntilChanged()
                .collect { asking -> if (asking && overlay != null) openPanel() }
        }
        // The keyboard goes when no text box wants it, for example after a question is answered.
        scope.launch {
            advice.state
                .map { it.typing }
                .distinctUntilChanged()
                .collect { typing -> if (typing == null) setPanelFocusable(false) }
        }
    }

    private fun showPicker() {
        if (picker != null) return
        val view =
            composeView {
                val state by advice.state.collectAsStateWithLifecycle()
                state.picker?.let { CharacterPicker(it, state.roster, pickerActions) }
            }
        windowManager.addView(
            view,
            overlayParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT),
        )
        picker = view
    }

    private fun hidePicker() {
        picker?.let(windowManager::removeView)
        picker = null
    }

    private val pickerActions by lazy {
        PickerActions(
            onPick = advice::pickCharacter,
            onClose = advice::closePicker,
            onStop = advice::stop,
            onRetry = { advice.retryRoster() },
        )
    }

    /**
     * Tracks which app is in front from window changes. Only an activity coming to the front
     * counts: other windows (the keyboard, the notification shade, this app's overlay windows, a
     * launcher redrawing on a screen size change) open over the game without leaving it.
     */
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val source = event.packageName?.toString() ?: return
        val className = event.className?.toString() ?: return
        val activity = isActivity(source, className)
        Log.d(TAG, "window change: $source $className, activity=$activity")
        if (!activity) return
        gameInFront.value = source.startsWith(GAME_PACKAGE_PREFIX)
        prefs.edit { putBoolean(KEY_GAME_IN_FRONT, gameInFront.value) }
    }

    private fun isActivity(
        packageName: String,
        className: String,
    ) = try {
        packageManager.getActivityInfo(ComponentName(packageName, className), 0)
        true
    } catch (_: PackageManager.NameNotFoundException) {
        false
    }

    override fun onInterrupt() = Unit

    /** Keeps the tab and panel on screen when the screen size changes, for example on rotation. */
    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        moveTab(0f)
        resizeOverlay()
    }

    override fun onDestroy() {
        status.setRunning(false)
        overlay?.let(windowManager::removeView)
        picker?.let(windowManager::removeView)
        owner.destroy()
        scope.cancel()
        super.onDestroy()
    }

    private fun onTabTap() {
        Log.d(TAG, "tab tap, panelOpen=${panelOpen.value}")
        if (panelOpen.value) closePanel() else openPanel()
    }

    /** Reads the screen without typed text and opens the panel to show the reply. */
    private fun onTabHold() {
        Log.d(TAG, "tab hold, busy=${advice.busy}, panelOpen=${panelOpen.value}")
        // A reply still streaming is shown, not replaced.
        if (advice.busy) {
            openPanel()
            return
        }
        onSend("")
    }

    private fun onSend(question: String) {
        scope.launch {
            hideKeyboard()
            setPanelFocusable(false)
            val screenshot = captureGame()
            openPanel()
            advice.ask(question, screenshot)
        }
    }

    /**
     * Closes the keyboard, a system window that hiding the overlay leaves on screen, and waits up
     * to [KEYBOARD_HIDE_MILLIS] for its closing animation to end, so screenshots show the game.
     */
    private suspend fun hideKeyboard() {
        val view = overlay ?: return
        if (view.rootWindowInsets?.isVisible(WindowInsets.Type.ime()) != true) return
        val hidden = CompletableDeferred<Unit>()
        view.setWindowInsetsAnimationCallback(
            object : WindowInsetsAnimation.Callback(DISPATCH_MODE_STOP) {
                override fun onProgress(
                    insets: WindowInsets,
                    running: List<WindowInsetsAnimation>,
                ) = insets

                override fun onEnd(animation: WindowInsetsAnimation) {
                    if (animation.typeMask and WindowInsets.Type.ime() != 0) hidden.complete(Unit)
                }
            },
        )
        view.windowInsetsController?.hide(WindowInsets.Type.ime())
        withTimeoutOrNull(KEYBOARD_HIDE_MILLIS) { hidden.await() }
            ?: Log.w(TAG, "keyboard still closing at capture")
        view.setWindowInsetsAnimationCallback(null)
    }

    /** Screenshot of the game alone: the app's windows are hidden while it is taken. */
    private suspend fun captureGame(): Result<Screenshot> {
        overlay?.visibility = View.INVISIBLE
        delay(HIDE_SETTLE_MILLIS)
        val result = capture().map { it.toScreenshot() }
        overlay?.visibility = View.VISIBLE
        result.onSuccess {
            if (isDebuggable()) File(filesDir, "last-capture.jpg").writeBytes(it.claude)
        }
        return result
    }

    /** The panel takes keyboard focus only while the user types a follow-up. */
    private fun setPanelFocusable(focusable: Boolean) {
        val view = overlay ?: return
        val params = overlayParams ?: return
        params.flags =
            if (focusable) {
                // Touches outside a focusable window would otherwise stop reaching the game.
                (params.flags and LayoutParams.FLAG_NOT_FOCUSABLE.inv()) or
                    LayoutParams.FLAG_NOT_TOUCH_MODAL
            } else {
                params.flags or LayoutParams.FLAG_NOT_FOCUSABLE
            }
        // The window keeps its size under the keyboard; the panel's imePadding lifts its content.
        params.softInputMode = LayoutParams.SOFT_INPUT_ADJUST_NOTHING
        windowManager.updateViewLayout(view, params)
        if (!focusable) advice.stopTyping()
    }

    /**
     * Adds the one overlay window, on the right edge and the screen's full height, wide enough for
     * the tab and the open panel. Only the tab and the open panel take touches; the rest of the
     * window passes them to the game. The panel never covers the whole screen and takes focus only
     * while the user types, so system gestures and the game stay usable.
     */
    private fun showOverlay() {
        if (overlay != null) return
        val panelWidth = panelWidthPx()
        val params =
            overlayParams(panelWidth + dp(SideTabSize.width), LayoutParams.MATCH_PARENT).apply {
                flags = flags or LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH
                gravity = Gravity.END or Gravity.TOP
            }
        val view =
            composeView {
                val open by panelOpen.collectAsStateWithLifecycle()
                val y by tabY.collectAsStateWithLifecycle()
                val state by advice.state.collectAsStateWithLifecycle()
                val window = LocalView.current
                SideOverlay(
                    open = open,
                    tabY = y,
                    panelWidth = with(LocalDensity.current) { panelWidth.toDp() },
                    tabActions = tabActions,
                    onTouchableArea = { window.rootSurfaceControl?.setTouchableRegion(it) },
                ) { Panel(state, advice.draft, panelActions, askActions) }
            }
        closeOnOutsideTouch(view)
        windowManager.addView(view, params)
        overlay = view
        overlayParams = params
    }

    private fun hideOverlay() {
        closePanel()
        overlay?.let(windowManager::removeView)
        overlay = null
        overlayParams = null
    }

    /** Recreates the overlay so its size follows a new screen size, for example on rotation. */
    private fun resizeOverlay() {
        if (overlay == null) return
        hideOverlay()
        showOverlay()
    }

    private fun panelWidthPx() =
        max((screenWidth() * PANEL_WIDTH_FRACTION).roundToInt(), dp(PANEL_MIN_WIDTH_DP.dp))
            .coerceAtMost((screenWidth() * PANEL_MAX_WIDTH_FRACTION).roundToInt())

    private fun moveTab(dy: Float) {
        val maxY = screenHeight() - dp(SideTabSize.height)
        tabY.value = (tabY.value + dy.roundToInt()).coerceIn(0, maxY)
        prefs.edit { putInt(KEY_Y, tabY.value) }
    }

    private fun openPanel() {
        panelOpen.value = true
    }

    /** An outside touch is not a click on this view; the Close button is the accessible path. */
    @SuppressLint("ClickableViewAccessibility")
    private fun closeOnOutsideTouch(view: View) {
        view.setOnTouchListener { _, event ->
            if (event.actionMasked != MotionEvent.ACTION_OUTSIDE) return@setOnTouchListener false
            closePanel()
            true
        }
    }

    private fun closePanel() {
        if (!panelOpen.value) return
        setPanelFocusable(false)
        panelOpen.value = false
    }

    private val tabActions by lazy {
        SideTabActions(
            onTap = ::onTabTap,
            onHold = ::onTabHold,
            onOpen = ::openPanel,
            onClose = ::closePanel,
            onMove = ::moveTab,
        )
    }

    private val panelActions by lazy {
        PanelActions(
            onClose = ::closePanel,
            onHideButton = { settings.setButtonVisible(false) },
            onNewRun = advice::newRun,
            onCompose = { startTyping(Typing.Question) },
            onSend = ::onSend,
            onStop = advice::stop,
            onChangeCharacter = advice::openCharacterPicker,
        )
    }

    private val askActions by lazy {
        AskActions(
            onChange = advice::updateAsk,
            onSkip = advice::skipAsk,
            onStartTyping = { startTyping(Typing.Answer) },
        )
    }

    private fun startTyping(target: Typing) {
        advice.startTyping(target)
        setPanelFocusable(true)
    }

    private fun composeView(content: @Composable () -> Unit) =
        ComposeView(this).apply {
            owner.attach(this)
            setContent { CompanionTheme(dark = true, content = content) }
        }

    private fun overlayParams(
        width: Int,
        height: Int,
    ) = LayoutParams(
        width,
        height,
        LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
        LayoutParams.FLAG_NOT_FOCUSABLE,
        PixelFormat.TRANSLUCENT,
    )

    private fun screenWidth() = windowManager.maximumWindowMetrics.bounds.width()

    private fun screenHeight() = windowManager.maximumWindowMetrics.bounds.height()

    private fun dp(value: Dp) = (value.value * resources.displayMetrics.density).roundToInt()

    private fun isDebuggable() = applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0

    private companion object {
        const val TAG = "CznOverlay"
        const val KEY_Y = "button_y"
        const val KEY_GAME_IN_FRONT = "game_in_front"
        const val PANEL_MIN_WIDTH_DP = 420
        const val PANEL_WIDTH_FRACTION = 0.4f
        const val PANEL_MAX_WIDTH_FRACTION = 0.85f

        /** Prefix of the game's package; store builds differ in the suffix. */
        const val GAME_PACKAGE_PREFIX = "com.smilegate.chaoszero"

        /** Time for the hidden tab to leave the composited frame before the screenshot. */
        const val HIDE_SETTLE_MILLIS = 150L

        const val KEYBOARD_HIDE_MILLIS = 1_000L
    }
}
