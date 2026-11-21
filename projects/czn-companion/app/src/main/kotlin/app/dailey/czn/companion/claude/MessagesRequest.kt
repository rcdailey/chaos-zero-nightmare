package app.dailey.czn.companion.claude

import app.dailey.czn.companion.ask.AnsweredAsk
import app.dailey.czn.companion.ask.PlayerAsks
import app.dailey.czn.companion.run.EntryKind
import app.dailey.czn.companion.run.RunEntry
import com.anthropic.core.JsonValue
import com.anthropic.models.beta.messages.BetaBase64ImageSource
import com.anthropic.models.beta.messages.BetaCacheControlEphemeral
import com.anthropic.models.beta.messages.BetaContentBlockParam
import com.anthropic.models.beta.messages.BetaMcpToolset
import com.anthropic.models.beta.messages.BetaMessageParam
import com.anthropic.models.beta.messages.BetaOutputConfig
import com.anthropic.models.beta.messages.BetaRequestMcpServerUrlDefinition
import com.anthropic.models.beta.messages.BetaTextBlockParam
import com.anthropic.models.beta.messages.BetaTool
import com.anthropic.models.beta.messages.MessageCreateParams
import kotlin.io.encoding.Base64

/**
 * Builds Messages API requests. Wire rules that subscription tokens and caching depend on:
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
    const val ASK_PLAYER = "ask_player"

    /**
     * @param turn messages Claude and the app exchanged after [text] in this turn: Claude's
     *   responses that stopped for a tool, each followed by the app's tool results if any.
     * @param character the run's character, or null while unknown.
     * @param onScreen the characters the data server named on the screenshot
     *   ([app.dailey.czn.companion.characters.ScreenCharacters.describe]), or null without them.
     *   Saved as the entry's [RunEntry.screen], which history sends after the saved text.
     */
    fun build(
        history: List<RunEntry>,
        text: String,
        jpeg: ByteArray?,
        cznUrl: String,
        cznToken: String,
        character: String? = null,
        turn: List<BetaMessageParam> = emptyList(),
        onScreen: String? = null,
    ): MessageCreateParams =
        MessageCreateParams
            .builder()
            .model(MODEL)
            .maxTokens(8192)
            .outputConfig(BetaOutputConfig.builder().effort(BetaOutputConfig.Effort.MEDIUM).build())
            .apply { BETAS.forEach(::addBeta) }
            .systemOfBetaTextBlockParams(
                listOf(
                    textBlock(IDENTITY),
                    textBlock(APP_PROMPT, cached = true),
                    textBlock("The run's character: ${character ?: "not known yet"}."),
                ),
            ).addMcpServer(
                BetaRequestMcpServerUrlDefinition
                    .builder()
                    .name(CZN_SERVER)
                    .url(cznUrl)
                    .authorizationToken(cznToken)
                    .build(),
            ).addTool(BetaMcpToolset.builder().mcpServerName(CZN_SERVER).build())
            .addTool(SET_RUN_CHARACTER_TOOL)
            .addTool(ASK_RUN_CHARACTER_TOOL)
            .addTool(ASK_PLAYER_TOOL)
            .apply {
                history.forEachIndexed { index, entry ->
                    val texts =
                        listOf(withAsks(entry.text, entry.asks).ifBlank { SCREEN_ONLY }) +
                            listOf(entry.screen).filter(String::isNotBlank)
                    // The cache breakpoint goes on the last block of the last message.
                    val blocks =
                        texts.mapIndexed { i, text ->
                            BetaContentBlockParam.ofText(
                                textBlock(text, index == history.lastIndex && i == texts.lastIndex),
                            )
                        }
                    if (entry.kind == EntryKind.User) {
                        addUserMessageOfContentBlockParams(blocks)
                    } else {
                        addAssistantMessageOfContentBlockParams(blocks)
                    }
                }
                addUserMessageOfContentBlockParams(
                    listOfNotNull(
                        jpeg?.let(::imageBlock),
                        BetaContentBlockParam.ofText(text.ifBlank { SCREEN_ONLY }),
                        onScreen?.let(BetaContentBlockParam::ofText),
                    ),
                )
                turn.forEach(::addMessage)
            }.build()

    /**
     * A saved reply as Claude reads it later: each ask it made, with the answers, as an
     * `<asked>` block where it was asked. Only saved data goes in, so the text stays identical
     * across turns and keeps the cache.
     */
    private fun withAsks(
        text: String,
        asks: List<AnsweredAsk>,
    ): String {
        if (asks.isEmpty()) return text
        val parts = mutableListOf<String>()
        var start = 0
        asks.sortedBy { it.offset }.forEach { ask ->
            val at = ask.offset.coerceIn(start, text.length)
            parts += text.substring(start, at).trim()
            parts += "<asked>\n${PlayerAsks.describe(ask.questions, ask.answers)}\n</asked>"
            start = at
        }
        parts += text.substring(start).trim()
        return parts.filter(String::isNotEmpty).joinToString("\n\n")
    }

    private fun textBlock(
        text: String,
        cached: Boolean = false,
    ) = BetaTextBlockParam
        .builder()
        .text(text)
        .apply { if (cached) cacheControl(BetaCacheControlEphemeral.builder().build()) }
        .build()

    private fun imageBlock(jpeg: ByteArray) =
        BetaContentBlockParam.ofImage(
            BetaBase64ImageSource
                .builder()
                .mediaType(BetaBase64ImageSource.MediaType.IMAGE_JPEG)
                .data(Base64.encode(jpeg))
                .build(),
        )

    private fun tool(
        name: String,
        description: String,
        properties: Map<String, Any>,
        required: List<String>,
    ) = BetaTool
        .builder()
        .name(name)
        .description(description.trimIndent().replace('\n', ' '))
        .inputSchema(
            BetaTool.InputSchema
                .builder()
                .properties(JsonValue.from(properties))
                .required(required)
                .build(),
        ).build()

    private val SET_RUN_CHARACTER_TOOL =
        tool(
            SET_RUN_CHARACTER,
            """
            Saves the character whose deck this run builds, when the screen shows it beyond doubt.
            The player sees it and can change it. Use the name exactly as the czn tools spell it.
            """,
            mapOf(
                "name" to
                    mapOf(
                        "type" to "string",
                        "description" to "Character name as the czn tools spell it",
                    ),
            ),
            listOf("name"),
        )

    private val ASK_RUN_CHARACTER_TOOL =
        tool(
            ASK_RUN_CHARACTER,
            """
            Shows the player character cards and waits until they pick the character whose deck
            this run builds; the pick is saved. Returns the name, or that the player skipped the
            choice.
            """,
            mapOf(
                "candidates" to
                    mapOf(
                        "type" to "array",
                        "items" to mapOf("type" to "string"),
                        "description" to
                            "Names to offer, as the czn tools spell them, for example the team " +
                            "of three seen earlier; omit to offer every character",
                    ),
            ),
            emptyList(),
        )

    // Declared before ASK_PLAYER_TOOL, which reads it while the object initializes.
    private val OPTION_SCHEMA =
        mapOf(
            "type" to "object",
            "properties" to
                mapOf(
                    "label" to
                        mapOf(
                            "type" to "string",
                            "description" to "A few words; what is sent back",
                        ),
                    "description" to
                        mapOf("type" to "string", "description" to "One short line on its effect"),
                    "recommended" to
                        mapOf("type" to "boolean", "description" to "The option you advise"),
                ),
            "required" to listOf("label"),
        )

    private val ASK_PLAYER_TOOL =
        tool(
            ASK_PLAYER,
            """
            Shows the player questions with options in the conversation and waits for the
            answers. The player picks one option, or several when multi_select is true, and can
            always type their own answer instead. Several questions are answered one after
            another and sent together. Returns each question with its answer, or that the player
            skipped them.
            """,
            mapOf(
                "questions" to
                    mapOf(
                        "type" to "array",
                        "minItems" to 1,
                        "maxItems" to PlayerAsks.MAX_QUESTIONS,
                        "items" to
                            mapOf(
                                "type" to "object",
                                "properties" to
                                    mapOf(
                                        "question" to
                                            mapOf(
                                                "type" to "string",
                                                "description" to
                                                    "The complete question, ending with ?",
                                            ),
                                        "header" to
                                            mapOf(
                                                "type" to "string",
                                                "description" to
                                                    "Label of at most 3 words, for example Path",
                                            ),
                                        "options" to
                                            mapOf(
                                                "type" to "array",
                                                "minItems" to PlayerAsks.MIN_OPTIONS,
                                                "maxItems" to PlayerAsks.MAX_OPTIONS,
                                                "items" to OPTION_SCHEMA,
                                            ),
                                        "multi_select" to
                                            mapOf(
                                                "type" to "boolean",
                                                "description" to
                                                    "Allow picking several options",
                                            ),
                                    ),
                                "required" to listOf("question", "header", "options"),
                            ),
                    ),
            ),
            listOf("questions"),
        )

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
          prefer the screen. czn tool results leave out empty fields, so a missing field means
          the data has no value for it.
        - Screens show characters as portraits without names. The app names them for you: a
          line "Characters on screen, top to bottom, left to right: ..." after the player's
          message comes from comparing each figure with every character's card art, and is
          authoritative. "A or B" means one of the two; do not pick one unless the screen
          shows a name. "unidentified" stays unidentified. Without that line, call the
          characters unidentified. You MUST NOT name a character from looks, element icons,
          or class icons.
        - You MUST state only player facts the screen or this run's history shows. You MUST
          NOT invent progress, clears, ownership, or past purchases ("the highest level
          you've cleared"). When comparing two screens, attribute a change only to what both
          screens show; when the cause is unclear, say so.
        - For game systems (season mechanics, Environment Variables, shops, restrictions),
          you MUST check search_posts before advising, and advise from what the posts say.
          When the screen and posts leave a detail open, such as which node is which, name
          what to tap instead of guessing.
        - The player asks for help with anything in the game: a Chaos run's deck, an event or
          minigame, a character's build (partner, equipment, potentials), where to spend
          points, a team. Advise on the player's goal only, once you know it. When neither the
          player's message nor this run's history states it, you MUST call ask_player before
          advising, offering 2 to 4 goals this screen suggests, the likeliest recommended. On
          a character screen, for example: optimize this character, pick a partner, set up
          equipment. You need not guess right: the player steers over the next turns, so make
          each step easy. Once known, the goal carries over; when a later screen fits it
          poorly, ask again with goals that fit, the known one first.
        - Team advice MUST start from who is already on the team: say whether the current
          team matches a recommended one, and suggest only changes. Give it only when the goal
          is the team.
        - Every reply MUST start with a <seen>...</seen> block: a few short lines on what is on
          screen (names, numbers, choices), so later questions in this run can refer back to
          it. The panel shows it collapsed; nothing may come before it.
        - After the block, the advice MUST be one sentence with the recommended action, then at
          most two short reasons, about 50 words in total. The player reads it mid-game on a
          narrow panel. Go longer only when the player asks for detail.
        - When the advice depends on something not on this screen (deck, equipment, a card's
          details, map nodes), you MUST name what to tap to show it and end with "Then
          long-press." You cannot tap for the player; the next request carries that screen.
          Give any advice the current screen supports first.
        - The advice MUST NOT narrate lookups or thinking ("Let me check..."), repeat facts from
          the seen block, or comment on anything that needs no action from the player.
        - The panel renders Markdown. You SHOULD bold card and choice names. You MUST NOT use
          headings or tables.
        - Earlier messages in this conversation are this run's history. You SHOULD use them.
        - A Chaos run builds one character's deck. The last system block names that character
          or says it is not known yet. When the goal is a Chaos run and the character is not
          known, you MUST settle it before advising on the run: call set_run_character when
          the screen shows it beyond doubt, otherwise ask_run_character, offering only the
          plausible characters (for example the team of three on screen). You MUST NOT guess
          the character, and MUST NOT ask again once it is known unless the screen
          contradicts it.
        - When the best action depends on something else only the player knows (what they
          own, a preference) and neither the screen nor the history settles it, you MAY call
          ask_player, batching every open question into one call. You SHOULD advise instead
          when a reasonable guess exists, and MUST NOT ask what a screenshot could show; name
          what to tap instead.
        - The app shows earlier questions you asked, with the player's answers, as <asked>
          blocks in your past replies. You MUST NOT write <asked> blocks yourself.
        """.trimIndent()
}
