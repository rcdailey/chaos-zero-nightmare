package app.dailey.czn.companion.overlay

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Whether the system has the overlay service bound right now. Being enabled in Accessibility
 * settings is not enough: after a reinstall the service can stay enabled but unbound. Kept in
 * memory, because the service runs in the app's process.
 */
class OverlayStatus {
    private val _running = MutableStateFlow(false)
    val running: StateFlow<Boolean> = _running.asStateFlow()

    fun setRunning(running: Boolean) {
        _running.value = running
    }
}
