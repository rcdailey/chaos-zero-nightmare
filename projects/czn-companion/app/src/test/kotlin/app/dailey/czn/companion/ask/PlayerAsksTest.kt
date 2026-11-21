package app.dailey.czn.companion.ask

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** What Claude sends to ask_player and what it reads back, and what a run saves. */
class PlayerAsksTest {
    private fun options(count: Int) =
        (1..count).joinToString(",", "[", "]") { """{"label":"O$it"}""" }

    private fun input(vararg optionCounts: Int) =
        optionCounts.joinToString(",", """{"questions":[""", "]}") {
            """{"question":"Q?","header":"H","options":${options(it)}}"""
        }

    @Test
    fun `Claude's input becomes questions, without the app's character flag`() {
        val json =
            """
            {"questions":[{"question":"Which path?","header":"Path","multi_select":true,
            "characters":true,"options":[{"label":"Elite","description":"Hard",
            "recommended":true},{"label":"Shop"}]}]}
            """.trimIndent()
        assertEquals(
            listOf(
                Question(
                    "Which path?",
                    "Path",
                    listOf(Option("Elite", "Hard", recommended = true), Option("Shop")),
                    multiSelect = true,
                ),
            ),
            PlayerAsks.parse(json).getOrThrow(),
        )
    }

    @Test
    fun `input outside the card's limits is refused with a reason`() {
        val refused =
            listOf(
                """{"questions":[]}""",
                input(2, 2, 2, 2, 2),
                input(1),
                input(5),
                """{"questions":[{"question":"","header":"H","options":${options(2)}}]}""",
                """{"questions":"nope"}""",
            )
        refused.forEach { json ->
            val result = PlayerAsks.parse(json)
            assertTrue(result.isFailure, json)
            assertTrue(
                result
                    .exceptionOrNull()
                    ?.message
                    .orEmpty()
                    .isNotBlank(),
                json,
            )
        }
        assertTrue(PlayerAsks.parse(input(2, 4, 3, 2)).isSuccess)
    }

    @Test
    fun `Claude reads each question with its answer, or that the player skipped`() {
        val questions =
            listOf(
                Question("Which path?", "Path", listOf(Option("Elite"), Option("Shop"))),
                Question("Keep?", "Keep", listOf(Option("A"), Option("B")), multiSelect = true),
            )
        assertEquals(
            "Q: Which path?\nA: Elite\nQ: Keep?\nA: A, B; typed \"and C\"",
            PlayerAsks.describe(
                questions,
                listOf(Answer(listOf("Elite")), Answer(listOf("A", "B"), "and C")),
            ),
        )
        assertEquals("The player skipped the questions.", PlayerAsks.describe(questions, null))
    }

    @Test
    fun `saved asks read back unchanged`() {
        val asks =
            listOf(
                AnsweredAsk(
                    12,
                    listOf(
                        Question("Who?", "Character", listOf(Option("Mika")), characters = true),
                    ),
                    listOf(Answer(typed = "Mei Lin")),
                ),
                AnsweredAsk(40, listOf(Question("Q?", "H", listOf(Option("A")))), null),
            )
        assertEquals(asks, PlayerAsks.decode(PlayerAsks.encode(asks)))
        assertEquals(emptyList<AnsweredAsk>(), PlayerAsks.decode(PlayerAsks.encode(emptyList())))
    }
}
