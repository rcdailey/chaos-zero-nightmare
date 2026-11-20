package app.dailey.czn.companion.overlay

import android.accessibilityservice.AccessibilityService
import android.annotation.SuppressLint
import android.content.ComponentName
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.PixelFormat
import android.os.SystemClock
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
import kotlinx.coroutines.launch
import java.io.File
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * Accessibility service that owns the floating button, the side panel, and screenshots. It reads
 * only which app is in front, to show the button over the game alone. The game exposes no
 * accessibility content, so the screen image is the only input about it.
 */
class OverlayService : AccessibilityService() {
    private val owner = OverlayOwner()
    private val scope = MainScope()
    private val advice by lazy {
        AdviceController(
            scope,
            (application as CompanionApp).conversation,
        )
    }
    private var panelParams: LayoutParams? = null
    private val windowManager by lazy { getSystemService(WindowManager::class.java) }
    private val prefs by lazy { getSharedPreferences("overlay", MODE_PRIVATE) }
    private var button: ComposeView? = null
    private var buttonParams: LayoutParams? = null
    private var panel: ComposeView? = null

    private val settings by lazy { (application as CompanionApp).overlaySettings }
    private val status by lazy { (application as CompanionApp).overlayStatus }

    /** When an outside touch last closed the panel; a button tap right after it must not reopen. */
    private var panelClosedAt = 0L

    /**
     * Whether the game is the app in front. False until the first window change after the service
     * starts: the service cannot read which app is already open.
     */
    private val gameInFront = MutableStateFlow(false)

    override fun onServiceConnected() {
        owner.start()
        status.setRunning(true)
        scope.launch {
            combine(settings.buttonVisible, gameInFront) { visible, inGame -> visible && inGame }
                .distinctUntilChanged()
                .collect { show -> if (show) showButton() else hideButton() }
        }
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

    /** Keeps the button on screen when the screen size changes, for example on rotation. */
    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        moveButton(0f, 0f)
    }

    override fun onDestroy() {
        status.setRunning(false)
        button?.let(windowManager::removeView)
        panel?.let(windowManager::removeView)
        owner.destroy()
        scope.cancel()
        super.onDestroy()
    }

    private fun onButtonTap() {
        if (panel != null) {
            closePanel()
            return
        }
        if (SystemClock.uptimeMillis() - panelClosedAt < TAP_AFTER_CLOSE_MILLIS) return
        Log.d(TAG, "button tap: open panel")
        openPanel()
    }

    /** Reads the screen without typed text and opens the panel to show the reply. */
    private fun onButtonLongPress() {
        Log.d(TAG, "button long press, busy=${advice.busy}, panelOpen=${panel != null}")
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
        val views = listOfNotNull(button, panel)
        views.forEach { it.visibility = View.INVISIBLE }
        delay(HIDE_SETTLE_MILLIS)
        val result = capture().map { it.toJpeg() }
        views.forEach { it.visibility = View.VISIBLE }
        result.onSuccess { if (isDebuggable()) File(filesDir, "last-capture.jpg").writeBytes(it) }
        return result
    }

    /** The panel takes keyboard focus only while the user types a follow-up. */
    private fun setPanelFocusable(focusable: Boolean) {
        val view = panel ?: return
        val params = panelParams ?: return
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

    private fun showButton() {
        if (button != null) return
        val params =
            overlayParams(LayoutParams.WRAP_CONTENT, LayoutParams.WRAP_CONTENT).apply {
                gravity = Gravity.TOP or Gravity.START
                x = prefs.getInt(KEY_X, Int.MAX_VALUE).coerceAtMost(screenWidth() - buttonSizePx())
                y = prefs.getInt(KEY_Y, screenHeight() / 2)
            }
        val view =
            composeView {
                FloatingButton(
                    onTap = ::onButtonTap,
                    onLongPress = ::onButtonLongPress,
                    onDrag = { moveButton(it.x, it.y) },
                )
            }
        windowManager.addView(view, params)
        button = view
        buttonParams = params
    }

    private fun hideButton() {
        closePanel()
        button?.let(windowManager::removeView)
        button = null
        buttonParams = null
    }

    private fun moveButton(
        dx: Float,
        dy: Float,
    ) {
        val params = buttonParams ?: return
        params.x = (params.x + dx.roundToInt()).coerceIn(0, screenWidth() - buttonSizePx())
        params.y = (params.y + dy.roundToInt()).coerceIn(0, screenHeight() - buttonSizePx())
        windowManager.updateViewLayout(button, params)
        prefs.edit {
            putInt(KEY_X, params.x)
            putInt(KEY_Y, params.y)
        }
    }

    /**
     * The panel never covers the whole screen and takes focus only while the user types, so system
     * gestures and the game stay usable. A touch outside it closes it, as does its close button.
     */
    private fun openPanel() {
        if (panel != null) return
        val width =
            max((screenWidth() * PANEL_WIDTH_FRACTION).roundToInt(), dp(PANEL_MIN_WIDTH_DP))
                .coerceAtMost((screenWidth() * PANEL_MAX_WIDTH_FRACTION).roundToInt())
        val params =
            overlayParams(width, LayoutParams.MATCH_PARENT).apply {
                flags = flags or LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH
                gravity = Gravity.END or Gravity.TOP
            }
        val view =
            composeView {
                val state by advice.state.collectAsStateWithLifecycle()
                Panel(state, advice.draft, panelActions)
            }
        closeOnOutsideTouch(view)
        windowManager.addView(view, params)
        panel = view
        panelParams = params
        // Keep the button above the panel so the next tap can close it.
        button?.let {
            windowManager.removeView(it)
            windowManager.addView(it, buttonParams)
        }
    }

    // An outside touch is not a click on this view; the Close button is the accessible path.
    @SuppressLint("ClickableViewAccessibility")
    private fun closeOnOutsideTouch(view: View) {
        view.setOnTouchListener { _, event ->
            if (event.actionMasked != MotionEvent.ACTION_OUTSIDE) return@setOnTouchListener false
            panelClosedAt = SystemClock.uptimeMillis()
            closePanel()
            true
        }
    }

    private fun closePanel() {
        advice.stopComposing()
        advice.cancelNewRun()
        panel?.let(windowManager::removeView)
        panel = null
        panelParams = null
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

    private fun buttonSizePx() = dp(BUTTON_SIZE_DP)

    private fun dp(value: Int) = (value * resources.displayMetrics.density).roundToInt()

    private fun isDebuggable() = applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0

    private companion object {
        const val TAG = "CznOverlay"
        const val KEY_X = "button_x"
        const val KEY_Y = "button_y"
        const val BUTTON_SIZE_DP = 56
        const val PANEL_MIN_WIDTH_DP = 420
        const val PANEL_WIDTH_FRACTION = 0.4f
        const val PANEL_MAX_WIDTH_FRACTION = 0.85f
        const val TAP_AFTER_CLOSE_MILLIS = 400L

        /** Prefix of the game's package; store builds differ in the suffix. */
        const val GAME_PACKAGE_PREFIX = "com.smilegate.chaoszero"

        /** Time for the hidden button to leave the composited frame before the screenshot. */
        const val HIDE_SETTLE_MILLIS = 150L
    }
}
