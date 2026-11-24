package app.dailey.czn.companion.run

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** How Claude's update_run_state input changes the saved run state. */
class RunDeckTest {
    private fun update(
        deck: RunDeck?,
        inputJson: String,
    ) = RunDecks.apply(deck, RunDecks.parse(inputJson).getOrThrow())

    private val start =
        update(
            null,
            """
            {"limit": 120,
             "target": {"cards": [{"card": "Radiant Sword Flow", "version": "II", "copies": 2}],
                        "removed": ["Sword Slash"], "gear": {"weapon": "Runic Hammer"}},
             "current": {"cards": [{"card": "Sword Slash", "copies": 2},
                                   {"card": "Radiant Sword Flow"}, {"card": "Sword Resonance"}],
                         "gear": {"weapon": "Criticism of Discord"}}}
            """,
        )

    @Test
    fun `an update changes only the entries it names, in the deck it names`() {
        val after =
            update(
                start,
                """
                {"points": 20, "current": {
                  "cards": [{"card": "Radiant Sword Flow", "copies": 0},
                            {"card": "Radiant Sword Flow", "version": "II", "divine": true},
                            {"card": "Sword Slash", "copies": 1}],
                  "removed": ["Sword Slash"], "gear": {"armor": "Crown of Corruption"}}}
                """,
            )

        assertEquals(
            listOf(
                DeckCard("Sword Slash", copies = 1),
                DeckCard("Sword Resonance"),
                DeckCard("Radiant Sword Flow", "II", divine = true),
            ),
            after.current.cards,
        )
        assertEquals(listOf("Sword Slash"), after.current.removed)
        assertEquals(DeckGear("Criticism of Discord", "Crown of Corruption"), after.current.gear)
        assertEquals(20 to 120, after.points to after.limit)
        assertEquals(start.target, after.target)
    }

    @Test
    fun `invalid input is refused with a message for Claude`() {
        val refused =
            RunDecks.parse("""{"current": {"cards": [{"card": "X", "copies": -1}]}}""")
        assertTrue(refused.isFailure)
        assertTrue(RunDecks.parse("""{"limit": 0}""").isFailure)
        assertTrue(RunDecks.parse("""{"character": " "}""").isFailure)
    }

    private fun states(deck: RunDeck) =
        DeckComparison.compare(deck).map { listOfNotNull(it.slot, it.name, it.version) to it.state }

    @Test
    fun `each part of the current deck is compared with the target`() {
        val deck =
            update(
                null,
                """
                {"target": {"cards": [{"card": "Blade of the Cycle", "version": "III", "copies": 2},
                                      {"card": "Radiant Sword Flow", "version": "II"},
                                      {"card": "Sword Resonance", "version": "I"},
                                      {"card": "Kyrken's Apostle"}, {"card": "Aimed Fire"}],
                            "removed": ["Sword Slash", "Standing Blade"],
                            "gear": {"weapon": "Runic Hammer", "armor": "Titan Combat Suit"}},
                 "current": {"cards": [{"card": "Blade of the Cycle", "version": "III"},
                                       {"card": "Radiant Sword Flow", "version": "I", "fixable": true},
                                       {"card": "Sword Resonance", "version": "V"},
                                       {"card": "Kyrken's Apostle", "seen": false},
                                       {"card": "Ambush"}],
                             "removed": ["Sword Slash"],
                             "gear": {"weapon": "Runic Hammer", "armor": "Mask of Emotions"}}}
                """,
            )

        assertEquals(
            listOf(
                listOf("Weapon", "Runic Hammer") to MatchState.Match,
                listOf("Armor", "Mask of Emotions") to MatchState.Wrong,
                listOf("Trinket", "Trinket") to MatchState.Unchecked,
                listOf("Blade of the Cycle", "III") to MatchState.Missing,
                listOf("Radiant Sword Flow", "I") to MatchState.Fixable,
                listOf("Sword Resonance", "V") to MatchState.Wrong,
                listOf("Kyrken's Apostle") to MatchState.Unchecked,
                listOf("Aimed Fire") to MatchState.Missing,
                listOf("Ambush") to MatchState.Extra,
                listOf("Sword Slash") to MatchState.Match,
                listOf("Standing Blade") to MatchState.Missing,
            ),
            states(deck),
        )
    }

