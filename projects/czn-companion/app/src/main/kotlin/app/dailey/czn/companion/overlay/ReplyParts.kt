package app.dailey.czn.companion.overlay

/**
 * A reply split for display. Questions Claude asked split the advice: [advice] has one more
 * segment than there are asks, and ask `i` shows between segments `i` and `i + 1`.
 */
data class ReplyParts(
    val advice: List<String>,
) {
    companion object {
        /** [askOffsets] are the reply lengths when each question was asked, in order. */
        fun parse(
            text: String,
            askOffsets: List<Int> = emptyList(),
        ): ReplyParts {
            val bounds = askOffsets.map { it.coerceIn(0, text.length) }
            val starts = listOf(0) + bounds
            val ends = bounds + text.length
            val advice =
                starts.zip(ends) { start, end -> text.substring(start, maxOf(start, end)).trim() }
            return ReplyParts(advice)
        }
    }
}
