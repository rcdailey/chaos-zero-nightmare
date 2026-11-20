package app.dailey.czn.companion

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import app.dailey.czn.companion.auth.AnthropicAuth
import app.dailey.czn.companion.auth.AuthState
import app.dailey.czn.companion.auth.CznDataAuth
import app.dailey.czn.companion.auth.LoopbackSignIn
import app.dailey.czn.companion.overlay.OverlaySettings
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class SignInUiState(
    val auth: AuthState = AuthState.SignedOut,
    val awaitingCode: Boolean = false,
    val busy: Boolean = false,
    val error: String? = null,
)

data class MainUiState(
    val anthropic: SignInUiState = SignInUiState(),
    val cznData: SignInUiState = SignInUiState(),
    val buttonVisible: Boolean = true,
)

class MainViewModel(
    private val anthropic: AnthropicAuth,
    private val cznData: CznDataAuth,
    private val overlay: OverlaySettings,
) : ViewModel() {
    private val anthropicUi = MutableStateFlow(SignInUiState())
    private val cznDataUi = MutableStateFlow(SignInUiState())

    val uiState: StateFlow<MainUiState> =
        combine(
            anthropic.state,
            anthropicUi,
            cznData.state,
            cznDataUi,
            overlay.buttonVisible,
        ) { a, aUi, c, cUi, buttonVisible ->
            MainUiState(aUi.copy(auth = a), cUi.copy(auth = c), buttonVisible)
        }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), MainUiState())

    fun showFloatingButton() = overlay.setButtonVisible(true)

    /** Returns the URL the screen opens in the browser. */
    fun beginCznDataSignIn(): String {
        cznDataUi.update { it.copy(error = null) }
        return cznData.beginSignIn()
    }

    fun completeCznDataSignIn(redirect: String) {
        cznDataUi.update { it.copy(busy = true, error = null) }
        viewModelScope.launch {
            val result = cznData.completeSignIn(redirect)
            cznDataUi.update { it.copy(busy = false, error = result.exceptionOrNull()?.message) }
        }
    }

    fun cancelCznDataSignIn() {
        cznDataUi.update { it.copy(error = "Sign-in was cancelled.") }
    }

    fun signOutCznData() {
        cznData.signOut()
        cznDataUi.update { SignInUiState() }
    }

    private var anthropicSignIn: LoopbackSignIn? = null

    /**
     * Starts the redirect sign-in and returns the URL the screen opens in the browser. The browser
     * comes back to [returnUri] after the redirect.
     */
    fun beginAnthropicSignIn(returnUri: String): String {
        anthropicSignIn?.cancel()
        val signIn = anthropic.beginLoopbackSignIn(returnUri)
        anthropicSignIn = signIn
        anthropicUi.update { it.copy(awaitingCode = false, busy = true, error = null) }
        viewModelScope.launch {
            val result = signIn.finish()
            if (anthropicSignIn !== signIn) return@launch
            anthropicSignIn = null
            anthropicUi.update {
                it.copy(
                    busy = false,
                    error =
                        result.exceptionOrNull()?.let {
                            "Sign-in did not finish. Try again, or use a code."
                        },
                )
            }
        }
        return signIn.url
    }

    /** The browser closed; a sign-in that already received its redirect still finishes. */
    fun browserClosedForAnthropic() {
        anthropicSignIn?.cancel()
    }

    /** Fallback: returns the URL of a sign-in page that shows a code to paste. */
    fun beginAnthropicCodeSignIn(): String {
        anthropicSignIn?.cancel()
        anthropicUi.update { it.copy(awaitingCode = true, busy = false, error = null) }
        return anthropic.beginCodeSignIn()
    }

    fun completeAnthropicSignIn(pasted: String) {
        anthropicUi.update { it.copy(busy = true, error = null) }
        viewModelScope.launch {
            val result = anthropic.completeCodeSignIn(pasted)
            anthropicUi.update {
                it.copy(
                    busy = false,
                    awaitingCode = result.isFailure,
                    error = result.exceptionOrNull()?.message,
                )
            }
        }
    }

    fun signOutAnthropic() {
        anthropic.signOut()
        anthropicUi.update { SignInUiState() }
    }
}
