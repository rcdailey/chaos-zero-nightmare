package app.dailey.czn.companion.claude

import app.dailey.czn.companion.ask.Answer
import app.dailey.czn.companion.ask.AnsweredAsk
import app.dailey.czn.companion.ask.Option
import app.dailey.czn.companion.ask.Question
import app.dailey.czn.companion.run.DeckCard
import app.dailey.czn.companion.run.DeckGear
import app.dailey.czn.companion.run.DeckParts
import app.dailey.czn.companion.run.EntryKind
import app.dailey.czn.companion.run.RunDeck
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
        jpegs: List<ByteArray> = listOf(byteArrayOf(1, 2, 3)),
        text: String = "Why B?",
        run: RunState = RunState(),
        turn: List<BetaMessageParam> = emptyList(),
        history: List<RunEntry> = this.history,
        previousMessageId: String? = null,
    ) = wire(
        MessagesRequest.build(
            history,
            text,
            jpegs,
            "https://czn.example/mcp",
            "czn-token",
            run,
            turn,
            previousMessageId,
        ),
    )

    private fun JsonObject.messages() = this["messages"]!!.jsonArray.map { it.jsonObject }

    /** The player's text in the newest message: the block before the run state. */
    private fun JsonObject.question() = content().dropLast(1).last().text()

    @Test
    fun `a reply saved before exchanges were kept reaches Claude as text with its names line`() {
        val line = "Characters on screen, top to bottom, left to right: Mika"
        val saved =
            listOf(
                RunEntry(EntryKind.User, "Who?", screen = line),
                RunEntry(EntryKind.Assistant, "Mika."),
            )
        val messages = build(history = saved).messages()
        assertEquals(listOf("Who?", line), messages[0].content().map { it.text() })
        assertEquals("Mika.", messages[1].content().single().text())
    }

    /** A saved reply's exchange: Claude thought, looked a card up, then answered. */
    private fun exchange(): String {
        fun block(json: String) = jsonMapper().readValue(json, BetaContentBlockParam::class.java)
        val messages =
            listOf(
                BetaMessageParam.ofAssistantOfContentBlockParams(
                    listOf(
                        block("""{"type":"thinking","thinking":"hm","signature":"sig"}"""),
                        block(
                            """{"type":"mcp_tool_use","id":"m1","name":"get_card",""" +
                                """"server_name":"czn","input":{"name":"Blade"}}""",
                        ),
                        block(
                            """{"type":"mcp_tool_result","tool_use_id":"m1",""" +
                                """"content":[{"type":"text","text":"Blade: 360% Damage"}]}""",
                        ),
                        block("""{"type":"text","text":"Take Blade."}"""),
                    ),
                ),
            )
        return jsonMapper().writeValueAsString(messages)
    }

    @Test
    fun `later turns see earlier tool calls and results, without thinking`() {
        val saved =
            listOf(
                RunEntry(EntryKind.User, "Which?", screen = "Characters on screen: Mika"),
                RunEntry(EntryKind.Assistant, "Take Blade.", exchange = exchange()),
            )
        val messages = build(history = saved).messages()

        // The names line came from a tool call the exchange already holds.
        assertEquals(listOf("Which?"), messages[0].content().map { it.text() })
        val reply = messages[1].content()
        assertEquals(
            listOf("mcp_tool_use", "mcp_tool_result", "text"),
            reply.map { it.string("type") },
        )
        assertEquals(
            "Blade: 360% Damage",
            reply[1]["content"]!!
                .jsonArray[0]
                .jsonObject
                .text(),
        )
    }

    /** The request body as the SDK sends it. */
    private fun wire(params: MessageCreateParams) =
        Json.parseToJsonElement(jsonMapper().writeValueAsString(params._body())).jsonObject

    @Test
    fun `a screen read without text sends the default question, never empty text`() {
        val messages = build(text = "").messages()
        assertEquals(
            MessagesRequest.SCREEN_ONLY,
            messages
                .first()
                .content()
                .single()
                .text(),
        )
        assertEquals(MessagesRequest.SCREEN_ONLY, messages.last().question())
    }

    @Test
    fun `identity line is the whole first system block`() {
        val system = build()["system"]!!.jsonArray
        assertEquals(MessagesRequest.IDENTITY, system[0].jsonObject.text())
    }

    @Test
    fun `without saved deck state Claude reads that the run's state is not known yet`() {
        val unknown =
            build(run = RunState())
                .messages()
                .last()
                .content()
                .last()
                .text()
                .orEmpty()
        assertTrue("not known" in unknown && "none" in unknown)
    }

    @Test
    fun `the saved deck state is the newest message's last block and nowhere earlier`() {
        val deck =
            RunDeck(
                limit = 120,
                points = 40,
                target =
                    DeckParts(
                        cards =
                            listOf(
                                DeckCard("Radiant Sword Flow", "II", divine = true, copies = 3),
                            ),
                    ),
                current =
                    DeckParts(
                        cards = listOf(DeckCard("Radiant Sword Flow", "III", divine = true)),
                        removed = listOf("Sword Slash"),
                        gear = DeckGear(weapon = "Criticism of Discord"),
                    ),
                dropFirst = listOf("Sword Resonance V Divine"),
            )
        val messages = build(run = RunState("Rossweisse", deck)).messages()
        val state =
            messages
                .last()
                .content()
                .last()
                .text()
                .orEmpty()
        for (part in listOf(
            "Rossweisse",
            "40/120 points",
            "Radiant Sword Flow III (Divine)",
            "Sword Slash",
            "weapon Criticism of Discord",
            "Radiant Sword Flow II (Divine) x3",
            "Sword Resonance V Divine",
            "does not match: Radiant Sword Flow III (Divine) (target Epiphany II, Divine)",
        )) {
            assertTrue(part in state, part)
        }
        val earlier = messages.dropLast(1).flatMap { it.content() }.mapNotNull { it.text() }
        assertTrue(earlier.none { "40/120" in it })
    }

    private fun toolExchange(): Pair<BetaMessageParam, BetaMessageParam> {
        val toolUse =
            BetaMessageParam.ofAssistantOfContentBlockParams(
                listOf(
                    BetaContentBlockParam.ofToolUse(
                        BetaToolUseBlockParam
                            .builder()
                            .id("t1")
                            .name(MessagesRequest.ASK_PLAYER)
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
        return toolUse to result
    }

    @Test
    fun `tool exchanges of this turn follow the newest message`() {
        val (toolUse, result) = toolExchange()
        val messages =
            build(jpegs = emptyList(), turn = listOf(toolUse, result))["messages"]!!
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
    fun `history has no images and the newest message labels each screenshot in order`() {
        val messages = build(jpegs = listOf(byteArrayOf(1), byteArrayOf(2))).messages()
        assertEquals(listOf("user", "assistant", "user"), messages.map { it.string("role") })
        val imageCounts = messages.map { m -> m.content().count { it.string("type") == "image" } }
        assertEquals(listOf(0, 0, 2), imageCounts)
        val newest = messages.last().content()
        assertEquals(
            listOf("Screenshot 1:", "image", "Screenshot 2:", "image"),
            newest.take(4).map { it.text() ?: it.string("type") },
        )
        val images = newest.filter { it.string("type") == "image" }
        assertEquals(
            listOf("AQ==", "Ag=="),
            images.map { it["source"]!!.jsonObject.string("data") },
        )
        assertEquals("Why B?", messages.last().question())
    }

    @Test
    fun `cache breakpoints mark the app prompt, the last two turns, and the request's end`() {
        val turns =
            (1..3).flatMap {
                listOf(
                    RunEntry(EntryKind.User, "Q$it"),
                    RunEntry(
                        EntryKind.Assistant,
                        "A$it",
                        exchange =
                            if (it ==
                                3
                            ) {
                                exchange()
                            } else {
                                ""
                            },
                    ),
                )
            }
        val (toolUse, result) = toolExchange()
        val body = build(history = turns, turn = listOf(toolUse, result))

        fun marked(blocks: List<JsonObject>) = blocks.map { it["cache_control"] != null }
        val system = body["system"]!!.jsonArray.map { it.jsonObject }
        assertEquals(listOf(false, true), marked(system))
        val messages = body.messages()
        val markedBlocks =
            messages.flatMapIndexed { at, message ->
                message.content().mapIndexedNotNull { block, it ->
                    (at to block).takeIf { _ -> it["cache_control"] != null }
                }
            }
        // Turn 2's reply, turn 3's last block (its exchange's text), and this turn's result.
        assertEquals(listOf(3 to 0, 5 to 2, messages.lastIndex to 0), markedBlocks)
    }

    @Test
    fun `a response that ended on thinking is sent back without that thinking`() {
        fun block(json: String) = jsonMapper().readValue(json, BetaContentBlockParam::class.java)
        val (_, result) = toolExchange()
        val response =
            BetaMessageParam.ofAssistantOfContentBlockParams(
                listOf(
                    block("""{"type":"thinking","thinking":"a","signature":"s1"}"""),
                    toolExchange()
                        .first
                        .content()
                        .get()
                        .asContentBlockParams()
                        .single(),
                    block("""{"type":"thinking","thinking":"b","signature":"s2"}"""),
                ),
            )
        val sent = build(turn = listOf(response, result)).messages()
        val types = sent[sent.lastIndex - 1].content().map { it.string("type") }
        assertEquals(listOf("thinking", "tool_use"), types)
    }

    @Test
    fun `each request names the run's newest response for cache diagnostics`() {
        val first = build(previousMessageId = null)["diagnostics"]!!.jsonObject
        assertTrue(first.containsKey("previous_message_id"))
        val later = build(previousMessageId = "msg_1")["diagnostics"]!!.jsonObject
        assertEquals("msg_1", later.string("previous_message_id"))
    }

    @Test
    fun `clearing old tool results never clears deck plans`() {
        val edit =
            build()["context_management"]!!
                .jsonObject["edits"]!!
                .jsonArray
                .single()
                .jsonObject
        val kept = edit["exclude_tools"]!!.jsonArray.map { it.jsonPrimitive.content }
        assertTrue("character_guide" in kept)
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
                emptyList(),
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
    fun `Claude has the three app tools`() {
        val names = build()["tools"]!!.jsonArray.mapNotNull { it.jsonObject.string("name") }
        assertEquals(
            setOf(
                MessagesRequest.UPDATE_RUN_STATE,
                MessagesRequest.ASK_PLAYER,
                MessagesRequest.IDENTIFY_SCREEN,
            ),
            names.toSet(),
        )
        assertEquals(3, names.size)
    }

    @Test
    fun `czn server is attached with its token`() {
        val body = build(jpegs = emptyList())
        val server = body["mcp_servers"]!!.jsonArray[0].jsonObject
        assertEquals("czn-token", server.string("authorization_token"))
        assertEquals(
            server.string("name"),
            body["tools"]!!.jsonArray[0].jsonObject.string("mcp_server_name"),
        )
        // Without a screenshot, the newest message is the question and the run state.
        assertEquals(
            2,
            body
                .messages()
                .last()
                .content()
                .size,
        )
    }

    private fun JsonObject.string(key: String) = this[key]?.jsonPrimitive?.content

    private fun JsonObject.text() = string("text")

    private fun JsonObject.content() = this["content"]!!.jsonArray.map { it.jsonObject }
}
