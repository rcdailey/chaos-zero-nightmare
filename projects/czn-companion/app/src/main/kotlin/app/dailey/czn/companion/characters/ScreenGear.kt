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
    val items: List<ScreenItem> = emptyList(),
)

/** An item a slot's icon may show, with what comparing gear needs. */
@Serializable
data class ScreenItem(
    val name: String,
    val rarity: String,
    val slot: String,
    val text: String,
    /** Stat name to its values per upgrade level, such as "ATK" to "28/45/62/74/82". */
    val stats: Map<String, String> = emptyMap(),
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
         * The text Claude reads: a heading, then one line per named slot, such as "row 1: Runic
         * Hammer (Legendary Weapon, ATK 28/45/62/74/82): +20% Critical Damage ..."; null when
         * the server named no slot. Slots are not numbered: left-out slots (empty ones) would
         * shift the numbers.
         */
        fun describe(slots: List<ScreenSlot>): String? {
            if (slots.isEmpty()) return null
            val lines =
                slots.sortedBy(ScreenSlot::row).map { slot ->
                    "row ${slot.row}: " +
                        slot.items.joinToString(
                            if (slot.result == "close") " or " else " / ",
                            transform = ::describe,
                        )
                }
            return "Gear in slots, by row from the top, left to right:\n" + lines.joinToString("\n")
        }

        private fun describe(item: ScreenItem): String {
            val stats = item.stats.entries.joinToString("") { (stat, values) -> ", $stat $values" }
            val text = item.text.replace(Regex("\\s*\n\\s*"), " ")
            return "${item.name} (${item.rarity} ${item.slot}$stats): $text"
        }
    }
}
