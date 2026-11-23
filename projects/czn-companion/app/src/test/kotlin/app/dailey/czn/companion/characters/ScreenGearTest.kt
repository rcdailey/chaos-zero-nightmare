package app.dailey.czn.companion.characters

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

/** The line Claude reads for each kind of gear slot the data server names. */
class ScreenGearTest {
    @Test
    fun `slots read by row with each item's details, shared icons and close calls apart`() {
        val hammer =
            ScreenItem("Runic Hammer", "Legendary", "Weapon", "+20% Crit", mapOf("ATK" to "28/82"))
        val crown = ScreenItem("Crown of Corruption", "Legendary", "Armor", "Corrupt\nfoes")
        val fairy = ScreenItem("Fairy King's Crown", "Unique", "Armor", "Fairy")
        val hunter = ScreenItem("Big Game Hunter", "Rare", "Weapon", "+100%", mapOf("ATK" to "25"))
        val harmonized = ScreenItem("Harmonization: Big Game Hunter", "Rare", "Weapon", "+150%")
        val slots =
            listOf(
                ScreenSlot(1, "match", listOf(hammer)),
                ScreenSlot(1, "close", listOf(crown, fairy)),
                ScreenSlot(2, "match", listOf(hunter, harmonized)),
            )
        assertEquals(
            "Gear in slots, by row from the top, left to right:\n" +
                "row 1: Runic Hammer (Legendary Weapon, ATK 28/82): +20% Crit\n" +
                "row 1: Crown of Corruption (Legendary Armor): Corrupt foes or " +
                "Fairy King's Crown (Unique Armor): Fairy\n" +
                "row 2: Big Game Hunter (Rare Weapon, ATK 25): +100% / " +
                "Harmonization: Big Game Hunter (Rare Weapon): +150%",
            ScreenGear.describe(slots),
        )
    }

    @Test
    fun `a screen without known gear adds no line`() {
        assertNull(ScreenGear.describe(emptyList()))
    }
}
