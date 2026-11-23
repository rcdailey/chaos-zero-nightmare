package app.dailey.czn.companion.overlay

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

/** Where Claude's questions split a reply's advice. */
class ReplyPartsTest {
    @Test
    fun `a reply without questions is all advice, an old screen description included`() {
        val old = "<seen>Map.</seen>\n\nTake the elite."
        assertEquals(ReplyParts(listOf(old)), ReplyParts.parse(old))
    }

    @Test
    fun `questions split the advice where Claude asked them`() {
        val text = "Two paths.\n\nTake the elite."
        val asked = text.indexOf("\n\nTake")
        assertEquals(
            ReplyParts(listOf("Two paths.", "Take the elite.")),
            ReplyParts.parse(text, askOffsets = listOf(asked)),
        )
    }

    @Test
    fun `a question asked before any advice comes first, and one at the end comes last`() {
        val text = "Take the elite."
        assertEquals(
            ReplyParts(listOf("", "Take the elite.", "")),
            ReplyParts.parse(text, askOffsets = listOf(0, text.length)),
        )
    }
}
