package app.dailey.czn.companion

import android.app.Application
import app.dailey.czn.companion.auth.AnthropicAuth
import app.dailey.czn.companion.auth.CznDataAuth
import app.dailey.czn.companion.auth.TokenStore
import app.dailey.czn.companion.characters.CharacterRoster
import app.dailey.czn.companion.characters.ScreenIdentifier
import app.dailey.czn.companion.claude.ClaudeConversation
import app.dailey.czn.companion.overlay.OverlaySettings
import app.dailey.czn.companion.overlay.OverlayStatus
import app.dailey.czn.companion.run.DeckDetails
import app.dailey.czn.companion.run.RunDatabase
import app.dailey.czn.companion.run.RunRepository
import app.dailey.czn.companion.run.SaveDataCost
import app.dailey.czn.companion.run.ScreenshotArchive
import io.ktor.client.HttpClient
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.serialization.kotlinx.json.json
import kotlinx.serialization.json.Json
import java.io.File

/** Owns the app's long-lived objects; every screen and service gets them from here. */
class CompanionApp : Application() {
    val http by lazy {
        HttpClient(OkHttp) {
            // Enables per-request timeouts; requests without one keep the engine defaults.
            install(HttpTimeout)
            install(ContentNegotiation) {
                json(
                    Json {
                        ignoreUnknownKeys = true
                        encodeDefaults = true
                        explicitNulls = false
                    },
                )
            }
        }
    }
    val tokenStore by lazy { TokenStore(File(filesDir, "secrets")) }
    val anthropicAuth by lazy { AnthropicAuth(http, tokenStore) }
    val cznDataAuth by lazy { CznDataAuth(http, tokenStore) }
    val overlaySettings by lazy { OverlaySettings(getSharedPreferences("overlay", MODE_PRIVATE)) }
    val overlayStatus = OverlayStatus()
    private val runDatabase by lazy {
        RunDatabase.open(this, getDatabasePath("runs.db").absolutePath)
    }
    val characterRoster by lazy { CharacterRoster(http, cznDataAuth) }
    val conversation by lazy {
        ClaudeConversation(
            anthropicAuth,
            cznDataAuth,
            RunRepository(runDatabase),
            ScreenIdentifier(http, cznDataAuth),
            SaveDataCost(http, cznDataAuth),
            DeckDetails(http, cznDataAuth),
            ScreenshotArchive(File(filesDir, "screenshots")),
        )
    }
}
