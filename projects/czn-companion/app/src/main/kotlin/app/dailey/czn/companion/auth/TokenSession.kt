package app.dailey.czn.companion.auth

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.Json

/**
 * Stored tokens and sign-in status for one account. Provider-specific sign-in classes supply the
 * refresh request; this class decides when to call it and what a failed refresh means.
 *
 * @param refresh exchanges a refresh token for new tokens; returns null when the provider rejects
 *   it (the account must sign in again) and throws on network failure (tokens are kept).
 */
class TokenSession(
    private val store: TokenStore,
    private val name: String,
    private val accountLabel: String,
    private val now: () -> Long,
    private val refresh: suspend (refreshToken: String) -> OAuthTokens?,
) {
    private val json = Json { ignoreUnknownKeys = true }
    private val lock = Mutex()
    private val _state =
        MutableStateFlow(if (load() == null) AuthState.SignedOut else AuthState.SignedIn)
    val state: StateFlow<AuthState> = _state.asStateFlow()

    fun save(tokens: OAuthTokens) {
        store.write(name, json.encodeToString(tokens))
        _state.value = AuthState.SignedIn
    }

    /** A current access token, renewed first when it expires within five minutes. */
    suspend fun accessToken(): String =
        lock.withLock {
            val tokens = load() ?: throw SignInRequired("Sign in to $accountLabel.")
            if (tokens.expiresAtMillis - now() > REFRESH_MARGIN_MILLIS) return tokens.accessToken
            val renewed = refresh(tokens.refreshToken)
            if (renewed == null) {
                store.delete(name)
                _state.value = AuthState.NeedsSignIn("$accountLabel sign-in expired.")
                throw SignInRequired("$accountLabel sign-in expired.")
            }
            store.write(name, json.encodeToString(renewed))
            renewed.accessToken
        }

    fun clear() {
        store.delete(name)
        _state.value = AuthState.SignedOut
    }

    private fun load(): OAuthTokens? =
        store.read(name)?.let { json.decodeFromString<OAuthTokens>(it) }

    private companion object {
        const val REFRESH_MARGIN_MILLIS = 5 * 60 * 1000L
    }
}
