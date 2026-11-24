package app.dailey.czn.companion.characters

import android.util.Log
import app.dailey.czn.companion.auth.CznDataAuth
import io.ktor.client.HttpClient
import io.ktor.client.plugins.timeout
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.forms.formData
import io.ktor.client.request.forms.submitFormWithBinaryData
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.Headers
import io.ktor.http.HttpHeaders
import io.ktor.http.isSuccess
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject

/**
 * What the data server found on one screenshot: the characters line ([ScreenCharacters.describe])
 * and the gear text ([ScreenGear.describe]), each null when it found none, and [response], its
 * raw answer for this screenshot with every box, candidate, and match score, kept for debugging.
 */
class ShotIdentity(
    val characters: String?,
    val gear: String?,
    val response: String,
)

@Serializable
private data class ShotAnswer(
    val characters: List<ScreenFigure> = emptyList(),
    val gear: List<ScreenSlot> = emptyList(),
)

/**
 * Names the characters and the gear in slots on screenshots through the CZN data server, which
 * compares each figure with every character's card art and each slot with every equipment icon,
 * in one call for all of a request's screenshots (`POST /identify-screenshots`).
 */
class ScreenIdentifier(
    private val http: HttpClient,
    private val cznData: CznDataAuth,
) {
    private val json = Json { ignoreUnknownKeys = true }

    /**
     * What the server found on each of [jpegs] (full resolution: small figures and icons match
     * better), in the same order; null when it did not answer in time or failed, which is logged.
     */
    suspend fun identify(jpegs: List<ByteArray>): List<ShotIdentity>? {
        val body = post(jpegs) ?: return null
        val shots =
            try {
                json.parseToJsonElement(body).jsonObject.getValue("screenshots").jsonArray.map {
                    val answer = json.decodeFromJsonElement<ShotAnswer>(it)
                    ShotIdentity(
                        ScreenCharacters.describe(answer.characters),
                        ScreenGear.describe(answer.gear),
                        it.toString(),
                    )
                }
            } catch (e: SerializationException) {
                Log.w(TAG, "identify-screenshots answered $body", e)
                return null
            } catch (e: IllegalArgumentException) {
                Log.w(TAG, "identify-screenshots answered $body", e)
                return null
            }
        if (shots.size != jpegs.size) {
            Log.w(TAG, "identify-screenshots answered ${shots.size} of ${jpegs.size} screenshots")
            return null
        }
        Log.d(TAG, "identified ${shots.size} screenshots")
        return shots
    }

    private suspend fun post(jpegs: List<ByteArray>): String? =
        try {
            val response =
                http.submitFormWithBinaryData(
                    IDENTIFY_URL,
                    formData {
                        jpegs.forEachIndexed { index, jpeg ->
                            append(
                                "image",
                                jpeg,
                                Headers.build {
                                    append(
                                        HttpHeaders.ContentType,
                                        ContentType.Image.JPEG.toString(),
                                    )
                                    append(
                                        HttpHeaders.ContentDisposition,
                                        "filename=\"$index.jpg\"",
                                    )
                                },
                            )
                        }
                    },
                ) {
                    bearerAuth(cznData.accessToken())
                    // The server reads one screenshot at a time.
                    timeout { requestTimeoutMillis = TIMEOUT_MILLIS_PER_SHOT * jpegs.size }
                }
            val body = response.bodyAsText()
            if (response.status.isSuccess()) {
                body
            } else {
                Log.w(TAG, "$IDENTIFY_URL failed: ${response.status} $body")
                null
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.w(TAG, "$IDENTIFY_URL failed", e)
            null
        }

    private companion object {
        const val TAG = "CznAdvice"
        const val TIMEOUT_MILLIS_PER_SHOT = 20_000L

        /** On the same host as the MCP endpoint, which takes the same token. */
        val IDENTIFY_URL = CznDataAuth.MCP_URL.removeSuffix("/mcp") + "/identify-screenshots"
    }
}
