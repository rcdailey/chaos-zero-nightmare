package app.dailey.czn.companion.characters

import app.dailey.czn.companion.auth.CznDataAuth
import io.ktor.client.HttpClient
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.get
import io.ktor.client.statement.bodyAsText
import io.ktor.http.isSuccess
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

@Serializable
data class RosterCharacter(
    val name: String,
    @SerialName("image_url") val imageUrl: String? = null,
)

@Serializable
private data class CharactersResponse(
    val characters: List<RosterCharacter>,
)

/**
 * Every playable character, from the CZN data server's `GET /characters` (its MCP tools reply
 * in text meant for Claude). The card art is a Prydwen CDN URL; the app hosts no images. Fetched
 * once per process, again after a failure.
 */
class CharacterRoster(
    private val http: HttpClient,
    private val cznData: CznDataAuth,
) {
    private val json = Json { ignoreUnknownKeys = true }
    private val lock = Mutex()
    private var cached: List<RosterCharacter>? = null

    suspend fun characters(): List<RosterCharacter> =
        lock.withLock {
            cached ?: fetch().also { cached = it }
        }

    private suspend fun fetch(): List<RosterCharacter> {
        val response = http.get(CHARACTERS_URL) { bearerAuth(cznData.accessToken()) }
        val body = response.bodyAsText()
        check(response.status.isSuccess()) {
            "The CZN data server refused the character list: ${response.status} $body"
        }
        return json.decodeFromString<CharactersResponse>(body).characters
    }

    companion object {
        /** On the same host as the MCP endpoint, which takes the same token. */
        val CHARACTERS_URL = CznDataAuth.MCP_URL.removeSuffix("/mcp") + "/characters"
    }
}
