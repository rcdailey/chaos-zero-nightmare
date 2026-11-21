package app.dailey.czn.companion.claude

import app.dailey.czn.companion.ask.Answer
import app.dailey.czn.companion.ask.AnsweredAsk
import app.dailey.czn.companion.ask.Option
import app.dailey.czn.companion.ask.Question
import app.dailey.czn.companion.run.EntryKind
import app.dailey.czn.companion.run.RunEntry
import com.anthropic.core.JsonValue
import com.anthropic.core.jsonMapper
import com.anthropic.models.beta.messages.BetaContentBlockParam
import com.anthropic.models.beta.messages.BetaMessageParam
import com.anthropic.models.beta.messages.BetaToolResultBlockParam
import com.anthropic.models.beta.messages.BetaToolUseBlockParam
import com.anthropic.models.beta.messages.MessageCreateParams
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The wire contract subscription tokens and prompt caching depend on. */
class MessagesRequestTest {
    private val history =
        listOf(
            RunEntry(EntryKind.User, ""),
            RunEntry(EntryKind.Assistant, "Seen: epiphany offer. Pick B."),
        )

    private fun build(
        jpeg: ByteArray? = byteArrayOf(1, 2, 3),
        text: String = "Why B?",
        character: String? = null,
        turn: List<BetaMessageParam> = emptyList(),
    ) = wire(
        MessagesRequest.build(
            history,
            text,
            jpeg,
            "https://czn.example/mcp",
            "czn-token",
            character,
            turn,
        ),
    )

    /** The request body as the SDK sends it. */
    private fun wire(params: MessageCreateParams) =
        Json.parseToJsonElement(jsonMapper().writeValueAsString(params._body())).jsonObject

    @Test
    fun `a screen read without text sends the default question, never empty text`() {
        val messages = build(text = "")["messages"]!!.jsonArray.map { it.jsonObject }
        assertEquals(
            MessagesRequest.SCREEN_ONLY,
            messages
                .first()
                .content()
                .single()
                .text(),
        )
        assertEquals(
            MessagesRequest.SCREEN_ONLY,
            messages
                .last()
                .content()
                .last()
                .text(),
        )
    }

    @Test
    fun `identity line is the whole first system block`() {
        val system = build()["system"]!!.jsonArray
        assertEquals(MessagesRequest.IDENTITY, system[0].jsonObject.text())
    }

    @Test
    fun `the run's character reaches Claude in the system prompt`() {
        fun system(character: String?) =
            build(jpeg = null, character = character)["system"]!!
                .jsonArray
                .joinToString { it.jsonObject.text().orEmpty() }
        assertTrue("Mika" in system("Mika"))
        assertTrue("not known" in system(null))
    }

    @Test
    fun `tool exchanges of this turn follow the newest message`() {
        val toolUse =
            BetaMessageParam.ofAssistantOfContentBlockParams(
                listOf(
                    BetaContentBlockParam.ofToolUse(
                        BetaToolUseBlockParam
                            .builder()
                            .id("t1")
                            .name(MessagesRequest.ASK_RUN_CHARACTER)
                            .input(JsonValue.from(emptyMap<String, Any>()))
                            .build(),
                    ),
                ),
            )
        val result =
            BetaMessageParam.ofUserOfContentBlockParams(
                listOf(
                    BetaContentBlockParam.ofToolResult(
                        BetaToolResultBlockParam
                            .builder()
                            .toolUseId("t1")
                            .content("Mika")
                            .build(),
                    ),
                ),
            )
        val messages =
            build(jpeg = null, turn = listOf(toolUse, result))["messages"]!!
                .jsonArray
                .map { it.jsonObject }
        assertEquals(
            listOf("user", "assistant", "user", "assistant", "user"),
            messages.map { it.string("role") },
        )
        assertEquals(
            "tool_result",
            messages
                .last()
                .content()
                .single()
                .string("type"),
        )
    }

    @Test
    fun `history keeps order and only the newest message has an image`() {
        val messages = build()["messages"]!!.jsonArray.map { it.jsonObject }
        assertEquals(listOf("user", "assistant", "user"), messages.map { it.string("role") })
        val imageCounts = messages.map { m -> m.content().count { it.string("type") == "image" } }
        assertEquals(listOf(0, 0, 1), imageCounts)
        assertEquals(
            "Why B?",
            messages
                .last()
                .content()
                .last()
                .text(),
        )
    }

    @Test
    fun `cache breakpoint sits on the last history message`() {
        val messages = build()["messages"]!!.jsonArray.map { it.jsonObject }
        assertNull(messages[0].content()[0]["cache_control"])
        assertNotNull(messages[1].content()[0]["cache_control"])
        assertNull(messages[2].content().last()["cache_control"])
    }

    @Test
    fun `a saved reply shows Claude its questions and the answers where it asked them`() {
        val ask =
            AnsweredAsk(
                offset = "Two paths.".length,
                questions = listOf(Question("Which path?", "Path", listOf(Option("Elite")))),
                answers = listOf(Answer(listOf("Elite"))),
            )
        val params =
            MessagesRequest.build(
                listOf(
                    RunEntry(EntryKind.User, ""),
                    RunEntry(
                        EntryKind.Assistant,
                        "Two paths.\n\nTake the elite.",
                        asks = listOf(ask),
                    ),
                ),
                "Why?",
                null,
                "https://czn.example/mcp",
                "czn-token",
            )
        val reply = wire(params)["messages"]!!.jsonArray[1].jsonObject
        assertEquals(
            "Two paths.\n\n<asked>\nQ: Which path?\nA: Elite\n</asked>\n\nTake the elite.",
            reply.content().single().text(),
        )
    }

    @Test
    fun `Claude can ask the player questions`() {
        val names = build()["tools"]!!.jsonArray.mapNotNull { it.jsonObject.string("name") }
        assertTrue(MessagesRequest.ASK_PLAYER in names)
    }

    @Test
    fun `czn server is attached with its token`() {
        val body = build(jpeg = null)
        val server = body["mcp_servers"]!!.jsonArray[0].jsonObject
        assertEquals("czn-token", server.string("authorization_token"))
        assertEquals(
            server.string("name"),
            body["tools"]!!.jsonArray[0].jsonObject.string("mcp_server_name"),
        )
        assertEquals(
            1,
            body["messages"]!!
                .jsonArray
                .last()
                .jsonObject
                .content()
                .size,
        )
    }

    private fun JsonObject.string(key: String) = this[key]?.jsonPrimitive?.content

    private fun JsonObject.text() = string("text")

    private fun JsonObject.content() = this["content"]!!.jsonArray.map { it.jsonObject }
}
