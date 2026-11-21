package app.dailey.czn.companion.claude

import app.dailey.czn.companion.run.EntryKind
import app.dailey.czn.companion.run.RunEntry
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import kotlin.io.encoding.Base64

/**
 * Builds the Messages API request body. Wire rules that subscription tokens and caching depend on:
 * - The first system block is exactly [IDENTITY]; any other first block gets a bare 429.
 * - Only the newest user message carries an image, so everything before it stays byte-identical
 *   across turns and the cache breakpoint on the last history message can hit.
 * - The run's character goes in a system block after the cached app prompt, so setting it costs
 *   one history cache miss and nothing more.
 */
object MessagesRequest {
    const val IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
    const val MODEL = "claude-sonnet-5-5"
    const val CZN_SERVER = "czn"

    /** Sent for a screen read without typed text; the API rejects empty text blocks. */
    const val SCREEN_ONLY = "Advise me on this screen."
    val BETAS = listOf("oauth-2025-04-20", "claude-code-20250219", "mcp-client-2025-11-20")

    /** Tools the app runs itself, in [ClaudeConversation]. */
    const val SET_RUN_CHARACTER = "set_run_character"
    const val ASK_RUN_CHARACTER = "ask_run_character"

    /**
     * @param turn messages Claude and the app exchanged after [text] in this turn: assistant
     *   content that stopped for an app tool, then the tool results.
     * @param character the run's character, or null while unknown.
     */
    fun build(
        history: List<RunEntry>,
        text: String,
        jpeg: ByteArray?,
        cznUrl: String,
        cznToken: String,
        character: String? = null,
        turn: List<JsonObject> = emptyList(),
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
                addJsonObject {
                    text("The run's character: ${character ?: "not known yet"}.")
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
                add(SET_RUN_CHARACTER_TOOL)
                add(ASK_RUN_CHARACTER_TOOL)
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
                turn.forEachIndexed { index, message ->
                    add(if (index == turn.lastIndex) message.withCacheBreakpoint() else message)
                }
            }
        }

    /** [message] with a cache breakpoint on its last content block. */
    private fun JsonObject.withCacheBreakpoint(): JsonObject {
        val content = this["content"]?.jsonArray ?: return this
        val last = content.lastOrNull()?.jsonObject ?: return this
        val marked = JsonObject(last + ("cache_control" to EPHEMERAL))
        return JsonObject(this + ("content" to JsonArray(content.dropLast(1) + marked)))
    }

    private val EPHEMERAL = buildJsonObject { put("type", "ephemeral") }

    private fun tool(
        name: String,
        description: String,
        properties: JsonObjectBuilder.() -> Unit,
        required: List<String>,
    ) = buildJsonObject {
        put("name", name)
        put("description", description.trimIndent().replace('\n', ' '))
        putJsonObject("input_schema") {
            put("type", "object")
            putJsonObject("properties", properties)
            putJsonArray("required") { required.forEach(::add) }
        }
    }

    private val SET_RUN_CHARACTER_TOOL =
        tool(
            SET_RUN_CHARACTER,
            """
            Saves the character whose deck this run builds, when the screen shows it beyond doubt.
            The player sees it and can change it. Use the name exactly as the czn tools spell it.
            """,
            {
                putJsonObject("name") {
                    put("type", "string")
                    put("description", "Character name as the czn tools spell it")
                }
            },
            listOf("name"),
        )

    private val ASK_RUN_CHARACTER_TOOL =
        tool(
            ASK_RUN_CHARACTER,
            """
            Shows the player a full-screen grid of character cards and waits until they pick the
            character whose deck this run builds; the pick is saved. Returns the name, or that
            the player closed the grid without choosing.
            """,
            {
                putJsonObject("candidates") {
                    put("type", "array")
                    putJsonObject("items") { put("type", "string") }
                    put(
                        "description",
                        "Names to offer, as the czn tools spell them, for example the team of " +
                            "three seen earlier; omit to offer every character",
                    )
                }
                putJsonObject("reason") {
                    put("type", "string")
                    put("description", "Short title above the grid, for example why you ask")
                }
            },
            emptyList(),
        )

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
        - A run builds one character's deck. The last system block names that character. When
          it is not known and the screen shows it beyond doubt, you MUST call
          set_run_character. When advice depends on it and the screen leaves it open, you MUST
          call ask_run_character, offering only the plausible characters (for example the team
          of three seen earlier). You MUST NOT ask again once it is known unless the screen
          contradicts it.
        """.trimIndent()
}
