package app.dailey.czn.companion.claude

import app.dailey.czn.companion.run.EntryKind
import app.dailey.czn.companion.run.RunEntry
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import kotlin.io.encoding.Base64

/**
 * Builds the Messages API request body. Wire rules that subscription tokens and caching depend on:
 * - The first system block is exactly [IDENTITY]; any other first block gets a bare 429.
 * - Only the newest user message carries an image, so everything before it stays byte-identical
 *   across turns and the cache breakpoint on the last history message can hit.
 */
object MessagesRequest {
    const val IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
    const val MODEL = "claude-sonnet-5-5"
    const val CZN_SERVER = "czn"

    /** Sent for a screen read without typed text; the API rejects empty text blocks. */
    const val SCREEN_ONLY = "Advise me on this screen."
    val BETAS = listOf("oauth-2025-04-20", "claude-code-20250219", "mcp-client-2025-11-20")

    fun build(
        history: List<RunEntry>,
        text: String,
        jpeg: ByteArray?,
        cznUrl: String,
        cznToken: String,
    ): JsonObject =
        buildJsonObject {
            put("model", MODEL)
            put("max_tokens", 8192)
            put("stream", true)
            putJsonObject("output_config") { put("effort", "medium") }
            putJsonArray("system") {
                addJsonObject { text(IDENTITY) }
                addJsonObject {
                    text(APP_PROMPT)
                    cacheControl()
                }
            }
            putJsonArray("mcp_servers") {
                addJsonObject {
                    put("type", "url")
                    put("url", cznUrl)
                    put("name", CZN_SERVER)
                    put("authorization_token", cznToken)
                }
            }
            putJsonArray("tools") {
                addJsonObject {
                    put("type", "mcp_toolset")
                    put("mcp_server_name", CZN_SERVER)
                }
            }
            putJsonArray("messages") {
                history.forEachIndexed { index, entry ->
                    addJsonObject {
                        put("role", if (entry.kind == EntryKind.User) "user" else "assistant")
                        putJsonArray("content") {
                            addJsonObject {
                                text(entry.text.ifBlank { SCREEN_ONLY })
                                if (index == history.lastIndex) cacheControl()
                            }
                        }
                    }
                }
                addJsonObject {
                    put("role", "user")
                    putJsonArray("content") {
                        if (jpeg != null) {
                            addJsonObject {
                                put("type", "image")
                                putJsonObject("source") {
                                    put("type", "base64")
                                    put("media_type", "image/jpeg")
                                    put("data", Base64.encode(jpeg))
                                }
                            }
                        }
                        addJsonObject { text(text.ifBlank { SCREEN_ONLY }) }
                    }
                }
            }
        }

    private fun JsonObjectBuilder.text(value: String) {
        put("type", "text")
        put("text", value)
    }

    private fun JsonObjectBuilder.cacheControl() {
        putJsonObject("cache_control") { put("type", "ephemeral") }
    }

    private val APP_PROMPT =
        """
        You advise a player of the gacha deckbuilder Chaos Zero Nightmare (CZN) through a small
        panel over the game on an Android tablet. Each new request includes a screenshot of the
        game as the player sees it now.

        The key words MUST, MUST NOT, SHOULD, and MAY are to be read as in RFC 2119.

        - You MUST read the screenshot carefully and confirm every card, character, equipment,
          and partner name with the czn tools before relying on it; card text read from the
          screen goes to match_card_text. Make independent lookups in parallel. The screen
          shows the player's actual values, which can differ from the database's base values;
          prefer the screen.
        - Every reply MUST start with a <seen>...</seen> block: a few short lines on what is on
          screen (names, numbers, choices), so later questions in this run can refer back to
          it. The panel shows it collapsed; nothing may come before it.
        - After the block, the advice MUST be one sentence with the recommended action, then at
          most two short reasons, about 50 words in total. The player reads it mid-game on a
          narrow panel. Go longer only when the player asks for detail.
        - When the player sends no question and the goal is unclear, you MUST state your best
          guess of what they want in a few words and advise on that; do not stop to ask.
        - When the advice depends on something not on this screen (deck, equipment, a card's
          details, map nodes), you MUST name what to tap to show it and end with "Then
          long-press." You cannot tap for the player; the next request carries that screen.
          Give any advice the current screen supports first.
        - The advice MUST NOT narrate lookups or thinking ("Let me check..."), repeat facts from
          the seen block, or comment on anything that needs no action from the player.
        - The panel renders Markdown. You SHOULD bold card and choice names. You MUST NOT use
          headings or tables.
        - Earlier messages in this conversation are this run's history. You SHOULD use them.
        """.trimIndent()
}
