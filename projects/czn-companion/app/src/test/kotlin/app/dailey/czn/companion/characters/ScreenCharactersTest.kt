package app.dailey.czn.companion.characters

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

/** The line Claude reads for each kind of result the data server gives. */
class ScreenCharactersTest {
    @Test
    fun `matches, close calls, and unknown figures read in screen order`() {
        val figures =
            listOf(
                ScreenFigure("match", listOf("Rossweisse")),
                ScreenFigure("close", listOf("Maribell", "Veronica")),
                ScreenFigure("unknown"),
            )
        assertEquals(
            "Characters on screen, top to bottom, left to right: " +
                "Rossweisse, Maribell or Veronica, unidentified",
            ScreenCharacters.describe(figures),
        )
    }

    @Test
    fun `a screen without characters adds no line`() {
        assertNull(ScreenCharacters.describe(emptyList()))
    }
}
