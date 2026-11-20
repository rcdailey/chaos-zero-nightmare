package app.dailey.czn.companion.auth

import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.request.forms.submitForm
import io.ktor.http.URLBuilder
import io.ktor.http.Url
import io.ktor.http.isSuccess
import io.ktor.http.parameters
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Sign-in to the CZN data server through its Pocket ID provider. The browser returns to the app
 * at [REDIRECT_URI], which Pocket ID allows for client `czn-mcp` (home-ops
 * `kubernetes/apps/default/czn-data/oidcclient.yaml`). Tokens are issued for the MCP endpoint as
 * the RFC 8707 resource, which the server checks as the token audience.
 */
class CznDataAuth(
    private val http: HttpClient,
    private val store: TokenStore,
    private val now: () -> Long = System::currentTimeMillis,
) {
    private val session =
        TokenSession(store, TOKENS, "the CZN data server", now) { refreshToken ->
            requestTokens(
                "grant_type" to "refresh_token",
                "refresh_token" to refreshToken,
            )
        }
    val state: StateFlow<AuthState> = session.state

    /** Starts a sign-in and returns the URL to open in the browser. */
    fun beginSignIn(): String {
        val pkce = Pkce.create()
        val state = Pkce.create().verifier
        store.write(PENDING, "$state ${pkce.verifier}")
        return URLBuilder("$ISSUER/authorize")
            .apply {
                parameters.append("client_id", CLIENT_ID)
                parameters.append("response_type", "code")
                parameters.append("redirect_uri", REDIRECT_URI)
                parameters.append("scope", "openid offline_access")
                parameters.append("code_challenge", pkce.challenge)
                parameters.append("code_challenge_method", "S256")
                parameters.append("state", state)
                parameters.append("resource", RESOURCE)
            }.buildString()
    }

    /** Finishes the sign-in with the redirect the browser delivered to the app. */
    suspend fun completeSignIn(redirect: String): Result<Unit> =
        runCatching {
            val (state, verifier) =
                store.read(PENDING)?.split(' ', limit = 2) ?: error("Start the sign-in again.")
            val params = Url(redirect).parameters
            params["error"]?.let { error("Pocket ID refused the sign-in: $it") }
            require(params["state"] == state) { "This sign-in is stale. Start it again." }
            val code = params["code"] ?: error("Pocket ID returned no code.")
            val tokens =
                requestTokens(
                    "grant_type" to "authorization_code",
                    "code" to code,
                    "redirect_uri" to REDIRECT_URI,
                    "code_verifier" to verifier,
                ) ?: error("Pocket ID rejected the sign-in. Start it again.")
            session.save(tokens)
            store.delete(PENDING)
        }

    /** A current access token; see [TokenSession.accessToken]. */
    suspend fun accessToken(): String = session.accessToken()

    fun signOut() {
        session.clear()
        store.delete(PENDING)
    }

    /** Null when Pocket ID rejects the grant; network failures propagate. */
    private suspend fun requestTokens(vararg fields: Pair<String, String>): OAuthTokens? {
        val response =
            http.submitForm(
                "$ISSUER/api/oidc/token",
                parameters {
                    fields.forEach { (name, value) -> append(name, value) }
                    append("client_id", CLIENT_ID)
                    append("resource", RESOURCE)
                },
            )
        if (!response.status.isSuccess()) return null
        val body = response.body<TokenResponse>()
        return OAuthTokens(body.accessToken, body.refreshToken, now() + body.expiresIn * 1000)
    }

    @Serializable
    private data class TokenResponse(
        @SerialName("access_token") val accessToken: String,
        @SerialName("refresh_token") val refreshToken: String,
        @SerialName("expires_in") val expiresIn: Long,
    )

    companion object {
        const val REDIRECT_SCHEME = "app.dailey.czn.companion"
        const val REDIRECT_URI = "$REDIRECT_SCHEME:/oauth/czn"
        const val MCP_URL = "https://czn.dailey.app/mcp"
        private const val RESOURCE = MCP_URL
        private const val ISSUER = "https://auth.dailey.app"
        private const val CLIENT_ID = "czn-mcp"
        private const val TOKENS = "czn-tokens"
        private const val PENDING = "czn-pending"
    }
}
