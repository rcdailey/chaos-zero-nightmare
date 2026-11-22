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
import kotlinx.serialization.SerializationException
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
 * What the data server found on a screenshot: [line] as [ScreenCharacters.describe] writes it
 * (null when it found no figures), and [response], its raw answer with every figure's box,
 * candidates, and match scores, kept for debugging.
 */
class Identified(
    val line: String?,
    val response: String,
)

/**
 * Names the characters on a screenshot through the CZN data server, which compares each
 * character figure with every character's card art. Claude cannot tell characters apart
 * reliably from portraits, so it calls an app tool for this, and the app gives it the result
 * as text ([describe]).
 */
class ScreenCharacters(
    private val http: HttpClient,
    private val cznData: CznDataAuth,
) {
    private val json = Json { ignoreUnknownKeys = true }

    /** The characters on [jpeg], or null when the server could not answer ([postScreenshot]). */
    suspend fun identify(jpeg: ByteArray): Identified? {
        val body = postScreenshot(http, cznData, IDENTIFY_URL, jpeg) ?: return null
        val figures =
            try {
                json.decodeFromString<IdentifyResponse>(body).characters
            } catch (e: SerializationException) {
                Log.w(TAG, "identify answered $body", e)
                return null
            }
        Log.d(TAG, "identified ${figures.size} characters")
        return Identified(describe(figures), body)
    }

    companion object {
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

private const val TAG = "CznAdvice"
private const val TIMEOUT_MILLIS = 20_000L

/**
 * Sends [jpeg] (full resolution: small figures and icons match better) to one of the data
 * server's screenshot routes and returns its JSON answer, or null when it did not answer in
 * time or failed; failures are logged.
 */
internal suspend fun postScreenshot(
    http: HttpClient,
    cznData: CznDataAuth,
    url: String,
    jpeg: ByteArray,
): String? =
    try {
        val response =
            http.post(url) {
                bearerAuth(cznData.accessToken())
                contentType(ContentType.Image.JPEG)
                setBody(jpeg)
                timeout { requestTimeoutMillis = TIMEOUT_MILLIS }
            }
        val body = response.bodyAsText()
        if (response.status.isSuccess()) {
            body
        } else {
            Log.w(TAG, "$url failed: ${response.status} $body")
            null
        }
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        Log.w(TAG, "$url failed", e)
        null
    }
