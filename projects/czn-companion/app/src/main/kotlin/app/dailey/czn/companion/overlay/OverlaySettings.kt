package app.dailey.czn.companion.overlay

import android.content.SharedPreferences
import androidx.core.content.edit
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Whether the side tab is shown. The panel's Hide button hides it; the app's main screen
 * shows it again. Survives restarts.
 */
class OverlaySettings(
    private val prefs: SharedPreferences,
) {
    private val _buttonVisible = MutableStateFlow(prefs.getBoolean(KEY_VISIBLE, true))
    val buttonVisible: StateFlow<Boolean> = _buttonVisible.asStateFlow()

    fun setButtonVisible(visible: Boolean) {
        prefs.edit { putBoolean(KEY_VISIBLE, visible) }
        _buttonVisible.value = visible
    }

    private companion object {
        const val KEY_VISIBLE = "button_visible"
    }
}
