package app.dailey.czn.companion.claude

import app.dailey.czn.companion.auth.AnthropicAuth
import app.dailey.czn.companion.auth.CznDataAuth
import app.dailey.czn.companion.auth.SignInRequired
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
import com.anthropic.models.beta.messages.BetaToolUseBlock
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
fun interface PlayerInput {
    /**
     * Shows the character picker, limited to [candidates] unless empty, and returns the pick, or
     * null when the player closes the picker without choosing.
     */
    suspend fun pickCharacter(candidates: List<String>): String?
}

/**
 * Asks Claude about the current screen with the active run as context, streaming the reply. A
 * reply can take several requests: whenever Claude stops to call an app tool, the app runs it
 * (asking the player through [PlayerInput] if needed) and sends the result back. An exchange is
 * saved to the run only after the reply completes; a failed or cancelled request leaves the
 * conversation unchanged, though a character set before the failure stays set.
 */
class ClaudeConversation(
    private val anthropic: AnthropicAuth,
    private val cznData: CznDataAuth,
    private val run: RunRepository,
) {
    val entries get() = run.entries

    val character get() = run.character

    suspend fun setCharacter(name: String) = run.setCharacter(name)

    suspend fun newRun() = run.newRun()

    fun ask(
        text: String,
        jpeg: ByteArray?,
        player: PlayerInput,
    ): Flow<ReplyEvent> =
        flow {
            val anthropicToken =
                tokenOrNull(FailureKind.AnthropicSignIn) { anthropic.accessToken() } ?: return@flow
            val cznToken =
                tokenOrNull(FailureKind.CznSignIn) { cznData.accessToken() } ?: return@flow
            val history = run.entriesNow()
            // Read once, so the request prefix stays identical, and cached, across this turn.
            val character = run.characterNow()
            val reply = StringBuilder()
            val tools = mutableListOf<String>()
            val turn = mutableListOf<BetaMessageParam>()
            val client = client(anthropicToken)
            try {
                repeat(MAX_ROUNDS) {
                    val params =
                        MessagesRequest.build(
                            history,
                            text,
                            jpeg,
                            CznDataAuth.MCP_URL,
                            cznToken,
                            character,
                            turn,
                        )
                    val message = stream(client, params, reply, tools)
                    when (message.stopReason().orElse(null)) {
                        BetaStopReason.TOOL_USE -> {
                            turn += message.toParam()
                            turn += toolResults(message, player)
                        }

                        // A long server-side tool call paused the reply; resending resumes it.
                        BetaStopReason.PAUSE_TURN -> {
                            turn += message.toParam()
                        }

                        else -> {
                            run.appendExchange(text, reply.toString(), tools)
                            emit(ReplyEvent.Done)
                            return@flow
                        }
                    }
                }
                emit(ReplyEvent.Failed(FailureKind.Api, "Claude kept calling tools; try again."))
            } catch (e: UnauthorizedException) {
                emit(ReplyEvent.Failed(FailureKind.AnthropicSignIn, e.message.orEmpty()))
            } catch (e: AnthropicServiceException) {
                emit(serviceFailure(e))
            } catch (e: AnthropicIoException) {
                emit(ReplyEvent.Failed(FailureKind.Network, e.message ?: "Network error."))
            } catch (e: IOException) {
                emit(ReplyEvent.Failed(FailureKind.Network, e.message ?: "Network error."))
            } finally {
                client.close()
            }
        }.flowOn(Dispatchers.IO)

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

    /** Runs every app tool Claude called in [message] and returns their results as one message. */
    private suspend fun toolResults(
        message: BetaMessage,
        player: PlayerInput,
    ): BetaMessageParam {
        val results =
            message.content().mapNotNull { it.toolUse().orElse(null) }.map { use ->
                val (content, isError) = runTool(use, player)
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

    private suspend fun runTool(
        use: BetaToolUseBlock,
        player: PlayerInput,
    ): Pair<String, Boolean> {
        val input = use._input().asObject().orElse(emptyMap())
        return when (use.name()) {
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
                val picked = player.pickCharacter(candidates)
                if (picked == null) {
                    "The player skipped the choice." to false
                } else {
                    run.setCharacter(picked)
                    "The player picked $picked; it is saved as the run's character." to false
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

    private companion object {
        val JSON = com.anthropic.core.jsonMapper()

        /** Requests per reply; a reply that needs more is assumed stuck in a tool loop. */
        const val MAX_ROUNDS = 8
    }
}
