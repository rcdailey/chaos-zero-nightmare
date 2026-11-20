package app.dailey.czn.companion.auth

import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.request.header
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.URLBuilder
import io.ktor.http.contentType
import io.ktor.http.isSuccess
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Sign-in to the user's Anthropic (Claude subscription) account.
 *
 * Anthropic does not document this flow; it mirrors what opencode and pi do and was verified on
 * 2026-11-20. The browser redirects to a local port the app listens on; if that fails, the
 * browser can instead show a code the user pastes back. If Anthropic changes the flow, this class
 * is the only place to update.
 */
class AnthropicAuth(
    private val http: HttpClient,
    private val store: TokenStore,
    private val now: () -> Long = System::currentTimeMillis,
) {
    private val session =
        TokenSession(store, TOKENS, "your Anthropic account", now) { refreshToken ->
            requestTokens(TokenRequest(grantType = "refresh_token", refreshToken = refreshToken))
        }
    val state: StateFlow<AuthState> = session.state

    /**
     * Starts a sign-in whose browser redirect lands on a local port, so no code is pasted. Open
     * [LoopbackSignIn.url], then call [LoopbackSignIn.finish]; [returnUri] is where the browser
     * goes after the redirect, to bring the app back.
     */
    fun beginLoopbackSignIn(returnUri: String): LoopbackSignIn {
        val pkce = Pkce.create()
        val receiver = LoopbackReceiver.open(returnUri)
        return LoopbackSignIn(authorizeUrl(pkce, receiver.redirectUri), receiver) {
            receiver.use {
                runCatching {
                    val params = receiver.await()
                    require(params["state"] == pkce.verifier) { "Stale sign-in. Start it again." }
                    val code = params["code"] ?: error("Anthropic returned no code.")
                    exchange(code, pkce.verifier, receiver.redirectUri)
                }
            }
        }
    }

    /** Fallback sign-in: the browser shows a code the user pastes into [completeCodeSignIn]. */
    fun beginCodeSignIn(): String {
        val pkce = Pkce.create()
        store.write(PENDING, pkce.verifier)
        return authorizeUrl(pkce, COPY_CODE_REDIRECT_URI)
    }

    /** Finishes the sign-in started by [beginCodeSignIn] with the code the browser showed. */
    suspend fun completeCodeSignIn(pasted: String): Result<Unit> =
        runCatching {
            val verifier = store.read(PENDING) ?: error("Start the sign-in again.")
            val code = parsePastedCode(pasted).getOrThrow()
            require(code.state == null || code.state == verifier) {
                "This code belongs to a different sign-in. Start the sign-in again."
            }
            exchange(code.code, verifier, COPY_CODE_REDIRECT_URI)
            store.delete(PENDING)
        }

    private suspend fun exchange(
        code: String,
        verifier: String,
        redirectUri: String,
    ) {
        val tokens =
            requestTokens(
                TokenRequest(
                    grantType = "authorization_code",
                    code = code,
                    state = verifier,
                    redirectUri = redirectUri,
                    codeVerifier = verifier,
                ),
            ) ?: error("Anthropic rejected the sign-in. Start it again.")
        session.save(tokens)
    }

    private fun authorizeUrl(
        pkce: Pkce,
        redirectUri: String,
    ): String =
        URLBuilder(AUTHORIZE_URL)
            .apply {
                parameters.append("code", "true")
                parameters.append("client_id", CLIENT_ID)
                parameters.append("response_type", "code")
                parameters.append("redirect_uri", redirectUri)
                parameters.append("scope", SCOPES)
                parameters.append("code_challenge", pkce.challenge)
                parameters.append("code_challenge_method", "S256")
                parameters.append("state", pkce.verifier)
            }.buildString()

    /** A current access token; see [TokenSession.accessToken]. */
    suspend fun accessToken(): String = session.accessToken()

    fun signOut() {
        session.clear()
        store.delete(PENDING)
    }

    /** Null when Anthropic rejects the grant; network failures propagate. */
    private suspend fun requestTokens(request: TokenRequest): OAuthTokens? {
        val response =
            http.post(TOKEN_URL) {
                contentType(ContentType.Application.Json)
                header(HttpHeaders.UserAgent, USER_AGENT)
                setBody(request)
            }
        if (!response.status.isSuccess()) return null
        val body = response.body<TokenResponse>()
        return OAuthTokens(
            accessToken = body.accessToken,
            refreshToken = body.refreshToken,
            expiresAtMillis = now() + body.expiresIn * 1000,
        )
    }

    @Serializable
    private data class TokenRequest(
        @SerialName("grant_type") val grantType: String,
        @SerialName("client_id") val clientId: String = CLIENT_ID,
        val code: String? = null,
        val state: String? = null,
        @SerialName("redirect_uri") val redirectUri: String? = null,
        @SerialName("code_verifier") val codeVerifier: String? = null,
        @SerialName("refresh_token") val refreshToken: String? = null,
    )

    @Serializable
    private data class TokenResponse(
        @SerialName("access_token") val accessToken: String,
        @SerialName("refresh_token") val refreshToken: String,
        @SerialName("expires_in") val expiresIn: Long,
    )

    private companion object {
        const val CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
        const val AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
        const val TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
        const val COPY_CODE_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
        const val SCOPES =
            "org:create_api_key user:profile user:inference user:sessions:claude_code " +
                "user:mcp_servers user:file_upload"
        const val USER_AGENT = "axios/1.13.6"
        const val TOKENS = "anthropic-tokens"
        const val PENDING = "anthropic-pending"
    }
}

/**
 * A browser sign-in in progress. [finish] waits for the redirect and stores the tokens; [cancel]
 * stops the wait (for example when the user closes the browser), and [finish] then fails.
 */
class LoopbackSignIn internal constructor(
    val url: String,
    private val receiver: LoopbackReceiver,
    private val complete: suspend () -> Result<Unit>,
) {
    suspend fun finish(): Result<Unit> = complete()

    fun cancel() = receiver.close()
}

/** What the user pasted after browser sign-in, split into the code and the optional state. */
data class PastedCode(
    val code: String,
    val state: String?,
)

/**
 * Accepts the forms Anthropic's sign-in produces: `code#state` as shown on the callback page, the
 * full callback URL, a `code=...&state=...` query string, or a bare code.
 */
fun parsePastedCode(input: String): Result<PastedCode> {
    val value = input.trim()
    if (value.isEmpty()) return Result.failure(IllegalArgumentException("Paste the code first."))
    val query =
        when {
            value.startsWith("http://") || value.startsWith("https://") -> {
                value.substringAfter('?', "").substringBefore('#')
            }

            "code=" in value -> {
                value
            }

            else -> {
                null
            }
        }
    if (query != null) {
        val params =
            query
                .split('&')
                .mapNotNull { pair ->
                    pair.split('=', limit = 2).takeIf { it.size == 2 }?.let { it[0] to it[1] }
                }.toMap()
        val code =
            params["code"]?.takeIf { it.isNotEmpty() }
                ?: return Result.failure(IllegalArgumentException("No code found in that text."))
        return Result.success(PastedCode(code, params["state"]))
    }
    val code = value.substringBefore('#')
    val state = value.substringAfter('#', "").ifEmpty { null }
    return Result.success(PastedCode(code, state))
}
