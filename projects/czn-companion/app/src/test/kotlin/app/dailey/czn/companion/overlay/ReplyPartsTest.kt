package app.dailey.czn.companion.overlay

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

/** How a reply's `<seen>` block is shown while streaming and once complete. */
class ReplyPartsTest {
    @Test
    fun `closed block splits from the advice`() {
        val parts = ReplyParts.parse("<seen>\nSave screen.\n</seen>\n\nKeep it.", complete = true)
        assertEquals(ReplyParts("Save screen.", "Keep it."), parts)
    }

    @Test
    fun `an open block or partial tag is held back while streaming`() {
        assertEquals(ReplyParts(null, ""), ReplyParts.parse("<seen>Save scr", complete = false))
        assertEquals(ReplyParts(null, ""), ReplyParts.parse("<se", complete = false))
    }

    @Test
    fun `an unclosed block shows as advice once the reply ends`() {
        assertEquals(
            ReplyParts(null, "Save scr"),
            ReplyParts.parse("<seen>Save scr", complete = true),
        )
    }

    @Test
    fun `a reply without the block is all advice`() {
        assertEquals(ReplyParts(null, "Pick B."), ReplyParts.parse("Pick B.", complete = false))
    }
}
