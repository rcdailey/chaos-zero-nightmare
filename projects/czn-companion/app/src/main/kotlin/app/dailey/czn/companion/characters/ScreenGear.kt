package app.dailey.czn.companion.characters

import android.util.Log
import app.dailey.czn.companion.auth.CznDataAuth
import io.ktor.client.HttpClient
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

/** One gear slot the data server found on a screenshot (`POST /identify-gear`). */
@Serializable
data class ScreenSlot(
    /** The slot's row on screen, counted from 1 at the top. */
    val row: Int,
    /** `match` (the items drawn with one icon) or `close` (with either of two icons). */
    val result: String,
    val names: List<String> = emptyList(),
)

@Serializable
private data class IdentifyGearResponse(
    val gear: List<ScreenSlot>,
)

/**
 * Names the equipment a screenshot shows only as icons in slots, such as what each character
 * wears on the Equipment screen, through the CZN data server, which compares each slot with every
 * equipment icon. Claude cannot tell icons apart reliably, so it calls an app tool for this.
 */
class ScreenGear(
    private val http: HttpClient,
    private val cznData: CznDataAuth,
) {
    private val json = Json { ignoreUnknownKeys = true }

    /** The gear on [jpeg], or null when the server could not answer ([postScreenshot]). */
    suspend fun identify(jpeg: ByteArray): Identified? {
        val body = postScreenshot(http, cznData, IDENTIFY_GEAR_URL, jpeg) ?: return null
        val slots =
            try {
                json.decodeFromString<IdentifyGearResponse>(body).gear
            } catch (e: SerializationException) {
                Log.w(TAG, "identify-gear answered $body", e)
                return null
            }
        Log.d(TAG, "identified ${slots.size} gear slots")
        return Identified(describe(slots), body)
    }

    companion object {
        private const val TAG = "CznAdvice"

        val IDENTIFY_GEAR_URL = CznDataAuth.MCP_URL.removeSuffix("/mcp") + "/identify-gear"

        /**
         * The line Claude reads, such as "Gear in slots, by row from the top, left to right: row
         * 1: Runic Hammer; Crown of Corruption or Fairy King's Crown. row 2: Big Game Hunter /
         * Harmonization: Big Game Hunter"; null when the server named no slot.
         */
        fun describe(slots: List<ScreenSlot>): String? {
            if (slots.isEmpty()) return null
            val rows =
                slots.groupBy(ScreenSlot::row).toSortedMap().map { (row, inRow) ->
                    "row $row: " +
                        inRow.joinToString("; ") { slot ->
                            slot.names.joinToString(if (slot.result == "close") " or " else " / ")
                        }
                }
            return "Gear in slots, by row from the top, left to right: " +
                rows.joinToString(". ")
        }
    }
}