    @Test
    fun `while the target lacks copies of a card, its other versions are wrong versions`() {
        val deck =
            update(
                null,
                """
                {"target": {"cards": [{"card": "Blade of the Cycle", "version": "III", "copies": 2}]},
                 "current": {"cards": [{"card": "Blade of the Cycle", "version": "III"},
                                       {"card": "Blade of the Cycle"}]}}
                """,
            )

        assertEquals(
            listOf(
                listOf("Blade of the Cycle", "III") to MatchState.Missing,
                listOf("Blade of the Cycle") to MatchState.Wrong,
            ),
            states(deck).filter { (part, _) -> part.first().startsWith("Blade") },
        )
    }

    @Test
    fun `an Epiphany that also adds a Divine Epiphany says both`() {
        val after =
            update(
                start,
                """{"current": {"cards": [{"card": "Radiant Sword Flow", "copies": 0},
                   {"card": "Radiant Sword Flow", "version": "II", "divine": true}]}}""",
            )

        assertEquals(
            listOf("Epiphany: Radiant Sword Flow base to II (Divine)"),
            DeckChanges.lines(start, after),
        )
    }

    @Test
    fun `a card named with its Epiphany numeral matches the same card given a version`() {
        val info = CardInfo("c_1_rsp3", "Blade of the Cycle III", "Blade of the Cycle", "III")
        val deck =
            update(
                null,
                """
                {"target": {"cards": [{"card": "Blade of the Cycle", "version": "III"}]},
                 "current": {"cards": [{"card": "Blade of the Cycle III"}]}}
                """,
            ).copy(cardInfo = mapOf(RunDecks.infoKey("Blade of the Cycle III") to info))

        assertEquals(
            MatchState.Match,
            DeckComparison
                .compare(deck)
                .single {
                    it.kind ==
                        PartKind.Card
                }.state,
        )
    }

    @Test
    fun `the chat says what each update changed in either deck`() {
        val after =
            update(
                start,
                """
                {"limit": 140,
                 "current": {"cards": [{"card": "Radiant Sword Flow", "copies": 0},
                                       {"card": "Radiant Sword Flow", "version": "II"},
                                       {"card": "Sword Resonance", "divine": true},
                                       {"card": "Aimed Fire"},
                                       {"card": "Sword Slash", "copies": 1}],
                             "removed": ["Standing Blade"], "gear": {"armor": "Mask of Emotions"}},
                 "target": {"cards": [{"card": "Aimed Fire"}]}}
                """,
            )

        assertEquals(
            listOf(
                "Run character: Rossweisse",
                "Save Data limit: 140 points",
                "Took Sword Slash out of the deck",
                "Epiphany: Radiant Sword Flow base to II",
                "Divine Epiphany on Sword Resonance (Divine)",
                "Added Aimed Fire",
                "Removed Standing Blade",
                "Equipped Mask of Emotions (armor)",
                "Target: added Aimed Fire",
            ),
            DeckChanges.lines(start, after, "Rossweisse"),
        )
    }

    @Test
    fun `an update can name the run's character`() {
        assertEquals(
            "Rossweisse",
            RunDecks.parse("""{"character": "Rossweisse"}""").getOrThrow().character,
        )
    }

    @Test
    fun `Claude reads each costed character's points, or that the cost is unknown`() {
        val cost = DeckCost("Mika", 60, unknown = listOf("Zap"), warnings = listOf("4 Duplicates"))
        assertEquals(
            "Mika: 60 Faint Memory points; no card matches Zap, not counted; 4 Duplicates",
            SaveDataCost.describe("Mika", cost),
        )
        assertEquals(
            "Mika: Faint Memory cost unknown; the data server did not answer.",
            SaveDataCost.describe("Mika", null),
        )
    }
}
