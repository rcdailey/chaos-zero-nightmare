package app.dailey.czn.companion.characters

import android.util.Log
import app.dailey.czn.companion.auth.CznDataAuth
import io.ktor.client.HttpClient
import io.ktor.client.plugins.timeout
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.contentType
import io.ktor.http.isSuccess
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** One character figure the data server found on a screenshot (`POST /identify`). */
@Serializable
data class ScreenFigure(
    /** `match` (one name), `close` (two names, too close to choose), or `unknown` (none). */
    val result: String,
    val names: List<String> = emptyList(),
)

@Serializable
private data class IdentifyResponse(
    val characters: List<ScreenFigure>,
)

/**
 * Names the characters on a screenshot through the CZN data server, which compares each
 * character figure with every character's card art. Claude cannot tell characters apart
 * reliably from portraits, so the app asks before each screenshot request and gives Claude the
 * result as text ([describe]).
 */
class ScreenCharacters(
    private val http: HttpClient,
    private val cznData: CznDataAuth,
) {
    private val json = Json { ignoreUnknownKeys = true }

    /**
     * The characters on [jpeg] (full resolution: small figures match better), as the line
     * [describe] writes, or null when the server could not answer in time. Advice goes on
     * without the line, so every failure is only logged.
     */
    suspend fun identify(jpeg: ByteArray): String? =
        try {
            val response =
                http.post(IDENTIFY_URL) {
                    bearerAuth(cznData.accessToken())
                    contentType(ContentType.Image.JPEG)
                    setBody(jpeg)
                    timeout { requestTimeoutMillis = TIMEOUT_MILLIS }
                }
            val body = response.bodyAsText()
            if (response.status.isSuccess()) {
                val figures = json.decodeFromString<IdentifyResponse>(body).characters
                Log.d(TAG, "identified ${figures.size} characters")
                describe(figures)
            } else {
                Log.w(TAG, "identify failed: ${response.status} $body")
                null
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.w(TAG, "identify failed", e)
            null
        }

    companion object {
        private const val TAG = "CznAdvice"
        private const val TIMEOUT_MILLIS = 20_000L

        /** On the same host as the MCP endpoint, which takes the same token. */
        val IDENTIFY_URL = CznDataAuth.MCP_URL.removeSuffix("/mcp") + "/identify"

        /**
         * The line Claude reads, such as "Characters on screen, top to bottom, left to right:
         * Rossweisse, Maribell or Veronica, unidentified"; null when the server found none.
         */
        fun describe(figures: List<ScreenFigure>): String? {
            if (figures.isEmpty()) return null
            val names =
                figures.joinToString(", ") { figure ->
                    figure.names.joinToString(" or ").ifEmpty { "unidentified" }
                }
            return "Characters on screen, top to bottom, left to right: $names"
        }
    }
}
