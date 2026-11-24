package app.dailey.czn.companion.characters

import kotlinx.serialization.Serializable

/** One gear slot the data server found on a screenshot ([ScreenIdentifier]). */
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
    /** A form a Chaos event turns worn gear into, drawn with its base item's icon. */
    val transformed: Boolean = false,
)

/**
 * The equipment a screenshot shows only as icons in slots, such as what each character wears on
 * the Equipment screen, as text for Claude, which cannot tell icons apart reliably.
 */
object ScreenGear {
    /**
     * The text Claude reads: a heading, then one line per named slot, such as "row 1: Runic
     * Hammer (Legendary Weapon, ATK 28/45/62/74/82): +20% Critical Damage ..."; null when
     * the server named no slot. Slots are not numbered: left-out slots (empty ones) would
     * shift the numbers. Transformed forms follow their base item as a conditional note, so
     * Claude assumes the base item unless the run shows the transformation.
     */
    fun describe(slots: List<ScreenSlot>): String? {
        if (slots.isEmpty()) return null
        val lines =
            slots.sortedBy(ScreenSlot::row).map { slot ->
                val separator = if (slot.result == "close") " or " else " / "
                val (forms, bases) = slot.items.partition(ScreenItem::transformed)
                val worn = bases.ifEmpty { forms }
                val note =
                    if (bases.isEmpty() || forms.isEmpty()) {
                        ""
                    } else {
                        "; if a Chaos event transformed it this run: " +
                            forms.joinToString(" / ", transform = ::describe)
                    }
                "row ${slot.row}: " + worn.joinToString(separator, transform = ::describe) +
                    note
            }
        return "Gear in slots, by row from the top, left to right:\n" + lines.joinToString("\n")
    }

    private fun describe(item: ScreenItem): String {
        val stats = item.stats.entries.joinToString("") { (stat, values) -> ", $stat $values" }
        val text = item.text.replace(Regex("\\s*\n\\s*"), " ")
        return "${item.name} (${item.rarity} ${item.slot}$stats): $text"
    }
}
