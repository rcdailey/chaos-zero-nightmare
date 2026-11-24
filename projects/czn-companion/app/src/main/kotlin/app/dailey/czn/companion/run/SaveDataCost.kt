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

/** What the data server's `POST /save-data-cost` says one character's Save Data costs. */
@Serializable
data class DeckCost(
    val character: String,
    /** Faint Memory points of the deck's cards and removals. */
    val total: Int,
    /** What each card and removal adds to [total], and by which rules. */
    val lines: List<CostLine> = emptyList(),
    /** Card names the server matched to no card; they add no points. */
    val unknown: List<String>? = null,
    /** Rules the deck breaks, such as more Duplicates than Save Data keeps. */
    val warnings: List<String>? = null,
)

/** One card (with its copies) or one removal in a [DeckCost]. */
@Serializable
data class CostLine(
    val card: String? = null,
    val removed: String? = null,
    val copies: Int = 1,
    val points: Int,
    val rules: List<String> = emptyList(),
)

@Serializable
private data class CostCard(
    val card: String,
    val version: String?,
    val divine: Boolean,
    val copies: Int,
)

@Serializable
private data class CostRequest(
    val character: String,
    val cards: List<CostCard>,
    val removed: List<String>,
)

/**
 * Costs a character's Save Data through the data server, which applies the game's Faint Memory
 * rules, so the points in the run state come from those rules rather than Claude's arithmetic.
 */
class SaveDataCost(
    private val http: HttpClient,
    private val cznData: CznDataAuth,
) {
    private val json =
        Json {
            ignoreUnknownKeys = true
            encodeDefaults = true
        }

    /** The cost of [character]'s [deck], or null when the server did not answer; logged. */
    suspend fun cost(
        character: String,
        deck: DeckParts,
    ): DeckCost? =
        try {
            val cards = deck.cards.map { CostCard(it.card, it.version, it.divine, it.copies) }
            val response =
                http.post(SAVE_DATA_COST_URL) {
                    bearerAuth(cznData.accessToken())
                    contentType(ContentType.Application.Json)
                    setBody(json.encodeToString(CostRequest(character, cards, deck.removed)))
                    timeout { requestTimeoutMillis = TIMEOUT_MILLIS }
                }
            val body = response.bodyAsText()
            if (response.status.isSuccess()) {
                json.decodeFromString<DeckCost>(body)
            } else {
                Log.w(TAG, "save-data-cost failed: ${response.status} $body")
                null
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: SerializationException) {
            Log.w(TAG, "save-data-cost answered unreadably", e)
            null
        } catch (e: Exception) {
            Log.w(TAG, "save-data-cost failed", e)
            null
        }

    companion object {
        private const val TAG = "CznAdvice"
        private const val TIMEOUT_MILLIS = 20_000L

        val SAVE_DATA_COST_URL = CznDataAuth.MCP_URL.removeSuffix("/mcp") + "/save-data-cost"

        /** The line Claude reads for [cost], or for a character whose cost is unknown. */
        fun describe(
            name: String,
            cost: DeckCost?,
        ): String {
            if (cost == null) {
                return "$name: Faint Memory cost unknown; the data server did not answer."
            }
            val notes =
                listOfNotNull(
                    cost.unknown?.let { "no card matches ${it.joinToString(", ")}, not counted" },
                    cost.warnings?.joinToString("; "),
                )
            val parts =
                cost.lines.joinToString("; ") { line ->
                    val what =
                        line.card?.let { it + if (line.copies > 1) " x${line.copies}" else "" }
                            ?: "removed ${line.removed}"
                    "$what ${line.points} (${line.rules.joinToString(", ")})"
                }
            return "${cost.character}: ${cost.total} Faint Memory points" +
                notes.joinToString("") { "; $it" } +
                parts.takeIf { it.isNotEmpty() }?.let { "\n  per part: $it" }.orEmpty()
        }
    }
}
