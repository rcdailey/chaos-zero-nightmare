package app.dailey.czn.companion.run

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

/**
 * A Chaos run's deck state, for the run's character only: the [target] deck the run farms for,
 * the [current] deck its Save Data holds now, the Save Data limit, and what to drop first. Claude
 * changes it with `update_run_state` deltas ([RunUpdate]); the app sends it at the end of every
 * request and draws it in the deck view. [cardInfo] and [gearInfo] hold the data server's match
 * for each name the decks use ([DeckDetails]), by [infoKey].
 */
@Serializable
data class RunDeck(
    /** The Save Data storage limit in Faint Memory points; null while unknown. */
    val limit: Int? = null,
    /** Faint Memory points the current deck's Save Data holds; null while unknown. */
    val points: Int? = null,
    /** Faint Memory points the target deck would hold; null while unknown. */
    @SerialName("target_points") val targetPoints: Int? = null,
    val target: DeckParts = DeckParts(),
    val current: DeckParts = DeckParts(),
    /** Parts of the target to give up first when it costs more than [limit], in order. */
    @SerialName("drop_first") val dropFirst: List<String> = emptyList(),
    @SerialName("card_info") val cardInfo: Map<String, CardInfo> = emptyMap(),
    @SerialName("gear_info") val gearInfo: Map<String, GearInfo> = emptyMap(),
)

/** One deck: the whole card list (starting cards included), removed cards, and worn gear. */
@Serializable
data class DeckParts(
    val cards: List<DeckCard> = emptyList(),
    val removed: List<String> = emptyList(),
    val gear: DeckGear = DeckGear(),
)

/**
 * Copies of one card version a deck holds, as `save_data_cost` takes them. In an update,
 * copies 0 deletes the entry. [seen] and [fixable] matter in the current deck only: [seen] is
 * false while Claude assumed the entry (such as a starting card) without seeing it on a screen,
 * and [fixable] means Claude found that a later Epiphany can still turn this version into the
 * target's.
 */
@Serializable
data class DeckCard(
    val card: String,
    val version: String? = null,
    val divine: Boolean = false,
    val copies: Int = 1,
    val seen: Boolean = true,
    val fixable: Boolean = false,
)

@Serializable
data class DeckGear(
    val weapon: String? = null,
    val armor: String? = null,
    val trinket: String? = null,
) {
    /** Each slot's name with what it holds, in the game's order. */
    fun slots() = listOf("Weapon" to weapon, "Armor" to armor, "Trinket" to trinket)
}

/** The data server's card for a name a deck uses: its game id, version, cost, and art. */
@Serializable
data class CardInfo(
    @SerialName("game_id") val gameId: String,
    val name: String,
    @SerialName("base_name") val baseName: String,
    val variant: String = "",
    val cost: String? = null,
    @SerialName("image_url") val imageUrl: String? = null,
)

/** The data server's equipment for a name a deck uses: its game id, icon, and rarity frame. */
@Serializable
data class GearInfo(
    @SerialName("game_id") val gameId: String,
    val name: String,
    val slot: String? = null,
    val rarity: String? = null,
    @SerialName("image_url") val imageUrl: String? = null,
    @SerialName("background_url") val backgroundUrl: String? = null,
)

/**
 * Claude's `update_run_state` input: only what changed. A given field replaces its value; a
 * missing one keeps the saved value.
 */
@Serializable
data class RunUpdate(
    /** The character whose deck this run builds; the app saves it apart from [RunDeck]. */
    val character: String? = null,
    val limit: Int? = null,
    /** Points a counter on screen shows; otherwise the app costs the current deck itself. */
    val points: Int? = null,
    @SerialName("drop_first") val dropFirst: List<String>? = null,
    val target: PartsUpdate? = null,
    val current: PartsUpdate? = null,
)

/**
 * Changes to one deck. Each card entry is matched by card and version and replaces that entry;
 * [removed] replaces the whole list; each given gear slot replaces that slot.
 */
@Serializable
data class PartsUpdate(
    val cards: List<DeckCard> = emptyList(),
    val removed: List<String>? = null,
    val gear: DeckGear? = null,
) {
    /** Whether the change can alter the deck's Faint Memory points. */
    val costed get() = cards.isNotEmpty() || removed != null
}

object RunDecks {
    private val json =
        Json {
            ignoreUnknownKeys = true
            explicitNulls = false
        }

    /** Reads Claude's `update_run_state` input; fails with a message for Claude. */
    fun parse(inputJson: String): Result<RunUpdate> {
        val update =
            try {
                json.decodeFromString<RunUpdate>(inputJson)
            } catch (e: SerializationException) {
                return Result.failure(IllegalArgumentException("Invalid input: ${e.message}"))
            } catch (e: IllegalArgumentException) {
                return Result.failure(IllegalArgumentException("Invalid input: ${e.message}"))
            }
        val problem =
            when {
                update.limit != null && update.limit <= 0 -> {
                    "limit must be positive."
                }

                update.character != null && update.character.isBlank() -> {
                    "character must not be empty."
                }

                update.points != null && update.points < 0 -> {
                    "points must be 0 or more."
                }

                listOfNotNull(update.target, update.current)
                    .flatMap { it.cards }
                    .any { it.card.isBlank() || it.copies < 0 } -> {
                    "Every card needs a name and copies of 0 or more."
                }

                else -> {
                    null
                }
            }
        return problem?.let { Result.failure(IllegalArgumentException(it)) }
            ?: Result.success(update)
    }

