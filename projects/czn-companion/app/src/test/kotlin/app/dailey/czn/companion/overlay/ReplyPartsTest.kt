package app.dailey.czn.companion.overlay

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

/**
 * How a reply's `<seen>` block is shown while streaming and once complete, and where Claude's
 * questions split the advice.
 */
class ReplyPartsTest {
    @Test
    fun `closed block splits from the advice`() {
        val parts = ReplyParts.parse("<seen>\nSave screen.\n</seen>\n\nKeep it.", complete = true)
        assertEquals(ReplyParts("Save screen.", listOf("Keep it.")), parts)
    }

    @Test
    fun `an open block or partial tag is held back while streaming`() {
        val hidden = ReplyParts(null, listOf(""))
        assertEquals(hidden, ReplyParts.parse("<seen>Save scr", complete = false))
        assertEquals(hidden, ReplyParts.parse("<se", complete = false))
    }

    @Test
    fun `an unclosed block shows as advice once the reply ends`() {
        assertEquals(
            ReplyParts(null, listOf("Save scr")),
            ReplyParts.parse("<seen>Save scr", complete = true),
        )
    }

    @Test
    fun `a reply without the block is all advice`() {
        assertEquals(
            ReplyParts(null, listOf("Pick B.")),
            ReplyParts.parse("Pick B.", complete = false),
        )
    }

    @Test
    fun `questions split the advice where Claude asked them`() {
        val text = "<seen>Map.</seen>\n\nTwo paths.\n\nTake the elite."
        val asked = text.indexOf("\n\nTake")
        assertEquals(
            ReplyParts("Map.", listOf("Two paths.", "Take the elite.")),
            ReplyParts.parse(text, complete = true, askOffsets = listOf(asked)),
        )
    }

    @Test
    fun `a question asked before any advice comes first, and one at the end comes last`() {
        val text = "<seen>Map.</seen>\n\nTake the elite."
        assertEquals(
            ReplyParts("Map.", listOf("", "Take the elite.", "")),
            ReplyParts.parse(text, complete = true, askOffsets = listOf(0, text.length)),
        )
    }
}
