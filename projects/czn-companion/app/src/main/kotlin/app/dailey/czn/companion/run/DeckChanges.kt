package app.dailey.czn.companion.run

/**
 * What one `update_run_state` call changed, as few short lines the chat shows the player. A
 * deck saved for the first time gets one summary line; later changes get one line per kind
 * (cards added, taken out, given an Epiphany; removals; worn gear), naming every item of that
 * kind. Target deck lines start with "Target:". Changes only Claude needs, such as a card
 * confirmed on screen, get no line.
 */
object DeckChanges {
    fun lines(
        before: RunDeck?,
        after: RunDeck,
    ): List<String> {
        val old = before ?: RunDeck()
        val dropOrder = after.dropFirst != old.dropFirst
        return buildList {
            if (after.limit != old.limit && after.limit != null) {
                add("Save Data limit: ${after.limit} points")
            }
            addAll(deck("Current deck", "", old.current, after.current, false))
            addAll(deck("Target", "Target: ", old.target, after.target, dropOrder))
            if (dropOrder && !old.target.isEmpty()) {
                add("Target: new order of what to drop first")
            }
        }
    }

    private fun DeckParts.isEmpty() =
        cards.isEmpty() && removed.isEmpty() && gear.slots().all { it.second == null }

    private fun deck(
        title: String,
        prefix: String,
        old: DeckParts,
        new: DeckParts,
        dropOrder: Boolean,
    ): List<String> {
        if (old.isEmpty() && !new.isEmpty()) {
            val counts =
                listOf(
                    "card" to new.cards.sumOf { it.copies },
                    "removal" to new.removed.size,
                    "equipment" to new.gear.slots().count { it.second != null },
                ).filter { it.second > 0 }
                    .map { (noun, n) -> "$n $noun" + if (n > 1 && noun != "equipment") "s" else "" }
            val drop = "drop order".takeIf { dropOrder && new.cards.isNotEmpty() }
            return listOf("$title saved: " + (counts + listOfNotNull(drop)).joinToString(", "))
        }
        val changes =
            cards(old.cards, new.cards) + removals(old.removed, new.removed) +
                gear(old.gear, new.gear)
        return changes
            .groupBy({ it.first }, { it.second })
            .map { (kind, items) ->
                val line = kind + merged(items).joinToString(", ")
                if (prefix.isEmpty()) line.replaceFirstChar(Char::uppercase) else prefix + line
            }
    }

    /** Equal items become one, with a count: "Sword Slash x2". */
    private fun merged(items: List<String>) =
        items.groupingBy { it }.eachCount().map { (item, n) -> item + times(n) }

    /** Per base card, copies of each version and Divine state before and after. */
    private fun cards(
        old: List<DeckCard>,
        new: List<DeckCard>,
    ): List<Pair<String, String>> {
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
                    DIVINE to name(card, to)
                } else {
                    EPIPHANY to "$card ${version(from)} to ${version(to)}"
                },
            )
            listOf(gone to from, added to to).forEach { (map, key) ->
                val left = map.getValue(key) - 1
                if (left > 0) map[key] = left else map.remove(key)
            }
        }
        added.forEach { (version, n) -> add(ADDED to name(card, version) + times(n)) }
        gone.forEach { (version, n) -> add(TOOK_OUT to name(card, version) + times(n)) }
    }

    private fun fixable(
        old: List<DeckCard>,
        new: List<DeckCard>,
    ) = new
        .filter { card -> card.fixable && old.none { it.fixable && same(it, card) } }
        .map { FIXABLE to name(it.card, it.version.orEmpty() to it.divine) }

    private fun same(
        a: DeckCard,
        b: DeckCard,
    ) = a.card.equals(b.card, ignoreCase = true) && a.version.orEmpty() == b.version.orEmpty()

    private fun removals(
        old: List<String>,
        new: List<String>,
    ): List<Pair<String, String>> {
        val added = new.toMutableList().apply { old.forEach { remove(it) } }
        val undone = old.toMutableList().apply { new.forEach { remove(it) } }
        return added.map { REMOVED to it } + undone.map { UNDID_REMOVAL to it }
    }

    private fun gear(
        old: DeckGear,
        new: DeckGear,
    ) = old.slots().zip(new.slots()).mapNotNull { (before, after) ->
        val (slot, name) = after
        when {
            name == before.second -> null
            name == null -> EMPTIED to slot.lowercase()
            else -> EQUIPPED to "$name (${slot.lowercase()})"
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

    // Each change kind's line start; its items follow, comma-separated.
    private const val ADDED = "added "
    private const val TOOK_OUT = "took out "
    private const val EPIPHANY = "Epiphany: "
    private const val DIVINE = "Divine Epiphany on "
    private const val FIXABLE = "fixable by a later Epiphany: "
    private const val REMOVED = "removed "
    private const val UNDID_REMOVAL = "undid the removal of "
    private const val EQUIPPED = "equipped "
    private const val EMPTIED = "emptied the slot for "
}
