package app.dailey.czn.companion.run

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

/**
 * A Chaos run's deck state: the target plan, the Save Data storage limit, and per team member
 * the whole deck, removals, worn gear, and Faint Memory points. Claude changes it with
 * `update_run_state` deltas ([RunUpdate]); the app sends it at the end of every request.
 */
@Serializable
data class RunDeck(
    val plan: String = "",
    /** The Save Data storage limit in Faint Memory points; null while unknown. */
    val limit: Int? = null,
    val characters: List<DeckCharacter> = emptyList(),
    /** Parts of the plan to give up first when it costs more than [limit]. */
    @SerialName("drop_first") val dropFirst: List<String> = emptyList(),
)

@Serializable
data class DeckCharacter(
    val name: String,
    /** Faint Memory points this character's Save Data holds now; null while unknown. */
    val points: Int? = null,
    /** The whole deck, starting cards included. */
    val cards: List<DeckCard> = emptyList(),
    val removed: List<String> = emptyList(),
    val gear: DeckGear = DeckGear(),
)

/**
 * Copies of one card version the deck holds, as `save_data_cost` takes them. In an update,
 * copies 0 deletes the entry.
 */
@Serializable
data class DeckCard(
    val card: String,
    val version: String? = null,
    val divine: Boolean = false,
    val copies: Int = 1,
)

@Serializable
data class DeckGear(
    val weapon: String? = null,
    val armor: String? = null,
    val trinket: String? = null,
)

/**
 * Claude's `update_run_state` input: only what changed. A given field replaces its value; a
 * missing one keeps the saved value.
 */
@Serializable
data class RunUpdate(
    /** The character whose deck this run builds; the app saves it apart from [RunDeck]. */
    val character: String? = null,
    val plan: String? = null,
    val limit: Int? = null,
    @SerialName("drop_first") val dropFirst: List<String>? = null,
    val characters: List<CharacterUpdate> = emptyList(),
)

/**
 * Changes to one character, matched by name; a new name adds the character. Each card entry is
 * matched by card and version and replaces that entry; each given gear slot replaces that slot.
 */
@Serializable
data class CharacterUpdate(
    val name: String,
    val points: Int? = null,
    val cards: List<DeckCard> = emptyList(),
    val removed: List<String>? = null,
    val gear: DeckGear? = null,
)

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

                update.characters.any { it.name.isBlank() } -> {
                    "Every character needs a name."
                }

                update.characters
                    .flatMap {
                        it.cards
                    }.any { it.card.isBlank() || it.copies < 0 } -> {
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
        val characters = saved.characters.toMutableList()
        for (change in update.characters) {
            val at =
                characters.indexOfFirst {
                    it.name.equals(
                        change.name.trim(),
                        ignoreCase = true,
                    )
                }
            val before = characters.getOrNull(at) ?: DeckCharacter(change.name.trim())
            val after =
                before.copy(
                    points = change.points ?: before.points,
                    cards = applyCards(before.cards, change.cards),
                    removed = change.removed?.map(String::trim) ?: before.removed,
                    gear =
                        change.gear?.let { gear ->
                            DeckGear(
                                weapon = gear.weapon ?: before.gear.weapon,
                                armor = gear.armor ?: before.gear.armor,
                                trinket = gear.trinket ?: before.gear.trinket,
                            )
                        } ?: before.gear,
                )
            if (at < 0) characters += after else characters[at] = after
        }
        return saved.copy(
            plan = update.plan?.trim() ?: saved.plan,
            limit = update.limit ?: saved.limit,
            dropFirst = update.dropFirst ?: saved.dropFirst,
            characters = characters,
        )
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

    /** The state as Claude reads it: short lines per character, then the plan. */
    fun describe(deck: RunDeck): String =
        buildList {
            add("Save Data limit: ${deck.limit?.let { "$it points" } ?: "not known yet"}.")
            deck.characters.forEach { character ->
                val points = character.points?.toString() ?: "?"
                add("${character.name}: $points/${deck.limit ?: "?"} points")
                add(
                    "  cards: ${character.cards.joinToString(
                        "; ",
                        transform = ::card,
                    ).ifEmpty { "none" }}",
                )
                add("  removed: ${character.removed.joinToString(", ").ifEmpty { "none" }}")
                add("  gear: ${gear(character.gear)}")
            }
            add("Plan:\n${deck.plan.ifEmpty { "none yet" }}")
            if (deck.dropFirst.isNotEmpty()) {
                add("Drop first when over the limit: ${deck.dropFirst.joinToString("; ")}")
            }
        }.joinToString("\n")

    private fun card(card: DeckCard) =
        listOfNotNull(
            card.card,
            card.version,
            "(Divine)".takeIf { card.divine },
            "x${card.copies}".takeIf { card.copies > 1 },
        ).joinToString(" ")

    private fun gear(gear: DeckGear) =
        "weapon ${gear.weapon ?: "unknown"}; armor ${gear.armor ?: "unknown"}; " +
            "trinket ${gear.trinket ?: "unknown"}"
}
