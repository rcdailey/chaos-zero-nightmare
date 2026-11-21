package app.dailey.czn.companion.characters

import app.dailey.czn.companion.auth.CznDataAuth
import io.ktor.client.HttpClient
import io.ktor.client.plugins.sse.SSE
import io.ktor.client.request.bearerAuth
import io.modelcontextprotocol.kotlin.sdk.client.mcpStreamableHttp
import io.modelcontextprotocol.kotlin.sdk.types.TextContent
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.decodeFromJsonElement

@Serializable
data class RosterCharacter(
    val name: String,
    @SerialName("image_url") val imageUrl: String? = null,
)

/**
 * Every playable character, from the CZN data server's `list_characters` tool. The card art is
 * a Prydwen CDN URL; the app hosts no images. Fetched once per process, again after a failure.
 */
class CharacterRoster(
    http: HttpClient,
    private val cznData: CznDataAuth,
) {
    private val mcpHttp = http.config { install(SSE) }
    private val json = Json { ignoreUnknownKeys = true }
    private val lock = Mutex()
    private var cached: List<RosterCharacter>? = null

    suspend fun characters(): List<RosterCharacter> =
        lock.withLock {
            cached ?: fetch().also { cached = it }
        }

    private suspend fun fetch(): List<RosterCharacter> {
        val token = cznData.accessToken()
        val client = mcpHttp.mcpStreamableHttp(CznDataAuth.MCP_URL) { bearerAuth(token) }
        try {
            val result = client.callTool("list_characters", emptyMap())
            check(result.isError != true) { "The CZN data server refused the character list." }
            val rows: JsonElement =
                result.structuredContent?.get("result")
                    ?: json.parseToJsonElement(
                        result.content.filterIsInstance<TextContent>().joinToString("") { it.text },
                    )
            return json.decodeFromJsonElement(rows)
        } finally {
            client.close()
        }
    }
}
