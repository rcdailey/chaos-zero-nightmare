package app.dailey.czn.companion.overlay

/**
 * A reply split for display: Claude opens each reply with `<seen>...</seen>` describing the
 * screen, then gives advice. [seen] is null until its closing tag arrives, so a half-streamed
 * block never shows as text. A reply without the block is all advice.
 */
data class ReplyParts(
    val seen: String?,
    val advice: String,
) {
    companion object {
        private const val OPEN = "<seen>"
        private const val CLOSE = "</seen>"

        /** [complete] is false while the reply streams; an unclosed block then stays hidden. */
        fun parse(
            text: String,
            complete: Boolean,
        ): ReplyParts {
            val body = text.trimStart()
            if (!body.startsWith(OPEN)) {
                val maybeOpening = !complete && body.isNotEmpty() && OPEN.startsWith(body)
                return ReplyParts(null, if (maybeOpening) "" else text)
            }
            val end = body.indexOf(CLOSE)
            if (end < 0) {
                return ReplyParts(null, if (complete) body.removePrefix(OPEN).trim() else "")
            }
            val seen = body.substring(OPEN.length, end).trim()
            return ReplyParts(seen, body.substring(end + CLOSE.length).trim())
        }
    }
}
