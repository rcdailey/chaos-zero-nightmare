package app.dailey.czn.companion.claude

import app.dailey.czn.companion.ask.AnsweredAsk
import app.dailey.czn.companion.ask.PlayerAsks
import app.dailey.czn.companion.run.EntryKind
import app.dailey.czn.companion.run.RunEntry
import com.anthropic.core.JsonValue
import com.anthropic.models.beta.messages.BetaBase64ImageSource
import com.anthropic.models.beta.messages.BetaCacheControlEphemeral
import com.anthropic.models.beta.messages.BetaClearToolUses20250919Edit
import com.anthropic.models.beta.messages.BetaContentBlockParam
import com.anthropic.models.beta.messages.BetaContextManagementConfig
import com.anthropic.models.beta.messages.BetaDiagnosticsParam
import com.anthropic.models.beta.messages.BetaInputTokensClearAtLeast
import com.anthropic.models.beta.messages.BetaInputTokensTrigger
import com.anthropic.models.beta.messages.BetaMcpToolset
import com.anthropic.models.beta.messages.BetaMessageParam
import com.anthropic.models.beta.messages.BetaOutputConfig
import com.anthropic.models.beta.messages.BetaRequestMcpServerUrlDefinition
import com.anthropic.models.beta.messages.BetaTextBlockParam
import com.anthropic.models.beta.messages.BetaThinkingConfigAdaptive
import com.anthropic.models.beta.messages.BetaTool
import com.anthropic.models.beta.messages.BetaToolUsesKeep
import com.anthropic.models.beta.messages.MessageCreateParams
import kotlin.io.encoding.Base64

/** What the app knows about the active run, sent at the end of every request's newest message. */
data class RunState(
    /** The run's character, or null while unknown. */
    val character: String? = null,
    /** The target deck Claude saved with set_run_plan, or null before it saves one. */
    val plan: String? = null,
)

/**
 * Builds Messages API requests. Wire rules that subscription tokens and caching depend on:
 * - The first system block is exactly [IDENTITY]; any other first block gets a bare 429.
 * - Everything before the newest user message is byte-identical across a run's requests, so
 *   each request reads the previous one's cache: tools and system never change, earlier turns
 *   replay their saved exchange the same way every time, and what changes per request (the
 *   screenshot, [RunState]) sits in the newest user message, after the history breakpoints.
 * - Breakpoints (4 at most): the app prompt; the end of the turn before last, which the previous
 *   turn's request wrote, so a turn of more than 20 blocks still finds it; the end of the last
 *   turn; and the end of the request, which this turn's next request reads.
 * - Earlier turns replay without thinking blocks. Their requests had a screenshot that history
 *   leaves out, and replayed thinking must follow exactly the prompt it was made for; the API
 *   allows dropping thinking from every earlier turn.
 * - Cache entries use the default 5-minute lifetime. In the run of 2026-11-21, 1 gap between
 *   turns in 75 was longer than 5 minutes; Anthropic's guidance favors the 1-hour lifetime
 *   from about 1 gap in 30. Check `request_usage` for cache writes after long gaps if play
 *   patterns change.
 */
object MessagesRequest {
    const val IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
    const val MODEL = "claude-sonnet-5-5"
    const val CZN_SERVER = "czn"

    /** Sent for a screen read without typed text; the API rejects empty text blocks. */
    const val SCREEN_ONLY = "Advise me on this screen."
    val BETAS =
        listOf(
            "oauth-2025-04-20",
            "claude-code-20250219",
            "mcp-client-2025-11-20",
            "context-management-2025-06-27",
        )

    /** Tools the app runs itself, in [ClaudeConversation]. */
    const val SET_RUN_CHARACTER = "set_run_character"
    const val SET_RUN_PLAN = "set_run_plan"
    const val ASK_RUN_CHARACTER = "ask_run_character"
    const val ASK_PLAYER = "ask_player"
    const val IDENTIFY_CHARACTERS = "identify_characters"

