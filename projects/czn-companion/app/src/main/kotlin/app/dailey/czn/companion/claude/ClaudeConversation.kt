package app.dailey.czn.companion.claude

import android.database.sqlite.SQLiteException
import android.util.Log
import app.dailey.czn.companion.ask.Answer
import app.dailey.czn.companion.ask.AnsweredAsk
import app.dailey.czn.companion.ask.PlayerAsks
import app.dailey.czn.companion.ask.Question
import app.dailey.czn.companion.auth.AnthropicAuth
import app.dailey.czn.companion.auth.CznDataAuth
import app.dailey.czn.companion.auth.SignInRequired
import app.dailey.czn.companion.characters.ScreenIdentifier
import app.dailey.czn.companion.run.CardInfo
import app.dailey.czn.companion.run.DeckChanges
import app.dailey.czn.companion.run.DeckDetails
import app.dailey.czn.companion.run.OpenShot
import app.dailey.czn.companion.run.OpenTurn
import app.dailey.czn.companion.run.RequestUsageRow
import app.dailey.czn.companion.run.RunDecks
import app.dailey.czn.companion.run.RunRepository
import app.dailey.czn.companion.run.RunUpdate
import app.dailey.czn.companion.run.SaveDataCost
import app.dailey.czn.companion.run.ScreenshotArchive
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
import com.anthropic.models.beta.messages.BetaTextBlockParam
import com.anthropic.models.beta.messages.BetaToolResultBlockParam
import com.anthropic.models.beta.messages.BetaToolUseBlockParam
import com.anthropic.models.beta.messages.MessageCreateParams
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
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

    /** A run state update changed the decks; [lines] say what changed ([DeckChanges]). */
    data class DeckChanged(
        val lines: List<String>,
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
 * resolution for naming the characters on it ([ScreenIdentifier]) and for [ScreenshotArchive].
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
    private val screenIdentifier: ScreenIdentifier,
    private val saveDataCost: SaveDataCost,
    private val deckDetails: DeckDetails,
    private val archive: ScreenshotArchive,
) {
    /** The active run's deck state, for the deck view. */
    val deck get() = run.deck

    val entries get() = run.entries

    suspend fun entriesNow() = run.entriesNow()

    val character get() = run.character

    suspend fun setCharacter(name: String) = run.setCharacter(name)

    suspend fun newRun() = run.newRun()

    /** Asks [text] about [screenshots], in the order the player took them. */
    fun ask(
        text: String,
        screenshots: List<Screenshot>,
        player: PlayerInput,
    ): Flow<ReplyEvent> =
        flow {
            val anthropicToken =
                tokenOrNull(FailureKind.AnthropicSignIn) { anthropic.accessToken() } ?: return@flow
            val cznToken =
                tokenOrNull(FailureKind.CznSignIn) { cznData.accessToken() } ?: return@flow
            val open =
                OpenTurn(
                    text,
                    "",
                    screenshots.map { OpenShot(it.claude, archive.save(it.full)) },
                    "",
                    emptyList(),
                    emptyList(),
                    encodeTurn(emptyList()),
                )
            run.startOpenTurn(open)
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
        // Read once, so the newest message stays identical, and cached, across this turn.
        val state = RunState(run.characterNow(), run.deckNow())
        var previousMessageId = run.lastMessageId()
        var round = 0
        val reply = StringBuilder(open.reply)
        val tools = open.tools.toMutableList()
        val asks = open.asks.toMutableList()
        val changes = open.changes.toMutableList()
        val turn = decodeTurn(open.turn).toMutableList()
        // Set when Claude asks for the characters' names; saved with the question.
        var screen = open.screen

        suspend fun save() =
            run.saveOpenTurn(
                OpenTurn(
                    open.question,
                    screen,
                    open.shots,
                    reply.toString(),
                    tools.toList(),
                    asks.toList(),
                    encodeTurn(turn),
                    changes.toList(),
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
                    turn +=
                        toolResults(uses) { use ->
                            if (use.name() == MessagesRequest.IDENTIFY_SCREEN) {
                                identifyScreen(open.shots.map { it.screenshot }) { screen = it }
                            } else {
                                runTool(use, player, reply, asks, changes)
                            }
                        }
                    save()
                }
                val params =
                    MessagesRequest.build(
                        history,
                        open.question,
                        open.shots.map { it.jpeg },
                        CznDataAuth.MCP_URL,
                        cznToken,
                        state,
                        turn,
                        previousMessageId,
                    )
                val started = System.currentTimeMillis()
                val message = stream(client, params, reply, tools)
                round += 1
                recordUsage(message, started, round, params.messages().size)
                previousMessageId = message.id()
                when (message.stopReason().orElse(null)) {
                    // App tools run at the start of the next round, from the saved message.
                    // A paused reply (a long server-side tool call) resumes when resent.
                    BetaStopReason.TOOL_USE, BetaStopReason.PAUSE_TURN -> {
                        val param = message.toParam()
                        turn += param
                        // A tool_use stop can come with only CZN data lookups and thinking (seen
                        // on device, 2026-11-22). Resent as is, the request would end on Claude's
                        // message, which the model rejects as prefill.
                        val stalled =
                            message.stopReason().get() == BetaStopReason.TOOL_USE &&
                                waitingToolUses(param).isEmpty()
                        if (stalled) turn += CONTINUE
                        save()
                    }

                    else -> {
                        run.appendExchange(
                            open.question,
                            reply.toString(),
                            tools,
                            asks,
                            screen,
                            open.shots.map { it.screenshot },
                            encodeTurn(turn + message.toParam()),
                            changes,
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

    /**
     * Saves one request's token use and cache behavior to the run ([RequestUsageRow]) and logs
     * it under [USAGE_TAG]. A failure to save never fails the reply.
     */
    private suspend fun recordUsage(
        message: BetaMessage,
        started: Long,
        round: Int,
        messages: Int,
    ) {
        val usage = message.usage()
        val creation = usage.cacheCreation().orElse(null)
        val cleared =
            message
                .contextManagement()
                .orElse(null)
                ?.appliedEdits()
                .orEmpty()
                .mapNotNull { it.clearToolUses20250919().orElse(null) }
        val miss =
            message
                .diagnostics()
                .orElse(null)
                ?.cacheMissReason()
                ?.orElse(null)
        val missJson = miss?.let { JSON.readTree(JSON.writeValueAsString(it)) }
        val passes =
            usage
                .iterations()
                .orElse(null)
                .orEmpty()
                .filter { it.isMessage() }
                .map {
                    it.inputTokens() + it.cacheCreationInputTokens() + it.cacheReadInputTokens()
                }
        val row =
            RequestUsageRow(
                runId = 0,
                startedAt = started,
                durationMs = System.currentTimeMillis() - started,
                messageId = message.id(),
                round = round,
                stopReason = message.stopReason().map { it.asString() }.orElse(""),
                messages = messages,
                inputTokens = usage.inputTokens(),
                cacheWrite5mTokens =
                    creation?.ephemeral5mInputTokens()
                        ?: usage.cacheCreationInputTokens().orElse(0),
                cacheWrite1hTokens = creation?.ephemeral1hInputTokens() ?: 0,
                cacheReadTokens = usage.cacheReadInputTokens().orElse(0),
                outputTokens = usage.outputTokens(),
                clearedToolUses = cleared.sumOf { it.clearedToolUses() },
                clearedInputTokens = cleared.sumOf { it.clearedInputTokens() },
                cacheMissReason = missJson?.get("type")?.asText().orEmpty(),
                cacheMissedTokens = missJson?.get("cache_missed_input_tokens")?.asLong() ?: 0,
                iterations = passes.size,
                largestPromptTokens = passes.maxOrNull() ?: 0,
            )
        // The response's block types in order, such as thinking,mcp_tool_use,text.
        val blocks =
            message.content().joinToString(",") {
                JSON.readTree(JSON.writeValueAsString(it))["type"]?.asText().orEmpty()
            }
        Log.i(USAGE_TAG, "$row blocks=$blocks")
        try {
            run.saveUsage(row)
        } catch (e: SQLiteException) {
            Log.w(USAGE_TAG, "usage not saved", e)
        }
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
     * Names the characters and the gear in slots on every archived screenshot of this request
     * for Claude, in one data server call. The result has one section per screenshot, labeled as
     * its image is in the request; [onScreen] receives the characters lines, labeled the same
     * way, to save with the question. The server's answer for each screenshot joins the archive.
     */
    private suspend fun identifyScreen(
        screenshots: List<String>,
        onScreen: (String) -> Unit,
    ): Pair<String, Boolean> {
        val jpegs = screenshots.associateWith { archive.read(it) }
        val kept = jpegs.filterValues { it != null }.keys.toList()
        if (kept.isEmpty()) {
            return "This request's screenshots are no longer kept; call the characters " +
                "unidentified and name no gear from their icons." to true
        }
        val found = screenIdentifier.identify(kept.map { jpegs.getValue(it)!! })
        if (found == null) {
            return "The data server did not answer; call the characters unidentified and name " +
                "no gear from their icons." to true
        }
        val byName = kept.zip(found).toMap()
        byName.forEach { (name, shot) -> archive.saveIdentify(name, shot.response) }
        val labels = screenshots.indices.map(MessagesRequest::screenshotLabel)
        val shots = screenshots.map(byName::get)
        val characters =
            shots.zip(labels).mapNotNull { (shot, label) ->
                shot?.characters?.let { "$label: $it" }
            }
        if (characters.isNotEmpty()) onScreen(characters.joinToString("\n"))
        val sections =
            shots.zip(labels).map { (shot, label) ->
                val text =
                    if (shot == null) {
                        "This screenshot is no longer kept; call its characters unidentified " +
                            "and name no gear from its icons."
                    } else {
                        (shot.characters ?: "No character figures found on this screenshot.") +
                            "\n" +
                            (shot.gear ?: "No gear slot on this screenshot shows a known icon.")
                    }
                "$label:\n$text"
            }
        return sections.joinToString("\n") to false
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
        changes: MutableList<String>,
    ): Pair<String, Boolean> {
        val inputJson = JSON.writeValueAsString(use._input())
        return when (use.name()) {
            MessagesRequest.ASK_PLAYER -> {
                PlayerAsks.parse(inputJson).fold(
                    onSuccess = { questions -> askPlayer(questions, player, reply, asks) to false },
                    onFailure = { it.message.orEmpty() to true },
                )
            }

            MessagesRequest.UPDATE_RUN_STATE -> {
                RunDecks.parse(inputJson).fold(
                    onSuccess = { update -> updateRunState(update, changes) to false },
                    onFailure = { it.message.orEmpty() to true },
                )
            }

            else -> {
                "Unknown tool: ${use.name()}" to true
            }
        }
    }

    /**
     * Asks the player [questions] and returns the answers for Claude. A run-character question
     * without options shows the whole roster instead and must be asked alone ([PlayerAsks]); a
     * character picked in a run-character question is saved as the run's character.
     */
    private suspend fun FlowCollector<ReplyEvent>.askPlayer(
        questions: List<Question>,
        player: PlayerInput,
        reply: StringBuilder,
        asks: MutableList<AnsweredAsk>,
    ): String {
        val roster = questions.singleOrNull()?.takeIf { it.characters && it.options.isEmpty() }
        val answers =
            if (roster != null) {
                player.pickCharacter()?.let { listOf(Answer(picked = listOf(it))) }
            } else {
                player.ask(questions).also {
                    val ask = AnsweredAsk(reply.length, questions, it)
                    asks += ask
                    emit(ReplyEvent.Asked(ask))
                }
            }
        val saved =
            questions.zip(answers.orEmpty()).firstNotNullOfOrNull { (question, answer) ->
                answer.picked.singleOrNull()?.takeIf { question.characters }
            }
        saved?.let { run.setCharacter(it) }
        return listOfNotNull(
            PlayerAsks.describe(questions, answers),
            saved?.let { "$it is saved as the run's character." },
        ).joinToString("\n")
    }

    /**
     * Applies Claude's run state [update]; [changes] receives what it changed for the chat. Then
     * costs each deck whose cards or removals changed with the data server's Faint Memory rules
     * (the current deck's cost is its points unless the update gives points), and asks the data
     * server for every new card and item name's game data and pictures.
     */
    private suspend fun FlowCollector<ReplyEvent>.updateRunState(
        update: RunUpdate,
        changes: MutableList<String>,
    ): String {
        val character = update.character?.trim()
        character?.let { run.setCharacter(it) }
        val (before, applied) = run.changeDeck { RunDecks.apply(it, update) }
        val owner = character ?: run.characterNow()
        val target = owner?.takeIf { update.target?.costed == true }
        val current = owner?.takeIf { update.current?.costed == true }
        val targetCost = target?.let { saveDataCost.cost(it, applied.target) }
        val currentCost = current?.let { saveDataCost.cost(it, applied.current) }
        val (cards, gear) = RunDecks.missingInfo(applied)
        val (cardInfo, gearInfo) =
            owner?.let { deckDetails.find(it, cards, gear) }
                ?: (emptyMap<String, CardInfo>() to emptyMap())
        val (_, deck) =
            run.changeDeck { saved ->
                val deck = saved ?: applied
                deck.copy(
                    targetPoints = targetCost?.total ?: deck.targetPoints,
                    points = currentCost?.takeIf { update.points == null }?.total ?: deck.points,
                    cardInfo = deck.cardInfo + cardInfo,
                    gearInfo = deck.gearInfo + gearInfo,
                )
            }
        val lines = DeckChanges.lines(before, deck)
        if (lines.isNotEmpty()) {
            changes += lines
            emit(ReplyEvent.DeckChanged(lines))
        }
        return buildList {
            add("Saved. The run state now:")
            character?.let { add("Character: $it") }
            add(RunDecks.describe(deck))
            if (target != null) add("Target: " + SaveDataCost.describe(target, targetCost))
            if (current != null) add("Current: " + SaveDataCost.describe(current, currentCost))
            if (owner == null && (update.target != null || update.current != null)) {
                add("The run's character is not known yet, so the decks are not costed.")
            }
        }.joinToString("\n")
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

    /** "character_guide: Rossweisse": the tool name and its first text argument, if any. */
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

        /** The player's message that lets a stalled reply go on. */
        val CONTINUE: BetaMessageParam =
            BetaMessageParam.ofUserOfContentBlockParams(
                listOf(
                    BetaContentBlockParam.ofText(
                        BetaTextBlockParam.builder().text("Continue.").build(),
                    ),
                ),
            )

        /** Requests per reply; a reply that needs more is assumed stuck in a tool loop. */
        const val MAX_ROUNDS = 8

        /** Logcat tag of each request's usage line (`adb logcat -s CznUsage`). */
        const val USAGE_TAG = "CznUsage"
    }
}
