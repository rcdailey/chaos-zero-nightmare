package app.dailey.czn.companion.ask

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** How the player answers Claude's questions on the card. */
class AskProgressTest {
    private val path = Question("Which path?", "Path", listOf(Option("Elite"), Option("Shop")))
    private val keep =
        Question(
            "Which cards to keep?",
            "Keep",
            listOf(Option("Blade"), Option("Flow"), Option("Strike")),
            multiSelect = true,
        )

    @Test
    fun `one single-choice question is sent by the tap`() {
        val done = AskProgress(listOf(path)).pick(1)
        assertTrue(done.complete)
        assertEquals(listOf(Answer(picked = listOf("Shop"))), done.answers())
    }

    @Test
    fun `a multiple-choice question waits for its button`() {
        val ticked = AskProgress(listOf(keep)).pick(2).pick(0)
        assertFalse(ticked.complete)
        val done = ticked.next()
        assertTrue(done.complete)
        assertEquals(listOf(Answer(picked = listOf("Blade", "Strike"))), done.answers())
    }

    @Test
    fun `unticking a choice removes it, and nothing ticked cannot move on`() {
        val none = AskProgress(listOf(keep)).pick(0).pick(0)
        assertEquals(none, none.next())
    }

    @Test
    fun `a typed answer replaces a single choice and needs text to move on`() {
        val other = AskProgress(listOf(path, keep)).pick(0).back().toggleOther()
        assertEquals(other, other.next())
        val typed = other.type("  Rest  ").next()
        assertEquals(1, typed.step)
        assertEquals(Answer(typed = "Rest"), typed.answers()[0])
    }

    @Test
    fun `a typed answer adds to multiple choices`() {
        val done =
            AskProgress(listOf(keep))
                .pick(1)
                .toggleOther()
                .type("the 0-cost one")
                .next()
        assertEquals(listOf(Answer(listOf("Flow"), "the 0-cost one")), done.answers())
    }

    @Test
    fun `several questions end on a review, sent only on confirm`() {
        val review = AskProgress(listOf(path, keep)).pick(0).pick(1).next()
        assertTrue(review.reviewing)
        assertFalse(review.complete)
        val done = review.confirm()
        assertTrue(done.complete)
        assertEquals(
            listOf(Answer(picked = listOf("Elite")), Answer(picked = listOf("Flow"))),
            done.answers(),
        )
    }

    @Test
    fun `going back keeps answers, and an edit after the review returns to it`() {
        val review = AskProgress(listOf(path, keep)).pick(0).pick(1).next()
        val back = review.back().back()
        assertEquals(0, back.step)
        assertEquals(setOf(0), back.draft?.picked)
        val edited = review.edit(0).pick(1)
        assertTrue(edited.reviewing)
        assertEquals(listOf("Shop"), edited.answers()[0].picked)
    }

    @Test
    fun `the first question has no back`() {
        val first = AskProgress(listOf(path, keep))
        assertFalse(first.canGoBack)
        assertEquals(first, first.back())
    }
}