    /**
     * Old tool results the API clears from the prompt it reads, while the app keeps sending
     * them: once a request passes [CLEAR_TRIGGER_TOKENS], results older than the newest
     * [KEEP_TOOL_USES] become placeholders, and Claude calls a tool again when it needs one.
     * Each clearing rewrites the cache from the first cleared result, so it waits until it
     * frees at least [CLEAR_AT_LEAST_TOKENS]. Measured on the 76-turn run of 2026-11-21: its
     * full history is about 222,000 tokens, and clearing 115 old results brings each request
     * to about 111,000, read from the cache. A run passes the trigger after roughly 40 turns.
     * The plan tools are never cleared. `request_usage` records each clearing.
     */
    private const val CLEAR_TRIGGER_TOKENS = 120_000L
    private const val KEEP_TOOL_USES = 30L
    private const val CLEAR_AT_LEAST_TOKENS = 50_000L

    /**
     * @param history the run's saved messages; an assistant entry with an exchange replays it,
     *   so Claude sees its earlier tool calls and their results.
     * @param turn messages Claude and the app exchanged after [text] in this turn: Claude's
     *   responses that stopped for a tool, each followed by the app's tool results if any.
     * @param previousMessageId the run's newest response id, for the API's cache diagnostics;
     *   null for a run's first request.
     */
    fun build(
        history: List<RunEntry>,
        text: String,
        jpeg: ByteArray?,
        cznUrl: String,
        cznToken: String,
        run: RunState = RunState(),
        turn: List<BetaMessageParam> = emptyList(),
        previousMessageId: String? = null,
    ): MessageCreateParams {
        val turns = history.chunked(2).map(::replay).filter(List<BetaMessageParam>::isNotEmpty)
        val newest =
            BetaMessageParam.ofUserOfContentBlockParams(
                listOfNotNull(
                    jpeg?.let(::imageBlock),
                    BetaContentBlockParam.ofText(textBlock(text.ifBlank { SCREEN_ONLY })),
                    BetaContentBlockParam.ofText(textBlock(describe(run))),
                ),
            )
        val messages =
            buildList {
                turns.forEachIndexed { index, messages ->
                    val marked = index >= turns.size - 2
                    addAll(if (marked) messages.dropLast(1) + cached(messages.last()) else messages)
                }
                add(newest)
                addAll(turn.mapNotNull(::withoutTrailingThinking))
            }
        return MessageCreateParams
            .builder()
            .model(MODEL)
            .maxTokens(8192)
            .outputConfig(BetaOutputConfig.builder().effort(BetaOutputConfig.Effort.HIGH).build())
            // Thinking is on by default but returned empty; a summary lands in the saved exchange.
            .thinking(
                BetaThinkingConfigAdaptive
                    .builder()
                    .display(BetaThinkingConfigAdaptive.Display.SUMMARIZED)
                    .build(),
            ).apply { BETAS.forEach(::addBeta) }
            .systemOfBetaTextBlockParams(
                listOf(textBlock(IDENTITY), textBlock(APP_PROMPT, cached = true)),
            ).addMcpServer(
                BetaRequestMcpServerUrlDefinition
                    .builder()
                    .name(CZN_SERVER)
                    .url(cznUrl)
                    .authorizationToken(cznToken)
                    .build(),
            ).addTool(BetaMcpToolset.builder().mcpServerName(CZN_SERVER).build())
            .addTool(SET_RUN_CHARACTER_TOOL)
            .addTool(SET_RUN_PLAN_TOOL)
            .addTool(ASK_RUN_CHARACTER_TOOL)
            .addTool(ASK_PLAYER_TOOL)
            .addTool(IDENTIFY_CHARACTERS_TOOL)
            .contextManagement(CLEAR_OLD_TOOL_RESULTS)
            .diagnostics(
                BetaDiagnosticsParam.builder().previousMessageId(previousMessageId).build(),
            ).messages(messages.dropLast(1) + cached(messages.last()))
            .build()
    }

