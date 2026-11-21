package app.dailey.czn.companion.overlay

/**
 * A reply split for display: Claude opens each reply with `<seen>...</seen>` describing the
 * screen, then gives advice. [seen] is null until its closing tag arrives, so a half-streamed
 * block never shows as text. A reply without the block is all advice.
 *
 * Questions Claude asked split the advice: [advice] has one more segment than there are asks,
 * and ask `i` shows between segments `i` and `i + 1`. An ask made before the seen block closed
 * shows before all advice.
 */
data class ReplyParts(
    val seen: String?,
    val advice: List<String>,
) {
    companion object {
        private const val OPEN = "<seen>"
        private const val CLOSE = "</seen>"

        /**
         * [complete] is false while the reply streams; an unclosed block then stays hidden.
         * [askOffsets] are the reply lengths when each question was asked, in order.
         */
        fun parse(
            text: String,
            complete: Boolean,
            askOffsets: List<Int> = emptyList(),
        ): ReplyParts {
            val lead = text.length - text.trimStart().length
            val body = text.substring(lead)
            val (seen, adviceStart) =
                when {
                    !body.startsWith(OPEN) -> {
                        val maybeOpening = !complete && body.isNotEmpty() && OPEN.startsWith(body)
                        null to if (maybeOpening) text.length else 0
                    }

                    body.indexOf(CLOSE) < 0 -> {
                        null to if (complete) lead + OPEN.length else text.length
                    }

                    else -> {
                        val end = body.indexOf(CLOSE)
                        body.substring(OPEN.length, end).trim() to lead + end + CLOSE.length
                    }
                }
            val bounds = askOffsets.map { it.coerceIn(adviceStart, text.length) }
            val starts = listOf(adviceStart) + bounds
            val ends = bounds + text.length
            val advice =
                starts.zip(
                    ends,
                ) { start, end -> text.substring(start, maxOf(start, end)).trim() }
            return ReplyParts(seen, advice)
        }
    }
}
