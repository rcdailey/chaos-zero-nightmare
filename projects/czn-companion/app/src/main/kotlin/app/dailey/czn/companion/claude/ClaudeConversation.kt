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
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
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
 * Asks Claude about the current screen with the active run as context, streaming the reply. An
 * exchange is saved to the run only after the reply completes; a failed or cancelled request
 * leaves the run unchanged.
 */
class ClaudeConversation(
    private val http: HttpClient,
    private val anthropic: AnthropicAuth,
    private val cznData: CznDataAuth,
    private val run: RunRepository,
) {
    private val json = Json { ignoreUnknownKeys = true }

    val entries get() = run.entries

    suspend fun newRun() = run.newRun()

    fun ask(
        text: String,
        jpeg: ByteArray?,
    ): Flow<ReplyEvent> =
        flow {
            val anthropicToken =
                tokenOrNull(FailureKind.AnthropicSignIn) { anthropic.accessToken() } ?: return@flow
            val cznToken =
                tokenOrNull(FailureKind.CznSignIn) { cznData.accessToken() } ?: return@flow
            val history = run.entriesNow()
            val body = MessagesRequest.build(history, text, jpeg, CznDataAuth.MCP_URL, cznToken)
            val reply = StringBuilder()
            val tools = mutableListOf<String>()
            try {
                http
                    .preparePost(MESSAGES_URL) {
                        header(HttpHeaders.Authorization, "Bearer $anthropicToken")
                        header("anthropic-version", "2023-06-01")
                        header("anthropic-beta", MessagesRequest.BETAS.joinToString(","))
                        header(HttpHeaders.UserAgent, "claude-cli/2.1.322 (external, cli)")
                        header("x-app", "cli")
                        contentType(ContentType.Application.Json)
                        setBody(body.toString())
                    }.execute { response ->
                        if (response.status != HttpStatusCode.OK) {
                            emit(httpFailure(response.status, response.bodyAsText()))
                            return@execute
                        }
                        val channel = response.bodyAsChannel()
                        var textBlock = false
                        // Name and streamed input of the tool call block in progress, if any.
                        var toolName: String? = null
                        val toolInput = StringBuilder()
                        while (true) {
                            val line = channel.readUTF8Line() ?: break
                            if (!line.startsWith("data:")) continue
                            val data = line.removePrefix("data:").trim()
                            val event = json.parseToJsonElement(data).jsonObject
                            when (event.string("type")) {
                                "content_block_start" -> {
                                    val block = event.obj("content_block")
                                    textBlock = block?.string("type") == "text"
                                    toolName =
                                        block
                                            ?.takeIf { it.string("type") == "mcp_tool_use" }
                                            ?.string("name")
                                    toolInput.clear()
                                    if (textBlock && reply.isNotEmpty()) {
                                        reply.append("\n\n")
                                        emit(ReplyEvent.TextDelta("\n\n"))
                                    }
                                }

                                "content_block_delta" -> {
                                    val delta = event.obj("delta")
                                    if (textBlock && delta?.string("type") == "text_delta") {
                                        val piece = delta.string("text").orEmpty()
                                        reply.append(piece)
                                        emit(ReplyEvent.TextDelta(piece))
                                    }
                                    if (toolName != null &&
                                        delta?.string("type") == "input_json_delta"
                                    ) {
                                        toolInput.append(delta.string("partial_json").orEmpty())
                                    }
                                }

                                "content_block_stop" -> {
                                    val name = toolName ?: continue
                                    val label = toolLabel(name, toolInput.toString())
                                    tools += label
                                    emit(ReplyEvent.ToolCall(label))
                                    toolName = null
                                }

                                "error" -> {
                                    val message = event.obj("error")?.string("message").orEmpty()
                                    emit(ReplyEvent.Failed(FailureKind.Api, message))
                                    return@execute
                                }

                                "message_stop" -> {
                                    run.appendExchange(text, reply.toString(), tools)
                                    emit(ReplyEvent.Done)
                                    return@execute
                                }
                            }
                        }
                        emit(ReplyEvent.Failed(FailureKind.Network, "The reply was cut off."))
                    }
            } catch (e: IOException) {
                emit(ReplyEvent.Failed(FailureKind.Network, e.message ?: "Network error."))
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
    private fun toolLabel(
        name: String,
        input: String,
    ): String {
        val arguments = runCatching { json.parseToJsonElement(input).jsonObject }.getOrNull()
        val first =
            arguments?.values?.firstNotNullOfOrNull { value ->
                (value as? JsonPrimitive)?.takeIf { it.isString }?.content
            }
        return if (first == null) name else "$name: $first"
    }

    private fun JsonObject.string(key: String) = this[key]?.jsonPrimitive?.content

    private fun JsonObject.obj(key: String) = this[key] as? JsonObject

    private companion object {
        const val MESSAGES_URL = "https://api.anthropic.com/v1/messages"
    }
}