    private val CLEAR_OLD_TOOL_RESULTS =
        BetaContextManagementConfig
            .builder()
            .addEdit(
                BetaClearToolUses20250919Edit
                    .builder()
                    .trigger(BetaInputTokensTrigger.builder().value(CLEAR_TRIGGER_TOKENS).build())
                    .keep(BetaToolUsesKeep.builder().value(KEEP_TOOL_USES).build())
                    .clearAtLeast(
                        BetaInputTokensClearAtLeast.builder().value(CLEAR_AT_LEAST_TOKENS).build(),
                    ).excludeTools(listOf(SET_RUN_PLAN, "plan_deck"))
                    .build(),
            ).build()

    /** The run state block at the end of the newest message. */
    private fun describe(run: RunState) =
        "Run state. Character: ${run.character ?: "not known yet"}. Saved plan: " +
            (run.plan?.let { "\n$it" } ?: "none yet.")

    /**
     * One saved turn (the player's message, then the reply) as it is sent again. A reply with a
     * saved exchange replays it without thinking; one saved before exchanges were kept is its
     * text, with the names line and the questions Claude asked written in.
     */
    private fun replay(entries: List<RunEntry>): List<BetaMessageParam> {
        val (user, reply) =
            entries.firstOrNull { it.kind == EntryKind.User } to
                entries.firstOrNull { it.kind == EntryKind.Assistant }
        if (user == null || reply == null) return emptyList()
        val exchange = reply.exchange.ifBlank { null }?.let(::withoutThinking)
        val texts =
            listOf(user.text.ifBlank { SCREEN_ONLY }) +
                listOf(user.screen).filter { exchange == null && it.isNotBlank() }
        val question =
            BetaMessageParam.ofUserOfContentBlockParams(
                texts.map { BetaContentBlockParam.ofText(textBlock(it)) },
            )
        val answer =
            exchange?.takeIf(List<BetaMessageParam>::isNotEmpty)
                ?: listOf(
                    BetaMessageParam.ofAssistantOfContentBlockParams(
                        listOf(
                            BetaContentBlockParam.ofText(
                                textBlock(withAsks(reply.text, reply.asks)),
                            ),
                        ),
                    ),
                )
        return listOf(question) + answer
    }

    /**
     * A saved exchange without thinking blocks; a message left empty is dropped, and messages
     * of one role that end up next to each other are joined, as the API requires.
     */
    private fun withoutThinking(json: String): List<BetaMessageParam> {
        val messages = JSON.readValue(json, Array<BetaMessageParam>::class.java)
        val joined = mutableListOf<Pair<BetaMessageParam.Role, List<BetaContentBlockParam>>>()
        for (message in messages) {
            val blocks = blocks(message).filterNot { it.isThinking() || it.isRedactedThinking() }
            if (blocks.isEmpty()) continue
            val last = joined.lastOrNull()
            if (last?.first == message.role()) {
                joined[joined.lastIndex] = last.first to last.second + blocks
            } else {
                joined += message.role() to blocks
            }
        }
        return joined.map { (role, blocks) -> message(role, blocks) }
    }

    /**
     * [message] without the thinking blocks it ends with: a response can end on one (seen on
     * device, 2026-11-22), and the API rejects an assistant message whose last block is
     * thinking. Dropping the newest thinking is allowed; null when nothing is left.
     */
    private fun withoutTrailingThinking(message: BetaMessageParam): BetaMessageParam? {
        if (message.role() != BetaMessageParam.Role.ASSISTANT) return message
        val blocks = blocks(message).dropLastWhile { it.isThinking() || it.isRedactedThinking() }
        return when {
            blocks.isEmpty() -> null
            blocks.size == blocks(message).size -> message
            else -> message(message.role(), blocks)
        }
    }

    private fun message(
        role: BetaMessageParam.Role,
        blocks: List<BetaContentBlockParam>,
    ) = if (role == BetaMessageParam.Role.ASSISTANT) {
        BetaMessageParam.ofAssistantOfContentBlockParams(blocks)
    } else {
        BetaMessageParam.ofUserOfContentBlockParams(blocks)
    }

