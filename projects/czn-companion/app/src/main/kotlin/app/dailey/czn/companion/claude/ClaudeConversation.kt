package app.dailey.czn.companion.claude

import app.dailey.czn.companion.ask.Answer
import app.dailey.czn.companion.ask.AnsweredAsk
import app.dailey.czn.companion.ask.Option
import app.dailey.czn.companion.ask.PlayerAsks
import app.dailey.czn.companion.ask.Question
import app.dailey.czn.companion.auth.AnthropicAuth
import app.dailey.czn.companion.auth.CznDataAuth
import app.dailey.czn.companion.auth.SignInRequired
import app.dailey.czn.companion.characters.ScreenCharacters
import app.dailey.czn.companion.run.OpenTurn
import app.dailey.czn.companion.run.RunRepository
import com.anthropic.client.AnthropicClient
import com.anthropic.client.okhttp.AnthropicOkHttpClient
import com.anthropic.core.JsonValue
import com.anthropic.errors.AnthropicIoException
import com.anthropic.errors.AnthropicServiceException
import com.anthropic.errors.UnauthorizedException
import com.anthropic.helpers.BetaMessageAccumulator
import com.anthropic.models.beta.messages.BetaContentBlockParam
import com.anthropic.models.beta.messages.BetaMessage
import com.anthropic.models.beta.messages.BetaMessageParam
import com.anthropic.models.beta.messages.BetaStopReason
import com.anthropic.models.beta.messages.BetaToolResultBlockParam
import com.anthropic.models.beta.messages.BetaToolUseBlockParam
import com.anthropic.models.beta.messages.MessageCreateParams
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.FlowCollector
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.job
import java.io.IOException

sealed interface ReplyEvent {
    data class TextDelta(
        val text: String,
    ) : ReplyEvent

    /** Claude called a CZN data tool; [label] is the tool name and its main argument. */
    data class ToolCall(
        val label: String,
    ) : ReplyEvent

    /** The player answered or skipped questions Claude asked; the reply continues. */
    data class Asked(
        val ask: AnsweredAsk,
    ) : ReplyEvent

    data object Done : ReplyEvent

    data class Failed(
        val kind: FailureKind,
        val message: String,
    ) : ReplyEvent
}

enum class FailureKind { Network, AnthropicSignIn, CznSignIn, Api }

/**
 * What Claude can ask of the player mid-reply. Each call suspends until the player answers; the
 * reply waits meanwhile, and cancelling the reply cancels the wait.
 */
interface PlayerInput {
    /** Shows the whole roster and returns the pick, or null when the player closes it. */
    suspend fun pickCharacter(): String?

    /**
     * Shows [questions] in the conversation and returns one answer per question, or null when
     * the player skips them.
     */
    suspend fun ask(questions: List<Question>): List<Answer>?
}

/**
 * One screenshot as JPEG twice: [claude] at the size Claude reads, [full] at the screen's
 * resolution for naming the characters on it ([ScreenCharacters]).
 */
class Screenshot(
    val claude: ByteArray,
    val full: ByteArray,
)

/**
 * Asks Claude about the current screen with the active run as context, streaming the reply. A
 * reply can take several requests: whenever Claude stops to call an app tool, the app runs it
 * (asking the player through [PlayerInput] if needed) and sends the result back. The exchange in
 * progress is kept as the run's [OpenTurn], so it can [resume] after the app is killed. It joins
 * the run's conversation only after the reply completes; a failed or stopped request leaves the
 * conversation unchanged, though a character set before the failure stays set.
 */