    /** [deck] (empty when null) with [update] applied; entries it does not name stay as saved. */
    fun apply(
        deck: RunDeck?,
        update: RunUpdate,
    ): RunDeck {
        val saved = deck ?: RunDeck()
        return saved.copy(
            limit = update.limit ?: saved.limit,
            points = update.points ?: saved.points,
            dropFirst = update.dropFirst?.map(String::trim) ?: saved.dropFirst,
            target = update.target?.let { applyParts(saved.target, it) } ?: saved.target,
            current = update.current?.let { applyParts(saved.current, it) } ?: saved.current,
        )
    }

    private fun applyParts(
        before: DeckParts,
        change: PartsUpdate,
    ) = DeckParts(
        cards = applyCards(before.cards, change.cards),
        removed = change.removed?.map(String::trim) ?: before.removed,
        gear =
            change.gear?.let { gear ->
                DeckGear(
                    weapon = gear.weapon?.trim() ?: before.gear.weapon,
                    armor = gear.armor?.trim() ?: before.gear.armor,
                    trinket = gear.trinket?.trim() ?: before.gear.trinket,
                )
            } ?: before.gear,
    )

    /** The key [RunDeck.cardInfo] and [RunDeck.gearInfo] use for a card version or item. */
    fun infoKey(
        name: String,
        version: String? = null,
    ) = listOfNotNull(name, version).joinToString(" ").trim().lowercase()

    /** Card versions and items the decks name that [deck] holds no data server match for. */
    fun missingInfo(deck: RunDeck): Pair<List<String>, List<String>> {
        val parts = listOf(deck.target, deck.current)
        val cards =
            parts.flatMap { part ->
                part.cards.map { listOfNotNull(it.card, it.version).joinToString(" ") } +
                    part.removed
            }
        val gear = parts.flatMap { part -> part.gear.slots().mapNotNull { it.second } }
        return cards.distinctBy(::infoKey).filter { infoKey(it) !in deck.cardInfo } to
            gear.distinctBy(::infoKey).filter { infoKey(it) !in deck.gearInfo }
    }

    private fun applyCards(
        cards: List<DeckCard>,
        changes: List<DeckCard>,
    ): List<DeckCard> {
        val result = cards.toMutableList()
        for (change in changes) {
            val entry =
                change
                    .copy(
                        card = change.card.trim(),
                        version =
                            change.version?.trim()?.ifEmpty {
                                null
                            },
                    )
            val at = result.indexOfFirst { sameEntry(it, entry) }
            when {
                entry.copies == 0 -> if (at >= 0) result.removeAt(at)
                at >= 0 -> result[at] = entry
                else -> result += entry
            }
        }
        return result
    }

    private fun sameEntry(
        a: DeckCard,
        b: DeckCard,
    ) = a.card.equals(b.card, ignoreCase = true) &&
        (a.version ?: "").equals(b.version ?: "", ignoreCase = true)

    fun encode(deck: RunDeck): String = json.encodeToString(deck)

    fun decode(saved: String): RunDeck = json.decodeFromString(saved)

    /**
     * The state as Claude reads it: the limit, each deck with its points, what to drop first,
     * then the app's comparison of the current deck with the target ([DeckComparison]).
     */
    fun describe(deck: RunDeck): String =
        buildList {
            val limit = deck.limit?.toString() ?: "?"
            add("Save Data limit: ${deck.limit?.let { "$it points" } ?: "not known yet"}.")
            if (deck.target == DeckParts()) {
                add("Target deck: none saved yet.")
            } else {
                add("Target deck (${deck.targetPoints ?: "?"}/$limit points):")
                addAll(parts(deck.target, current = false))
            }
            add("Current deck (${deck.points ?: "?"}/$limit points):")
            addAll(parts(deck.current, current = true))
            if (deck.dropFirst.isNotEmpty()) {
                add("Drop first when over the limit: ${deck.dropFirst.joinToString("; ")}")
            }
            if (deck.target != DeckParts()) add(DeckComparison.describe(deck))
        }.joinToString("\n")

    private fun parts(
        parts: DeckParts,
        current: Boolean,
    ) = listOf(
        "  cards: ${parts.cards.joinToString("; ") { card(it, current) }.ifEmpty { "none" }}",
        "  removed: ${parts.removed.joinToString(", ").ifEmpty { "none" }}",
        "  gear: " +
            parts.gear.slots().joinToString("; ") { (slot, name) ->
                "${slot.lowercase()} ${name ?: "unknown"}"
            },
    )

    private fun card(
        card: DeckCard,
        current: Boolean,
    ) = listOfNotNull(
        card.card,
        card.version,
        "(Divine)".takeIf { card.divine },
        "x${card.copies}".takeIf { card.copies > 1 },
        "(not seen on screen)".takeIf { current && !card.seen },
        "(fixable later)".takeIf { current && card.fixable },
    ).joinToString(" ")
}
