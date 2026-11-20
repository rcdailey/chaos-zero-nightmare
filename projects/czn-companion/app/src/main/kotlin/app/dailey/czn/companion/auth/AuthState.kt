package app.dailey.czn.companion.auth

import kotlinx.serialization.Serializable

/** Sign-in status of one account the app uses. */
sealed interface AuthState {
    data object SignedOut : AuthState

    data object SignedIn : AuthState

    /** Tokens were rejected or could not be renewed; the user must sign in again. */
    data class NeedsSignIn(
        val reason: String,
    ) : AuthState
}

/** Thrown when a request needs a token and the account has none that works. */
class SignInRequired(
    message: String,
) : Exception(message)

@Serializable
data class OAuthTokens(
    val accessToken: String,
    val refreshToken: String,
    val expiresAtMillis: Long,
)
