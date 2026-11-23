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
            {"plan": "Radiant Sword Flow II x3", "limit": 120, "characters": [{"name": "Rossweisse",
              "cards": [{"card": "Sword Slash", "copies": 2}, {"card": "Radiant Sword Flow"},
                        {"card": "Sword Resonance"}],
              "gear": {"weapon": "Criticism of Discord"}}]}
            """,
        )

    @Test
    fun `an update changes only the entries it names`() {
        val after =
            update(
                start,
                """
                {"characters": [{"name": "rossweisse", "points": 20,
                  "cards": [{"card": "Radiant Sword Flow", "copies": 0},
                            {"card": "Radiant Sword Flow", "version": "II", "divine": true},
                            {"card": "Sword Slash", "copies": 1}],
                  "removed": ["Sword Slash"], "gear": {"armor": "Crown of Corruption"}}]}
                """,
            )

        val ross = after.characters.single()
        assertEquals(
            listOf(
                DeckCard("Sword Slash", copies = 1),
                DeckCard("Sword Resonance"),
                DeckCard("Radiant Sword Flow", "II", divine = true),
            ),
            ross.cards,
        )
        assertEquals(listOf("Sword Slash"), ross.removed)
        assertEquals(DeckGear("Criticism of Discord", "Crown of Corruption"), ross.gear)
        assertEquals(20, ross.points)
        assertEquals("Radiant Sword Flow II x3" to 120, after.plan to after.limit)
    }

    @Test
    fun `a new name adds a team member`() {
        val after = update(start, """{"characters": [{"name": "Tressa", "points": 0}]}""")
        assertEquals(listOf("Rossweisse", "Tressa"), after.characters.map { it.name })
    }

    @Test
    fun `invalid input is refused with a message for Claude`() {
        val refused =
            RunDecks.parse(
                """{"characters": [{"name": "Ross", "cards": [{"card": "X", "copies": -1}]}]}""",
            )
        assertTrue(refused.isFailure)
        assertTrue(RunDecks.parse("""{"limit": 0}""").isFailure)
    }
}
