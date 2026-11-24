package app.dailey.czn.companion.run

/**
 * What one `update_run_state` call changed, as short lines the chat shows the player: cards
 * added, taken out, or given an Epiphany; removals; worn gear; the limit; and the same for the
 * target deck, marked "Target:". Changes only Claude needs, such as a card confirmed on screen,
 * get no line.
 */
object DeckChanges {
    fun lines(
        before: RunDeck?,
        after: RunDeck,
        character: String? = null,
    ): List<String> {
        val old = before ?: RunDeck()
        return buildList {
            character?.let { add("Run character: $it") }
            if (after.limit != old.limit && after.limit != null) {
                add("Save Data limit: ${after.limit} points")
            }
            addAll(parts(old.current, after.current).map { it.replaceFirstChar(Char::uppercase) })
            addAll(parts(old.target, after.target).map { "Target: $it" })
            if (after.dropFirst != old.dropFirst) add("Target: new order of what to drop first")
        }
    }

    private fun parts(
        old: DeckParts,
        new: DeckParts,
    ) = cards(old.cards, new.cards) + removals(old.removed, new.removed) + gear(old.gear, new.gear)

    /** Per base card, copies of each version and Divine state before and after. */
    private fun cards(
        old: List<DeckCard>,
        new: List<DeckCard>,
    ): List<String> {
        val names = (old + new).map { it.card.lowercase() }.distinct()
        return names.flatMap { name ->
            val before = counts(old.filter { it.card.lowercase() == name })
            val after = counts(new.filter { it.card.lowercase() == name })
            val gone =
                before.mapValues { (version, n) -> n - (after[version] ?: 0) }.filterValues {
                    it >
                        0
                }
            val added =
                after.mapValues { (version, n) -> n - (before[version] ?: 0) }.filterValues {
                    it >
                        0
                }
            val label = (old + new).first { it.card.lowercase() == name }.card
            changed(label, gone.toMutableMap(), added.toMutableMap())
        } + fixable(old, new)
    }

    private fun counts(cards: List<DeckCard>) =
        cards.groupBy { it.version.orEmpty() to it.divine }.mapValues { (_, list) ->
            list.sumOf { it.copies }
        }

    /** Pairs a lost version with a gained one as an Epiphany; the rest are copies in or out. */
    private fun changed(
        card: String,
        gone: MutableMap<Pair<String, Boolean>, Int>,
        added: MutableMap<Pair<String, Boolean>, Int>,
    ) = buildList {
        while (gone.isNotEmpty() && added.isNotEmpty()) {
            val from = gone.keys.first()
            val to = added.keys.first()
            add(
                if (from.first == to.first) {
                    "Divine Epiphany on ${name(card, to)}"
                } else {
                    "Epiphany: $card ${version(from)} to ${version(to)}"
                },
            )
            listOf(gone to from, added to to).forEach { (map, key) ->
                val left = map.getValue(key) - 1
                if (left > 0) map[key] = left else map.remove(key)
            }
        }
        added.forEach { (version, n) -> add("added ${name(card, version)}${times(n)}") }
        gone.forEach { (version, n) ->
            add("took ${name(card, version)}${times(n)} out of the deck")
        }
    }

    private fun fixable(
        old: List<DeckCard>,
        new: List<DeckCard>,
    ) = new
        .filter { card -> card.fixable && old.none { it.fixable && same(it, card) } }
        .map {
            "${name(
                it.card,
                it.version.orEmpty() to it.divine,
            )} can be fixed by a later Epiphany"
        }

    private fun same(
        a: DeckCard,
        b: DeckCard,
    ) = a.card.equals(b.card, ignoreCase = true) && a.version.orEmpty() == b.version.orEmpty()

    private fun removals(
        old: List<String>,
        new: List<String>,
    ): List<String> {
        val added = new.toMutableList().apply { old.forEach { remove(it) } }
        val undone = old.toMutableList().apply { new.forEach { remove(it) } }
        return added.map { "removed $it" } + undone.map { "undid the removal of $it" }
    }

    private fun gear(
        old: DeckGear,
        new: DeckGear,
    ) = old.slots().zip(new.slots()).mapNotNull { (before, after) ->
        val (slot, name) = after
        when {
            name == before.second -> null
            name == null -> "emptied the ${slot.lowercase()} slot"
            else -> "equipped $name (${slot.lowercase()})"
        }
    }

    private fun name(
        card: String,
        version: Pair<String, Boolean>,
    ) = listOfNotNull(card, version.first.ifEmpty { null }, "(Divine)".takeIf { version.second })
        .joinToString(" ")

    private fun version(version: Pair<String, Boolean>) =
        version.first.ifEmpty { "base" } + if (version.second) " (Divine)" else ""

    private fun times(n: Int) = if (n > 1) " x$n" else ""
}
