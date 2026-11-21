package app.dailey.czn.companion.claude

import app.dailey.czn.companion.auth.AnthropicAuth
import app.dailey.czn.companion.auth.CznDataAuth
import app.dailey.czn.companion.auth.SignInRequired
import app.dailey.czn.companion.run.RunRepository
import io.ktor.client.HttpClient
import io.ktor.client.request.header
import io.ktor.client.request.preparePost
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsChannel
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import io.ktor.utils.io.readUTF8Line
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.FlowCollector
import kotlinx.coroutines.flow.flow
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
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
    suspend fun pickCharacter(
        candidates: List<String>,
        reason: String?,
    ): String?
}

/**
 * Asks Claude about the current screen with the active run as context, streaming the reply. A
 * reply can take several requests: whenever Claude stops to call an app tool, the app runs it
 * (asking the player through [PlayerInput] if needed) and sends the result back. An exchange is
 * saved to the run only after the reply completes; a failed or cancelled request leaves the
 * conversation unchanged, though a character set before the failure stays set.
 */
class ClaudeConversation(
    private val http: HttpClient,
    private val anthropic: AnthropicAuth,
    private val cznData: CznDataAuth,
    private val run: RunRepository,
) {
    private val json = Json { ignoreUnknownKeys = true }

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
            val turn = mutableListOf<JsonObject>()
            try {
                repeat(MAX_ROUNDS) {
                    val body =
                        MessagesRequest.build(
                            history,
                            text,
                            jpeg,
                            CznDataAuth.MCP_URL,
                            cznToken,
                            character,
                            turn,
                        )
                    val round = stream(anthropicToken, body, reply, tools) ?: return@flow
                    val assistant = assistantMessage(round.blocks)
                    when (round.stopReason) {
                        "tool_use" -> {
                            turn += assistant
                            turn += toolResults(round.blocks, player)
                        }

                        // A long server-side tool call paused the reply; resending resumes it.
                        "pause_turn" -> {
                            turn += assistant
                        }

                        else -> {
                            run.appendExchange(text, reply.toString(), tools)
                            emit(ReplyEvent.Done)
                            return@flow
                        }
                    }
                }
                emit(ReplyEvent.Failed(FailureKind.Api, "Claude kept calling tools; try again."))
            } catch (e: IOException) {
                emit(ReplyEvent.Failed(FailureKind.Network, e.message ?: "Network error."))
            }
        }

    /** One response's content blocks, as the API streamed them, and why it stopped. */
    private class Round(
        val blocks: List<JsonObject>,
        val stopReason: String?,
    )

    /**
     * Sends one request and streams its text into [reply]; null after emitting a failure. Blocks
     * are rebuilt from the stream so they can be sent back verbatim when Claude calls an app tool.
     */
    private suspend fun FlowCollector<ReplyEvent>.stream(
        token: String,
        body: JsonObject,
        reply: StringBuilder,
        tools: MutableList<String>,
    ): Round? =
        http
            .preparePost(MESSAGES_URL) {
                header(HttpHeaders.Authorization, "Bearer $token")
                header("anthropic-version", "2023-06-01")
                header("anthropic-beta", MessagesRequest.BETAS.joinToString(","))
                header(HttpHeaders.UserAgent, "claude-cli/2.1.322 (external, cli)")
                header("x-app", "cli")
                contentType(ContentType.Application.Json)
                setBody(body.toString())
            }.execute { response ->
                if (response.status != HttpStatusCode.OK) {
                    emit(httpFailure(response.status, response.bodyAsText()))
                    return@execute null
                }
                val channel = response.bodyAsChannel()
                val blocks = mutableListOf<MutableMap<String, JsonElement>>()
                val texts = mutableMapOf<Int, StringBuilder>()
                val inputs = mutableMapOf<Int, StringBuilder>()
                var stopReason: String? = null
                while (true) {
                    val line = channel.readUTF8Line() ?: break
                    if (!line.startsWith("data:")) continue
                    val event =
                        json
                            .parseToJsonElement(
                                line.removePrefix("data:").trim(),
                            ).jsonObject
                    val index = event["index"]?.jsonPrimitive?.intOrNull ?: -1
                    when (event.string("type")) {
                        "content_block_start" -> {
                            val block = event.obj("content_block") ?: continue
                            blocks += block.toMutableMap()
                            if (block.string("type") == "text") {
                                texts[index] = StringBuilder()
                                if (reply.isNotEmpty()) {
                                    reply.append("\n\n")
                                    emit(ReplyEvent.TextDelta("\n\n"))
                                }
                            }
                        }

                        "content_block_delta" -> {
                            val delta = event.obj("delta") ?: continue
                            when (delta.string("type")) {
                                "text_delta" -> {
                                    val piece = delta.string("text").orEmpty()
                                    texts[index]?.append(piece)
                                    reply.append(piece)
                                    emit(ReplyEvent.TextDelta(piece))
                                }

                                "input_json_delta" -> {
                                    inputs
                                        .getOrPut(index, ::StringBuilder)
                                        .append(delta.string("partial_json").orEmpty())
                                }
                            }
                        }

                        "content_block_stop" -> {
                            val block = blocks.getOrNull(index) ?: continue
                            texts[index]?.let { block["text"] = JsonPrimitive(it.toString()) }
                            val input = inputs[index]?.toString()?.ifBlank { null }
                            if (input != null) block["input"] = json.parseToJsonElement(input)
                            if (block["type"]?.jsonPrimitive?.content == "mcp_tool_use") {
                                val label = toolLabel(block)
                                tools += label
                                emit(ReplyEvent.ToolCall(label))
                            }
                        }

                        "message_delta" -> {
                            stopReason = event.obj("delta")?.string("stop_reason") ?: stopReason
                        }

                        "error" -> {
                            val message = event.obj("error")?.string("message").orEmpty()
                            emit(ReplyEvent.Failed(FailureKind.Api, message))
                            return@execute null
                        }

                        "message_stop" -> {
                            return@execute Round(blocks.map(::JsonObject), stopReason)
                        }
                    }
                }
                emit(ReplyEvent.Failed(FailureKind.Network, "The reply was cut off."))
                null
            }

    /** The response as an assistant message to send back; the API rejects empty text blocks. */
    private fun assistantMessage(blocks: List<JsonObject>) =
        buildJsonObject {
            put("role", "assistant")
            putJsonArray("content") {
                blocks
                    .filterNot { it.string("type") == "text" && it.string("text").isNullOrEmpty() }
                    .forEach { block -> add(JsonObject(block.filterValues { it != JsonNull })) }
            }
        }

    /** Runs every app tool Claude called in [blocks] and returns their results as one message. */
    private suspend fun toolResults(
        blocks: List<JsonObject>,
        player: PlayerInput,
    ): JsonObject {
        val results =
            blocks.filter { it.string("type") == "tool_use" }.map { use ->
                val input = use.obj("input") ?: JsonObject(emptyMap())
                val (content, isError) = runTool(use.string("name"), input, player)
                buildJsonObject {
                    put("type", "tool_result")
                    put("tool_use_id", use.string("id"))
                    put("content", content)
                    if (isError) put("is_error", true)
                }
            }
        return buildJsonObject {
            put("role", "user")
            put("content", JsonArray(results))
        }
    }

    private suspend fun runTool(
        name: String?,
        input: JsonObject,
        player: PlayerInput,
    ): Pair<String, Boolean> =
        when (name) {
            MessagesRequest.SET_RUN_CHARACTER -> {
                val character = input.string("name")?.trim()
                if (character.isNullOrEmpty()) {
                    "name is required." to true
                } else {
                    run.setCharacter(character)
                    "Saved. The run's character is $character." to false
                }
            }

            MessagesRequest.ASK_RUN_CHARACTER -> {
                val candidates =
                    (input["candidates"] as? JsonArray)
                        ?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull?.trim() }
                        ?.filter(String::isNotEmpty)
                        .orEmpty()
                val picked = player.pickCharacter(candidates, input.string("reason"))
                if (picked == null) {
                    "The player closed the picker without choosing." to false
                } else {
                    run.setCharacter(picked)
                    "The player picked $picked; it is saved as the run's character." to false
                }
            }

            else -> {
                "Unknown tool: $name" to true
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

    private fun httpFailure(
        status: HttpStatusCode,
        body: String,
    ): ReplyEvent.Failed {
        val message =
            runCatching {
                json
                    .parseToJsonElement(body)
                    .jsonObject
                    .obj("error")
                    ?.string("message")
            }.getOrNull() ?: body.take(300)
        val lower = message.lowercase()
        val kind =
            when {
                status == HttpStatusCode.Unauthorized -> FailureKind.AnthropicSignIn
                "mcp" in lower && ("auth" in lower || "401" in lower) -> FailureKind.CznSignIn
                else -> FailureKind.Api
            }
        return ReplyEvent.Failed(kind, "${status.value}: $message")
    }

    /** "get_card: Blade of the Cycle": the tool name and its first text argument, if any. */
    private fun toolLabel(block: Map<String, JsonElement>): String {
        val name = (block["name"] as? JsonPrimitive)?.content.orEmpty()
        val first =
            (block["input"] as? JsonObject)?.values?.firstNotNullOfOrNull { value ->
                (value as? JsonPrimitive)?.takeIf { it.isString }?.content
            }
        return if (first == null) name else "$name: $first"
    }

    private fun JsonObject.string(key: String) = (this[key] as? JsonPrimitive)?.contentOrNull

    private fun JsonObject.obj(key: String) = this[key] as? JsonObject

    private companion object {
        const val MESSAGES_URL = "https://api.anthropic.com/v1/messages"

        /** Requests per reply; a reply that needs more is assumed stuck in a tool loop. */
        const val MAX_ROUNDS = 8
    }
}