    private fun blocks(message: BetaMessageParam): List<BetaContentBlockParam> {
        val content = message.content().orElse(null) ?: return emptyList()
        return content.contentBlockParams().orElse(null)
            ?: listOf(BetaContentBlockParam.ofText(textBlock(content.string().orElse(""))))
    }

    /** [message] with a cache breakpoint on its last block that can carry one. */
    private fun cached(message: BetaMessageParam): BetaMessageParam {
        val blocks = blocks(message).toMutableList()
        val at = blocks.indexOfLast { withCacheControl(it) != null }
        if (at < 0) return message
        blocks[at] = withCacheControl(blocks[at])!!
        return message(message.role(), blocks)
    }

    private fun withCacheControl(block: BetaContentBlockParam): BetaContentBlockParam? {
        val ephemeral = BetaCacheControlEphemeral.builder().build()
        return block.text().orElse(null)?.let {
            BetaContentBlockParam.ofText(it.toBuilder().cacheControl(ephemeral).build())
        } ?: block.image().orElse(null)?.let {
            BetaContentBlockParam.ofImage(it.toBuilder().cacheControl(ephemeral).build())
        } ?: block.toolUse().orElse(null)?.let {
            BetaContentBlockParam.ofToolUse(it.toBuilder().cacheControl(ephemeral).build())
        } ?: block.toolResult().orElse(null)?.let {
            BetaContentBlockParam.ofToolResult(it.toBuilder().cacheControl(ephemeral).build())
        } ?: block.mcpToolUse().orElse(null)?.let {
            BetaContentBlockParam.ofMcpToolUse(it.toBuilder().cacheControl(ephemeral).build())
        } ?: block.mcpToolResult().orElse(null)?.let {
            BetaContentBlockParam.ofMcpToolResult(it.toBuilder().cacheControl(ephemeral).build())
        }
    }

    private val JSON = com.anthropic.core.jsonMapper()

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
            Saves the character whose deck this run builds, when the screen shows it beyond doubt;
            otherwise use ask_run_character. The player sees it and can change it.
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

    private val SET_RUN_PLAN_TOOL =
        tool(
            SET_RUN_PLAN,
            """
            Saves the target deck for this run's character, replacing any saved before; the
            app sends it with every later request of the run. Write each card with its Epiphany
            version and copy count, which cards the Duplicates go to, which cards to remove,
            and the equipment to take.
            """,
            mapOf(
                "plan" to
                    mapOf(
                        "type" to "string",
                        "description" to "The target deck as short lines",
                    ),
            ),
            listOf("plan"),
        )

