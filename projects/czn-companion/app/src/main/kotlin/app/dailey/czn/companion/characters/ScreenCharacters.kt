package app.dailey.czn.companion.characters

import kotlinx.serialization.Serializable

/** One character figure the data server found on a screenshot ([ScreenIdentifier]). */
@Serializable
data class ScreenFigure(
    /** `match` (one name), `close` (two names, too close to choose), or `unknown` (none). */
    val result: String,
    val names: List<String> = emptyList(),
)

/**
 * The characters on a screenshot as text for Claude, which cannot tell characters apart
 * reliably from portraits.
 */
object ScreenCharacters {
    /**
     * The line Claude reads, such as "Characters on screen, top to bottom, left to right:
     * Rossweisse, Maribell or Veronica, unidentified"; null when the server found none.
     */
    fun describe(figures: List<ScreenFigure>): String? {
        if (figures.isEmpty()) return null
        val names =
            figures.joinToString(", ") { figure ->
                figure.names.joinToString(" or ").ifEmpty { "unidentified" }
            }
        return "Characters on screen, top to bottom, left to right: $names"
    }
}
