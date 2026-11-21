package app.dailey.czn.companion

import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.viewModels
import androidx.browser.auth.AuthTabIntent
import androidx.browser.customtabs.CustomTabsClient
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialExpressiveTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.tooling.preview.PreviewFontScale
import androidx.compose.ui.tooling.preview.PreviewLightDark
import androidx.compose.ui.tooling.preview.PreviewScreenSizes
import androidx.compose.ui.unit.dp
import androidx.core.net.toUri
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import app.dailey.czn.companion.auth.AuthState
import app.dailey.czn.companion.auth.CznDataAuth

class MainActivity : ComponentActivity() {
    private val viewModel: MainViewModel by viewModels {
        viewModelFactory {
            initializer {
                val app = application as CompanionApp
                MainViewModel(
                    app.anthropicAuth,
                    app.cznDataAuth,
                    app.overlaySettings,
                    app.overlayStatus,
                )
            }
        }
    }

    private val cznDataAuthTab =
        AuthTabIntent.registerActivityResultLauncher(this) { result ->
            if (result.resultCode == AuthTabIntent.RESULT_OK) {
                viewModel.completeCznDataSignIn(result.resultUri.toString())
            } else {
                viewModel.cancelCznDataSignIn()
            }
        }

    /** The sign-in itself arrives on the local port; the tab result only reports closing. */
    private val anthropicAuthTab =
        AuthTabIntent.registerActivityResultLauncher(this) { result ->
            if (result.resultCode != AuthTabIntent.RESULT_OK) viewModel.browserClosedForAnthropic()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        handleRedirect(intent)
        setContent {
            val state by viewModel.uiState.collectAsStateWithLifecycle()
            CompanionTheme {
                MainScreen(
                    state = state,
                    onAnthropicSignIn = {
                        openInBrowser(
                            viewModel.beginAnthropicSignIn(ANTHROPIC_RETURN_URI),
                            anthropicAuthTab,
                        )
                    },
                    onAnthropicCodeSignIn = {
                        openInBrowser(viewModel.beginAnthropicCodeSignIn(), launcher = null)
                    },
                    onAnthropicCode = viewModel::completeAnthropicSignIn,
                    onAnthropicSignOut = viewModel::signOutAnthropic,
                    onCznDataSignIn = {
                        openInBrowser(viewModel.beginCznDataSignIn(), cznDataAuthTab)
                    },
                    onCznDataSignOut = viewModel::signOutCznData,
                    onShowButton = viewModel::showFloatingButton,
                    onOpenAccessibilitySettings = {
                        startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                    },
                    onOpenAppInfo = {
                        startActivity(
                            Intent(
                                Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                "package:$packageName".toUri(),
                            ),
                        )
                    },
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleRedirect(intent)
    }

    /**
     * Opens [url] in an Auth Tab that closes when the page navigates to the app's scheme and
     * reports to [launcher]. Browsers without Auth Tab, and a null [launcher], get a Custom Tab;
     * the app's scheme then reaches [handleRedirect].
     */
    private fun openInBrowser(
        url: String,
        launcher: ActivityResultLauncher<Intent>?,
    ) {
        val browser = CustomTabsClient.getPackageName(this, null)
        if (launcher != null && browser != null &&
            CustomTabsClient.isAuthTabSupported(this, browser)
        ) {
            AuthTabIntent.Builder().build().launch(
                launcher,
                url.toUri(),
                CznDataAuth.REDIRECT_SCHEME,
            )
        } else {
            CustomTabsIntent.Builder().build().launchUrl(this, url.toUri())
        }
    }

    private fun handleRedirect(intent: Intent) {
        val data = intent.data ?: return
        if (data.scheme == CznDataAuth.REDIRECT_SCHEME && data.path == CZN_DATA_PATH) {
            viewModel.completeCznDataSignIn(data.toString())
        }
        setIntent(Intent(intent).setData(null))
    }

    private companion object {
        const val CZN_DATA_PATH = "/oauth/czn"
        const val ANTHROPIC_RETURN_URI = "${CznDataAuth.REDIRECT_SCHEME}:/oauth/anthropic"
    }
}

/** @param dark the overlay passes true: it sits over the game's dark art in any system theme. */
@Composable
fun CompanionTheme(
    dark: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val context = LocalContext.current
    val colors =
        if (dark) {
            dynamicDarkColorScheme(
                context,
            )
        } else {
            dynamicLightColorScheme(context)
        }
    val extra = if (dark) DarkExtraColors else LightExtraColors
    CompositionLocalProvider(LocalExtraColors provides extra) {
        MaterialExpressiveTheme(colorScheme = colors, content = content)
    }
}

/**
 * Color roles Material 3 lacks. [recommended] is the green that marks Claude's suggested
 * option; dynamic color has no green role, so it is fixed per light and dark theme.
 */
@Immutable
data class ExtraColors(
    val recommended: Color,
)

private val DarkExtraColors = ExtraColors(recommended = Color(0xFF81C995))
private val LightExtraColors = ExtraColors(recommended = Color(0xFF188038))

private val LocalExtraColors = staticCompositionLocalOf { DarkExtraColors }

/** The app theme's extra color roles; read inside [CompanionTheme]. */
val extraColors: ExtraColors
    @Composable @ReadOnlyComposable
    get() = LocalExtraColors.current

@Composable
fun MainScreen(
    state: MainUiState,
    onAnthropicSignIn: () -> Unit,
    onAnthropicCodeSignIn: () -> Unit,
    onAnthropicCode: (String) -> Unit,
    onAnthropicSignOut: () -> Unit,
    onCznDataSignIn: () -> Unit,
    onCznDataSignOut: () -> Unit,
    onShowButton: () -> Unit,
    onOpenAccessibilitySettings: () -> Unit,
    onOpenAppInfo: () -> Unit,
) {
    Scaffold { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(24.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text("CZN Companion", style = MaterialTheme.typography.headlineMedium)
            SignInCard(
                title = "Claude subscription",
                state = state.anthropic,
                onSignIn = onAnthropicSignIn,
                onSignOut = onAnthropicSignOut,
                codeFallback = CodeFallback(onAnthropicCodeSignIn, onAnthropicCode),
            )
            SignInCard(
                title = "CZN data server",
                state = state.cznData,
                onSignIn = onCznDataSignIn,
                onSignOut = onCznDataSignOut,
            )
            Card(Modifier.fillMaxWidth().widthIn(max = 640.dp)) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text("Side tab", style = MaterialTheme.typography.titleMedium)
                    if (!state.overlayRunning) {
                        Text(
                            "Off. Turn on CZN Companion in Accessibility settings. If Android " +
                                "blocks it, open App info, then the menu at the top, then " +
                                "Allow restricted settings, and try again.",
                        )
                        Button(onClick = onOpenAccessibilitySettings) {
                            Text("Open Accessibility settings")
                        }
                        TextButton(onClick = onOpenAppInfo) { Text("Open App info") }
                    } else {
                        Text(
                            "Shown on the right edge while the game is open. Tap it or pull it " +
                                "toward the middle to open the panel. Hold it, then let go, to " +
                                "read the screen. If a pull triggers Back, hold it first. Drag " +
                                "it up or down to move it.",
                        )
                        if (!state.buttonVisible) {
                            Text("Hidden from its panel.")
                            Button(onClick = onShowButton) { Text("Show side tab") }
                        }
                    }
                }
            }
        }
    }
}

/**
 * One account's sign-in controls. [codeFallback] adds a sign-in where the user pastes a code; it is
 * null for accounts that have no such fallback.
 */
@Composable
private fun SignInCard(
    title: String,
    state: SignInUiState,
    onSignIn: () -> Unit,
    onSignOut: () -> Unit,
    codeFallback: CodeFallback? = null,
) {
    Card(Modifier.fillMaxWidth().widthIn(max = 640.dp)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(
                when (val auth = state.auth) {
                    AuthState.SignedIn -> "Signed in."
                    AuthState.SignedOut -> "Not signed in."
                    is AuthState.NeedsSignIn -> auth.reason + " Sign in again."
                },
            )
            if (state.auth == AuthState.SignedIn) {
                OutlinedButton(onClick = onSignOut) { Text("Sign out") }
            } else {
                Button(onClick = onSignIn) { Text("Sign in in browser") }
                codeFallback?.let {
                    TextButton(onClick = it.onStart) { Text("Sign in with a code instead") }
                }
            }
            if (codeFallback != null && state.awaitingCode && state.auth != AuthState.SignedIn) {
                var code by rememberSaveable { mutableStateOf("") }
                OutlinedTextField(
                    value = code,
                    onValueChange = { code = it },
                    label = { Text("Code shown after sign-in") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Button(
                    onClick = { codeFallback.onSubmit(code) },
                    enabled = !state.busy && code.isNotBlank(),
                ) {
                    Text("Finish sign-in")
                }
            }
            state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        }
    }
}

private class CodeFallback(
    val onStart: () -> Unit,
    val onSubmit: (String) -> Unit,
)

@PreviewScreenSizes
@PreviewLightDark
@PreviewFontScale
@Composable
private fun MainScreenSignedOutPreview() =
    CompanionTheme {
        MainScreen(
            state =
                MainUiState(
                    anthropic =
                        SignInUiState(
                            awaitingCode = true,
                            error = "Anthropic rejected the code.",
                        ),
                ),
            onAnthropicSignIn = {},
            onAnthropicCodeSignIn = {},
            onAnthropicCode = {},
            onAnthropicSignOut = {},
            onCznDataSignIn = {},
            onCznDataSignOut = {},
            onShowButton = {},
            onOpenAccessibilitySettings = {},
            onOpenAppInfo = {},
        )
    }

@PreviewLightDark
@PreviewFontScale
@Composable
private fun MainScreenSignedInPreview() =
    CompanionTheme {
        MainScreen(
            state = MainUiState(anthropic = SignInUiState(auth = AuthState.SignedIn)),
            onAnthropicSignIn = {},
            onAnthropicCodeSignIn = {},
            onAnthropicCode = {},
            onAnthropicSignOut = {},
            onCznDataSignIn = {},
            onCznDataSignOut = {},
            onShowButton = {},
            onOpenAccessibilitySettings = {},
            onOpenAppInfo = {},
        )
    }