    private val IDENTIFY_CHARACTERS_TOOL =
        tool(
            IDENTIFY_CHARACTERS,
            """
            Names the characters drawn on this request's screenshot by comparing each figure with
            every character's card art. Returns a line "Characters on screen, top to bottom, left
            to right: ...": "A or B" means one of the two, too close to choose; "unidentified"
            means no character matched, such as a monster. Card art counts as a figure, so a
            card screen returns its owner's name for each card.
            """,
            emptyMap(),
            emptyList(),
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
                            "Only the plausible characters, as the czn tools spell them, such as " +
                            "the team of three on screen; omit to offer every character",
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
            Shows the player questions with options and waits for the answers. The player picks
            one option, or several when multi_select is true, and can always type their own
            answer instead. Returns each question with its answer, or that the player skipped
            them.
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
        game as the player sees it now; earlier messages are this run's history, with your
        earlier tool calls and their results. Use a result already there instead of calling
        the tool again; a result the API replaced with a cleared placeholder needs a new call.

        The key words MUST, MUST NOT, SHOULD, and MAY are to be read as in RFC 2119.

        - You MUST confirm every card, character, equipment, and partner name or text read
          from the screen with the czn tools before relying on it. Make independent lookups in
          parallel. Where the screen's values differ from the tools' base values, the screen
          shows the player's actual values; prefer it. czn tool results leave out empty
          fields, so a missing field means the data has no value for it.
        - Before you name a character the screen shows only as a picture, you MUST call
          identify_characters; its answer is authoritative, and earlier player messages carry
          the answer for their screenshot. Narrow "A or B" to one only when the screen shows a
          name. You MUST NOT name a character from looks, element icons, or class icons.
        - When you check a card's version (base, Epiphany I to V), you MUST state the version
          the czn tools matched, then how it differs from what the plan or the player wants. A
          version name on screen, often in parentheses after the card's name, decides the
          version: match it to alias.
        - You MUST NOT advise a step the screen shows is already done, or call the player's
          state a match for a plan before you have checked each part of it; name the parts
          still unchecked.
        - You MUST state only player facts the screen or this run's history shows, never
          invented progress, clears, ownership, or past purchases. When comparing two screens,
          attribute a change only to what both show; when the cause is unclear, say so.
        - Every game fact or recommendation (rules, mechanics, limits, costs, what a button
          does, builds, ratings) MUST come from a sentence on the screen or in a czn tool
          result this run that states it. Your own knowledge of the game is not a source, and
          a related result is not an answer (a result about editing Save Data does not say a
          run starts from one). Call the tools before any reply that needs a fact, follow-ups
          included. When no sentence states it, you MUST search again in the same turn, using
          the game's own words for the feature, menu, or action (such as "Build Preset") from
          the screen or the results, not the player's words. Only if that fails, say you don't
          know and point to the in-game help ("i" button) or a player community; MUST NOT
          suggest trying a control whose purpose no result states. You MUST NOT write that you
          found no source, cannot confirm, or don't know a game fact unless this turn made
          both searches for it.
        - Advise on the player's goal only (a Chaos run's deck, an event, a character's build,
          a team, where to spend points). When neither the player's message nor the history
          states it, you MUST call ask_player before advising, offering 2 to 4 goals this
          screen suggests, the likeliest recommended (on a character screen: optimize this
          character, pick a partner, set up equipment). You need not guess right; the player
          steers over later turns. The goal carries over; when a later screen fits it poorly,
          ask again with goals that fit, the known one first.
        - Team advice MUST start from the current team: say whether it matches a recommended
          one, and suggest only changes. Give it only when the goal is the team.
        - Every reply MUST start with a <seen>...</seen> block: a few short lines on what is on
          screen (names, numbers, choices), so later questions in this run can refer back to
          it. The panel shows it collapsed; nothing may come before it.
        - After the block, the advice MUST be one sentence with the recommended action, then at
          most two short reasons, about 50 words in total: the player reads it mid-game on a
          narrow panel. Go longer only when the player asks for detail. MUST NOT narrate
          lookups or thinking ("Let me check..."), repeat the seen block, or comment on
          anything that needs no action from the player.
        - When the advice depends on something not on this screen (deck, equipment, a card's
          details, map nodes), give any advice this screen supports, then MUST name what to
          tap to show it and end with "Then long-press." You cannot tap for the player; the
          next request carries that screen.
        - The panel renders Markdown. You SHOULD bold card and choice names. You MUST NOT use
          headings or tables.
        - A Chaos run builds one character's deck; the run state at the end of the newest
          message names that character and the run's saved plan, or says they are not known
          yet. When the goal is a Chaos run and the character is not known, you MUST settle it
          with set_run_character or ask_run_character before advising on the run. You MUST
          NOT guess it, or ask again once it is known unless the screen contradicts it.
        - Once the character is known and no plan is saved, you MUST settle the run's target
          deck from plan_deck and the player's deck and save it with set_run_plan before
          advising a pick; save it again when the plan changes. On a Duplicate, Epiphany,
          Divine Epiphany, removal, or card reward screen, you MUST check each option against
          the saved plan and name the part of the plan the pick serves, such as the card the
          plan sends Duplicates to.
        - When the best action depends on something only the player knows (what they own, a
          preference) and neither the screen nor the history settles it, you MAY call
          ask_player, batching every open question into one call. You SHOULD advise on a
          reasonable guess instead when one exists, and MUST NOT ask what a screenshot could
          show; name what to tap instead.
        - Some past replies show the questions you asked, with the player's answers, as
          <asked> blocks the app adds. You MUST NOT write <asked> blocks yourself.
        """.trimIndent()
}
