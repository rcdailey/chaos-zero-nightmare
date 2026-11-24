package app.dailey.czn.companion.run

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

@Serializable
private data class DetailsRequest(
    val character: String,
    val cards: List<String>,
    val equipment: List<String>,
)

/** The data server's `POST /deck-details` answer: one match or null per name, in order. */
@Serializable
private data class DetailsResponse(
    val cards: List<CardInfo?> = emptyList(),
    val equipment: List<GearInfo?> = emptyList(),
)

/**
 * Matches the card and equipment names a run's decks use to the game's data through the data
 * server, so the deck view can draw each part and compare cards by the game's names.
 */
class DeckDetails(
    private val http: HttpClient,
    private val cznData: CznDataAuth,
) {
    private val json = Json { ignoreUnknownKeys = true }

    /**
     * Matches for [cards] and [equipment] names of [character]'s decks, by [RunDecks.infoKey];
     * names nothing matches are left out. Empty maps when the server did not answer; logged.
     */
    suspend fun find(
        character: String,
        cards: List<String>,
        equipment: List<String>,
    ): Pair<Map<String, CardInfo>, Map<String, GearInfo>> {
        if (cards.isEmpty() &&
            equipment.isEmpty()
        ) {
            return emptyMap<String, CardInfo>() to emptyMap()
        }
        return try {
            val response =
                http.post(DECK_DETAILS_URL) {
                    bearerAuth(cznData.accessToken())
                    contentType(ContentType.Application.Json)
                    setBody(json.encodeToString(DetailsRequest(character, cards, equipment)))
                    timeout { requestTimeoutMillis = TIMEOUT_MILLIS }
                }
            val body = response.bodyAsText()
            if (!response.status.isSuccess()) {
                Log.w(TAG, "deck-details failed: ${response.status} $body")
                return emptyMap<String, CardInfo>() to emptyMap()
            }
            val found = json.decodeFromString<DetailsResponse>(body)
            matched(cards, found.cards) to matched(equipment, found.equipment)
        } catch (e: CancellationException) {
            throw e
        } catch (e: SerializationException) {
            Log.w(TAG, "deck-details answered unreadably", e)
            emptyMap<String, CardInfo>() to emptyMap()
        } catch (e: Exception) {
            Log.w(TAG, "deck-details failed", e)
            emptyMap<String, CardInfo>() to emptyMap()
        }
    }

    private fun <T : Any> matched(
        names: List<String>,
        found: List<T?>,
    ) = names
        .zip(found)
        .mapNotNull { (name, info) -> info?.let { RunDecks.infoKey(name) to it } }
        .toMap()

    companion object {
        private const val TAG = "CznAdvice"
        private const val TIMEOUT_MILLIS = 20_000L

        val DECK_DETAILS_URL = CznDataAuth.MCP_URL.removeSuffix("/mcp") + "/deck-details"
    }
}
