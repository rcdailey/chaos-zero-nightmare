package app.dailey.czn.companion.claude

import app.dailey.czn.companion.run.EntryKind
import app.dailey.czn.companion.run.RunEntry
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
    ) = MessagesRequest.build(history, text, jpeg, "https://czn.example/mcp", "czn-token")

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
            MessagesRequest
                .build(history, "", null, "u", "t", character)["system"]!!
                .jsonArray
                .joinToString { it.jsonObject.text().orEmpty() }
        assertTrue("Mika" in system("Mika"))
        assertTrue("not known" in system(null))
    }

    @Test
    fun `tool exchanges of this turn follow the newest message and end on a cache breakpoint`() {
        val toolUse =
            Json
                .parseToJsonElement(
                    """{"role":"assistant","content":[{"type":"tool_use","id":"t1",""" +
                        """"name":"ask_run_character","input":{}}]}""",
                ).jsonObject
        val result =
            Json
                .parseToJsonElement(
                    """{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1",""" +
                        """"content":"Mika"}]}""",
                ).jsonObject
        val messages =
            MessagesRequest
                .build(
                    history,
                    "Why B?",
                    null,
                    "u",
                    "t",
                    turn = listOf(toolUse, result),
                )["messages"]!!
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
        assertNotNull(messages.last().content().single()["cache_control"])
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