class ClaudeConversation(
    private val anthropic: AnthropicAuth,
    private val cznData: CznDataAuth,
    private val run: RunRepository,
    private val screenCharacters: ScreenCharacters,
) {
    val entries get() = run.entries

    suspend fun entriesNow() = run.entriesNow()

    val character get() = run.character

    suspend fun setCharacter(name: String) = run.setCharacter(name)

    suspend fun newRun() = run.newRun()

    fun ask(
        text: String,
        screenshot: Screenshot?,
        player: PlayerInput,
    ): Flow<ReplyEvent> =
        flow {
            val anthropicToken =
                tokenOrNull(FailureKind.AnthropicSignIn) { anthropic.accessToken() } ?: return@flow
            val cznToken =
                tokenOrNull(FailureKind.CznSignIn) { cznData.accessToken() } ?: return@flow
            val onScreen = screenshot?.let { screenCharacters.identify(it.full) }
            val open =
                OpenTurn(
                    text,
                    onScreen.orEmpty(),
                    screenshot?.claude,
                    "",
                    emptyList(),
                    emptyList(),
                    encodeTurn(emptyList()),
                )
            run.saveOpenTurn(open)
            continueTurn(open, anthropicToken, cznToken, player)
        }.flowOn(Dispatchers.IO)

    /** The exchange the app was in when it last stopped, if it never finished. */
    suspend fun openTurn(): OpenTurn? = run.openTurn()

    /**
     * Continues [openTurn] where it was saved: questions Claude asked that wait for the player
     * are asked again, and a request cut off mid-reply is sent again from its start.
     */
    fun resume(player: PlayerInput): Flow<ReplyEvent> =
        flow {
            val open = run.openTurn() ?: return@flow
            val anthropicToken =
                tokenOrNull(FailureKind.AnthropicSignIn) { anthropic.accessToken() }
            val cznToken = tokenOrNull(FailureKind.CznSignIn) { cznData.accessToken() }
            if (anthropicToken == null || cznToken == null) {
                run.discardOpenTurn()
                return@flow
            }
            continueTurn(open, anthropicToken, cznToken, player)
        }.flowOn(Dispatchers.IO)

    /** Ends the open exchange without saving it, as when the player stops the reply. */
    suspend fun discardOpenTurn() = run.discardOpenTurn()

    /**
     * Runs the exchange from [open] to its end, saving it after every request and every round of
     * app tools. It is saved to the run when the reply completes and discarded when it fails.
     */
    private suspend fun FlowCollector<ReplyEvent>.continueTurn(
        open: OpenTurn,
        anthropicToken: String,
        cznToken: String,
        player: PlayerInput,
    ) {
        val history = run.entriesNow()
        // Read once, so the request prefix stays identical, and cached, across this turn.
        val character = run.characterNow()
        val reply = StringBuilder(open.reply)
        val tools = open.tools.toMutableList()
        val asks = open.asks.toMutableList()
        val turn = decodeTurn(open.turn).toMutableList()

        suspend fun save() =
            run.saveOpenTurn(
                OpenTurn(
                    open.question,
                    open.screen,
                    open.jpeg,
                    reply.toString(),
                    tools.toList(),
                    asks.toList(),
                    encodeTurn(turn),
                ),
            )

        suspend fun fail(failure: ReplyEvent.Failed) {
            run.discardOpenTurn()
            emit(failure)
        }
        val client = client(anthropicToken)
        try {
            repeat(MAX_ROUNDS) {
                val uses = waitingToolUses(turn.lastOrNull())
                if (uses.isNotEmpty()) {
                    turn += toolResults(uses) { use -> runTool(use, player, reply, asks) }
                    save()
                }
                val params =
                    MessagesRequest.build(
                        history,
                        open.question,
                        open.jpeg,
                        CznDataAuth.MCP_URL,
                        cznToken,
                        character,
                        turn,
                        open.screen.ifBlank { null },
                    )
                val message = stream(client, params, reply, tools)
                when (message.stopReason().orElse(null)) {
                    // App tools run at the start of the next round, from the saved message.
                    // A paused reply (a long server-side tool call) resumes when resent.
                    BetaStopReason.TOOL_USE, BetaStopReason.PAUSE_TURN -> {
                        turn += message.toParam()
                        save()
                    }

                    else -> {
                        run.appendExchange(
                            open.question,
                            reply.toString(),
                            tools,
                            asks,
                            open.screen,
                        )
                        emit(ReplyEvent.Done)
                        return
                    }
                }
            }
            fail(ReplyEvent.Failed(FailureKind.Api, "Claude kept calling tools; try again."))
        } catch (e: UnauthorizedException) {
            fail(ReplyEvent.Failed(FailureKind.AnthropicSignIn, e.message.orEmpty()))
        } catch (e: AnthropicServiceException) {
            fail(serviceFailure(e))
        } catch (e: AnthropicIoException) {
            fail(ReplyEvent.Failed(FailureKind.Network, e.message ?: "Network error."))
        } catch (e: IOException) {
            fail(ReplyEvent.Failed(FailureKind.Network, e.message ?: "Network error."))
        } finally {
            client.close()
        }
    }

    /**
     * A client for one reply: the subscription token rotates, and the SDK takes it when the client
     * is built. The identity line, betas, and these headers are what subscription tokens need.
     */
    private fun client(token: String): AnthropicClient =
        AnthropicOkHttpClient
            .builder()
            .authToken(token)
            .putHeader("User-Agent", "claude-cli/2.1.322 (external, cli)")
            .putHeader("x-app", "cli")
            .build()

    /**
     * Sends one request and streams its text into [reply] and its CZN data lookups into [tools].
     * The SDK's stream blocks a thread, so the flow runs on [Dispatchers.IO], and cancelling the
     * reply closes the stream to stop the read.
     */
    private suspend fun FlowCollector<ReplyEvent>.stream(
        client: AnthropicClient,
        params: MessageCreateParams,
        reply: StringBuilder,
        tools: MutableList<String>,
    ): BetaMessage {
        val accumulator = BetaMessageAccumulator.create()
        // Name and streamed input of each CZN data lookup, by content block index, until it ends.
        val lookups = mutableMapOf<Long, Pair<String, StringBuilder>>()
        client.beta().messages().createStreaming(params).use { response ->
            val closer = currentCoroutineContext().job.invokeOnCompletion { response.close() }
            try {
                for (event in response.stream().iterator()) {
                    accumulator.accumulate(event)
                    event.contentBlockStart().orElse(null)?.let { start ->
                        val block = start.contentBlock()
                        if (block.isText() && reply.isNotEmpty()) {
                            reply.append("\n\n")
                            emit(ReplyEvent.TextDelta("\n\n"))
                        }
                        block.mcpToolUse().orElse(null)?.let {
                            lookups[start.index()] = it.name() to StringBuilder()
                        }
                    }
                    event.contentBlockDelta().orElse(null)?.let { delta ->
                        delta.delta().text().orElse(null)?.let {
                            reply.append(it.text())
                            emit(ReplyEvent.TextDelta(it.text()))
                        }
                        delta.delta().inputJson().orElse(null)?.let {
                            lookups[delta.index()]?.second?.append(it.partialJson())
                        }
                    }
                    event.contentBlockStop().orElse(null)?.let { stop ->
                        lookups.remove(stop.index())?.let { (name, input) ->
                            val label = toolLabel(name, input.toString())
                            tools += label
                            emit(ReplyEvent.ToolCall(label))
                        }
                    }
                }
            } finally {
                closer.dispose()
            }
        }
        return accumulator.message()
    }

    /** Runs the app tools in [uses] and returns their results as one message. */
    private suspend fun toolResults(
        uses: List<BetaToolUseBlockParam>,
        runTool: suspend (BetaToolUseBlockParam) -> Pair<String, Boolean>,
    ): BetaMessageParam {
        val results =
            uses.map { use ->
                val (content, isError) = runTool(use)
                BetaContentBlockParam.ofToolResult(
                    BetaToolResultBlockParam
                        .builder()
                        .toolUseId(use.id())
                        .content(content)
                        .apply { if (isError) isError(true) }
                        .build(),
                )
            }
        return BetaMessageParam.ofUserOfContentBlockParams(results)
    }

    /**
     * Runs one app tool. Questions for the player are recorded in [asks] at the current end of
     * [reply], so the saved reply shows each where Claude asked it.
     */
    private suspend fun FlowCollector<ReplyEvent>.runTool(
        use: BetaToolUseBlockParam,
        player: PlayerInput,
        reply: StringBuilder,
        asks: MutableList<AnsweredAsk>,
    ): Pair<String, Boolean> {
        val inputJson = JSON.writeValueAsString(use._input())
        val input = JsonValue.from(JSON.readTree(inputJson)).asObject().orElse(emptyMap())

        suspend fun ask(questions: List<Question>): List<Answer>? {
            val answers = player.ask(questions)
            val ask = AnsweredAsk(reply.length, questions, answers)
            asks += ask
            emit(ReplyEvent.Asked(ask))
            return answers
        }
        return when (use.name()) {
            MessagesRequest.ASK_PLAYER -> {
                PlayerAsks.parse(inputJson).fold(
                    onSuccess = { questions ->
                        PlayerAsks.describe(questions, ask(questions)) to
                            false
                    },
                    onFailure = { it.message.orEmpty() to true },
                )
            }

            MessagesRequest.SET_RUN_CHARACTER -> {
                val character = input["name"]?.asString()?.orElse(null)?.trim()
                if (character.isNullOrEmpty()) {
                    "name is required." to true
                } else {
                    run.setCharacter(character)
                    "Saved. The run's character is $character." to false
                }
            }

            MessagesRequest.ASK_RUN_CHARACTER -> {
                val candidates =
                    input["candidates"]
                        ?.asArray()
                        ?.orElse(null)
                        ?.mapNotNull { it.asString().orElse(null)?.trim() }
                        ?.filter(String::isNotEmpty)
                        .orEmpty()
                val answer =
                    if (candidates.isEmpty()) {
                        player.pickCharacter()?.let { Answer(picked = listOf(it)) }
                    } else {
                        ask(listOf(characterQuestion(candidates)))?.single()
                    }
                val picked = answer?.picked?.singleOrNull()
                val typed = answer?.typed
                when {
                    picked != null -> {
                        run.setCharacter(picked)
                        "The player picked $picked; it is saved as the run's character." to false
                    }

                    typed != null -> {
                        "The player typed \"$typed\", which is not saved. Find the name as the " +
                            "czn tools spell it and call set_run_character." to false
                    }

                    else -> {
                        "The player skipped the choice." to false
                    }
                }
            }

            else -> {
                "Unknown tool: ${use.name()}" to true
            }
        }
    }

    private suspend fun FlowCollector<ReplyEvent>.tokenOrNull(
        kind: FailureKind,
        get: suspend () -> String,
    ): String? =
        try {
            get()
        } catch (e: SignInRequired) {
            emit(ReplyEvent.Failed(kind, e.message.orEmpty()))
            null
        } catch (e: IOException) {
            emit(ReplyEvent.Failed(FailureKind.Network, e.message ?: "Network error."))
            null
        }

    /** An API error; an MCP auth error means the CZN data token, not Claude's, was refused. */
    private fun serviceFailure(e: AnthropicServiceException): ReplyEvent.Failed {
        val message = e.message.orEmpty()
        val lower = message.lowercase()
        val kind =
            if ("mcp" in lower && ("auth" in lower || "401" in lower)) {
                FailureKind.CznSignIn
            } else {
                FailureKind.Api
            }
        return ReplyEvent.Failed(kind, "${e.statusCode()}: $message")
    }

    /** "get_card: Blade of the Cycle": the tool name and its first text argument, if any. */
    private fun toolLabel(
        name: String,
        input: String,
    ): String {
        val arguments =
            runCatching { JsonValue.from(JSON.readTree(input)).asObject().orElse(null) }
                .getOrNull()
        val first = arguments?.values?.firstNotNullOfOrNull { it.asString().orElse(null) }
        return if (first == null) name else "$name: $first"
    }

    private fun characterQuestion(candidates: List<String>) =
        Question(
            question = "Which character's deck does this run build?",
            header = "Run character",
            options = candidates.map { Option(it) },
            characters = true,
        )

    private companion object {
        val JSON = com.anthropic.core.jsonMapper()

        /** [turn] as stored in [OpenTurn.turn]. */
        fun encodeTurn(turn: List<BetaMessageParam>): String = JSON.writeValueAsString(turn)

        fun decodeTurn(json: String): List<BetaMessageParam> =
            JSON.readValue(json, Array<BetaMessageParam>::class.java).toList()

        /** App tools Claude called in [message] that have no results yet: its last message. */
        fun waitingToolUses(message: BetaMessageParam?): List<BetaToolUseBlockParam> {
            if (message?.role() != BetaMessageParam.Role.ASSISTANT) return emptyList()
            return message
                .content()
                .orElse(null)
                ?.contentBlockParams()
                ?.orElse(null)
                .orEmpty()
                .mapNotNull { it.toolUse().orElse(null) }
        }

        /** Requests per reply; a reply that needs more is assumed stuck in a tool loop. */
        const val MAX_ROUNDS = 8
    }
}
