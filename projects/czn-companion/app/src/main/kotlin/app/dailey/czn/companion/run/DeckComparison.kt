package app.dailey.czn.companion.run

/**
 * How one part of the current deck compares with the target. [Unchecked] means Claude has not
 * seen that part on a screen yet, so the saved value may not be what the player has.
 */
enum class MatchState(
    val label: String,
) {
    Match("Matches target"),
    Fixable("Fixable later"),
    Wrong("Does not match"),
    Missing("Missing"),
    Extra("Not in target"),
    Unchecked("Not seen on screen yet"),
}

enum class PartKind { Card, Removal, Gear }

/**
 * One part of the current deck, or of the target that the current deck lacks, with how it
 * compares. [name] is the name Claude saved; [version] is null for a base card; [slot] names a
 * gear slot; [state] is null for gear the target leaves open. [key] finds the part's
 * [RunDeck.cardInfo] or [RunDeck.gearInfo].
 */
data class ComparedPart(
    val kind: PartKind,
    val name: String,
    val state: MatchState?,
    val version: String? = null,
    val divine: Boolean = false,
    val copies: Int = 1,
    val slot: String? = null,
    val note: String? = null,
) {
    val key get() = RunDecks.infoKey(name, version)
}

/**
 * Compares a run's current deck with its target. A card is one base card in one version:
 * names resolve through the data server's matches when the deck has them, so "Blade of the
 * Cycle III" and "Blade of the Cycle" with version III are the same card.
 */
object DeckComparison {
    /** The current deck's parts against the target: gear slots, then cards, then removals. */
    fun compare(deck: RunDeck): List<ComparedPart> = gear(deck) + cards(deck) + removals(deck)

    /** One line per state with the parts in it, for Claude. */
    fun describe(deck: RunDeck): String {
        val parts = compare(deck).filter { it.state != null }
        val lines =
            MatchState.entries.mapNotNull { state ->
                parts
                    .filter { it.state == state }
                    .takeIf { it.isNotEmpty() }
                    ?.joinToString(
                        "; ",
                        prefix = "  ${state.label.lowercase()}: ",
                        transform = ::label,
                    )
            }
        return (listOf("The current deck against the target:") + lines).joinToString("\n")
    }

    private fun label(part: ComparedPart) =
        listOfNotNull(
            part.slot?.lowercase(),
            "removal".takeIf { part.kind == PartKind.Removal },
            part.name,
            part.version,
            "(Divine)".takeIf { part.divine },
            "x${part.copies}".takeIf { part.copies > 1 },
            part.note?.let { "($it)" },
        ).joinToString(" ")

    private fun gear(deck: RunDeck) =
        deck.current.gear.slots().zip(deck.target.gear.slots()).map { (worn, wanted) ->
            val (slot, name) = worn
            val target = wanted.second
            val state =
                when {
                    name == null -> MatchState.Unchecked
                    target == null -> null
                    sameGear(deck, name, target) -> MatchState.Match
                    else -> MatchState.Wrong
                }
            ComparedPart(
                PartKind.Gear,
                name ?: slot,
                state,
                slot = slot,
                note = target?.takeIf { state == MatchState.Wrong }?.let { "target $it" },
            )
        }

    private fun sameGear(
        deck: RunDeck,
        a: String,
        b: String,
    ): Boolean {
        val ids = listOf(a, b).map { deck.gearInfo[RunDecks.infoKey(it)]?.gameId }
        return if (ids.all { it != null }) ids[0] == ids[1] else a.equals(b, ignoreCase = true)
    }

    private fun cards(deck: RunDeck): List<ComparedPart> {
        val unused = deck.current.cards.toMutableList()
        val parts = mutableListOf<ComparedPart>()
        for (wanted in deck.target.cards) {
            val same = unused.filter { base(deck, it) == base(deck, wanted) }
            val exact =
                same.firstOrNull {
                    version(deck, it) == version(deck, wanted) && it.divine == wanted.divine
                }
            val short = exact == null || exact.copies < wanted.copies
            if (exact != null) {
                unused -= exact
                val state = if (short) MatchState.Missing else MatchState.Match
                val note = "${exact.copies} of ${wanted.copies} copies".takeIf { short }
                parts += held(exact, state, note)
            }
            // While the target still lacks copies, other versions of the card are its wrong
            // versions; once it has them all, they are extra copies.
            val others = (same - setOfNotNull(exact)).takeIf { short }.orEmpty()
            unused -= others.toSet()
            others.forEach { other ->
                val state = if (other.fixable) MatchState.Fixable else MatchState.Wrong
                parts += held(other, state, "target ${versionLabel(wanted)}")
            }
            if (exact == null && others.isEmpty()) parts += part(wanted, MatchState.Missing, null)
        }
        return parts + unused.map { held(it, MatchState.Extra, null) }
    }

    /** A current card: [state] unless Claude has not seen it on a screen. */
    private fun held(
        card: DeckCard,
        state: MatchState,
        note: String?,
    ) = if (card.seen) part(card, state, note) else part(card, MatchState.Unchecked, null)

    private fun part(
        card: DeckCard,
        state: MatchState,
        note: String?,
    ) = ComparedPart(
        PartKind.Card,
        card.card,
        state,
        card.version,
        card.divine,
        card.copies,
        note = note,
    )

    private fun removals(deck: RunDeck): List<ComparedPart> {
        val done = deck.current.removed.toMutableList()
        val parts =
            deck.target.removed.map { wanted ->
                val match = done.firstOrNull { removedBase(deck, it) == removedBase(deck, wanted) }
                match?.let(done::remove)
                ComparedPart(
                    PartKind.Removal,
                    match ?: wanted,
                    if (match != null) MatchState.Match else MatchState.Missing,
                )
            }
        return parts + done.map { ComparedPart(PartKind.Removal, it, MatchState.Extra) }
    }

    private fun info(
        deck: RunDeck,
        card: DeckCard,
    ) = deck.cardInfo[RunDecks.infoKey(card.card, card.version)]

    private fun base(
        deck: RunDeck,
        card: DeckCard,
    ) = (info(deck, card)?.baseName ?: card.card).lowercase()

    private fun version(
        deck: RunDeck,
        card: DeckCard,
    ) = (card.version ?: info(deck, card)?.variant).orEmpty().uppercase()

    private fun removedBase(
        deck: RunDeck,
        name: String,
    ) = (deck.cardInfo[RunDecks.infoKey(name)]?.baseName ?: name).lowercase()

    private fun versionLabel(card: DeckCard) =
        listOfNotNull(
            card.version?.let { "Epiphany $it" } ?: "base",
            "Divine".takeIf { card.divine },
        ).joinToString(", ")
}
