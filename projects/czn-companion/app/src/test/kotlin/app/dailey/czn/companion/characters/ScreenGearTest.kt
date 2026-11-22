package app.dailey.czn.companion.characters

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

/** The line Claude reads for each kind of gear slot the data server names. */
class ScreenGearTest {
    @Test
    fun `slots read by row, shared icons and close calls each in their own words`() {
        val slots =
            listOf(
                ScreenSlot(1, "match", listOf("Runic Hammer")),
                ScreenSlot(1, "close", listOf("Crown of Corruption", "Fairy King's Crown")),
                ScreenSlot(2, "match", listOf("Big Game Hunter", "Harmonization: Big Game Hunter")),
            )
        assertEquals(
            "Gear in slots, by row from the top, left to right: " +
                "row 1: Runic Hammer; Crown of Corruption or Fairy King's Crown. " +
                "row 2: Big Game Hunter / Harmonization: Big Game Hunter",
            ScreenGear.describe(slots),
        )
    }

    @Test
    fun `a screen without known gear adds no line`() {
        assertNull(ScreenGear.describe(emptyList()))
    }
}
