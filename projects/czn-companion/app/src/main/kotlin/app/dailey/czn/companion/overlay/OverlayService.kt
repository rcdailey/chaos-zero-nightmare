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
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
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
     * Whether the game is the app in front. False until the first window change after the service
     * starts: the service cannot read which app is already open.
     */
    private val gameInFront = MutableStateFlow(false)

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
            combine(overGame, advice.state) { show, state ->
                show && state.picker?.inline == false
            }.distinctUntilChanged()
                .collect { show -> if (show) showPicker() else hidePicker() }
        }
        // Claude's inline question shows in the panel, so the panel opens for it.
        scope.launch {
            advice.state
                .map { it.picker?.inline == true }
                .distinctUntilChanged()
                .collect { inline -> if (inline && overlay != null) openPanel() }
        }
    }

    private fun showPicker() {
        if (picker != null) return
        val view =
            composeView {
                val state by advice.state.collectAsStateWithLifecycle()
                state.picker?.let { CharacterPicker(it, pickerActions) }
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
        if (activity) gameInFront.value = source.startsWith(GAME_PACKAGE_PREFIX)
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
        setPanelFocusable(false)
        scope.launch {
            val screenshot = captureGame()
            openPanel()
            advice.ask(question, screenshot)
        }
    }

    /** Screenshot of the game alone: the app's windows are hidden while it is taken. */
    private suspend fun captureGame(): Result<ByteArray> {
        overlay?.visibility = View.INVISIBLE
        delay(HIDE_SETTLE_MILLIS)
        val result = capture().map { it.toJpeg() }
        overlay?.visibility = View.VISIBLE
        result.onSuccess { if (isDebuggable()) File(filesDir, "last-capture.jpg").writeBytes(it) }
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
        params.softInputMode = LayoutParams.SOFT_INPUT_ADJUST_RESIZE
        windowManager.updateViewLayout(view, params)
        if (!focusable) advice.stopComposing()
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
                ) { Panel(state, advice.draft, panelActions, pickerActions) }
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
        advice.cancelNewRun()
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
            onNewRun = advice::requestNewRun,
            onConfirmNewRun = advice::confirmNewRun,
            onCancelNewRun = advice::cancelNewRun,
            onCompose = {
                advice.startComposing()
                setPanelFocusable(true)
            },
            onSend = ::onSend,
            onStop = advice::stop,
            onChangeCharacter = advice::openCharacterPicker,
        )
    }

    private fun composeView(content: @Composable () -> Unit) =
        ComposeView(this).apply {
            owner.attach(this)
            setContent { CompanionTheme(content) }
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
        const val PANEL_MIN_WIDTH_DP = 420
        const val PANEL_WIDTH_FRACTION = 0.4f
        const val PANEL_MAX_WIDTH_FRACTION = 0.85f

        /** Prefix of the game's package; store builds differ in the suffix. */
        const val GAME_PACKAGE_PREFIX = "com.smilegate.chaoszero"

        /** Time for the hidden tab to leave the composited frame before the screenshot. */
        const val HIDE_SETTLE_MILLIS = 150L
    }
}
