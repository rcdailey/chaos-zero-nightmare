package app.dailey.czn.companion.overlay

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.tooling.preview.PreviewFontScale
import androidx.compose.ui.tooling.preview.PreviewLightDark
import androidx.compose.ui.unit.dp
import app.dailey.czn.companion.CompanionTheme
import app.dailey.czn.companion.R
import app.dailey.czn.companion.characters.RosterCharacter
import coil3.compose.AsyncImage

class PickerActions(
    val onPick: (String) -> Unit = {},
    val onClose: () -> Unit = {},
    val onStop: () -> Unit = {},
    val onRetry: () -> Unit = {},
)

/**
 * Full-screen grid of character cards over the game, for the whole roster. Close tells a waiting
 * reply that the player chose nobody; Stop, shown only while a reply waits, stops that reply.
 */
@Composable
fun CharacterPicker(
    state: PickerState,
    actions: PickerActions,
) = Surface(color = MaterialTheme.colorScheme.surface, modifier = Modifier.fillMaxSize()) {
    Column(Modifier.windowInsetsPadding(WindowInsets.safeDrawing)) {
        TopAppBar(
            title = {
                Text(PICKER_TITLE, maxLines = 1, overflow = TextOverflow.Ellipsis)
            },
            actions = {
                if (state.forReply) {
                    IconButton(onClick = actions.onStop) {
                        Icon(painterResource(R.drawable.ic_stop), contentDescription = "Stop reply")
                    }
                }
                IconButton(onClick = actions.onClose) {
                    Icon(painterResource(R.drawable.ic_close), contentDescription = "Close")
                }
            },
            windowInsets = WindowInsets(0),
            colors =
                TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                ),
        )
        val options = state.options
        val roster = state.roster
        when {
            options.isNotEmpty() -> CharacterGrid(options, actions.onPick)
            roster is RosterState.Failed -> RosterError(roster.message, actions.onRetry)
            else -> Loading()
        }
    }
}

@Composable
private fun CharacterGrid(
    characters: List<RosterCharacter>,
    onPick: (String) -> Unit,
) = LazyVerticalGrid(
    columns = GridCells.Adaptive(minSize = 128.dp),
    contentPadding = PaddingValues(16.dp),
    horizontalArrangement = Arrangement.spacedBy(12.dp),
    verticalArrangement = Arrangement.spacedBy(12.dp),
) {
    items(characters, key = { it.name }) { character -> CharacterCard(character, onPick) }
}

/**
 * Claude's question with a few named characters, shown in the conversation under its reply: one
 * row that scrolls sideways when it overflows. The panel's Stop button stops the waiting reply.
 */
@Composable
fun InlineCharacterChoice(
    state: PickerState,
    actions: PickerActions,
) = Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
    Text(PICKER_TITLE, style = MaterialTheme.typography.titleSmall)
    LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        items(state.options, key = { it.name }) { character ->
            CharacterCard(character, actions.onPick, Modifier.width(INLINE_CARD_WIDTH))
        }
    }
    TextButton(onClick = actions.onClose) { Text("Skip") }
}

@Composable
private fun CharacterCard(
    character: RosterCharacter,
    onPick: (String) -> Unit,
    modifier: Modifier = Modifier,
) = Card(onClick = { onPick(character.name) }, modifier = modifier) {
    AsyncImage(
        model = character.imageUrl,
        contentDescription = null,
        contentScale = ContentScale.Crop,
        modifier = Modifier.fillMaxWidth().aspectRatio(CARD_ASPECT_RATIO).vignette(),
    )
    Text(
        character.name,
        style = MaterialTheme.typography.titleSmall,
        textAlign = TextAlign.Center,
        modifier = Modifier.fillMaxWidth().padding(8.dp),
    )
}

/**
 * Fades the portrait's left, right, and top edges to transparent so it blends into the card; the
 * bottom edge stays hard against the name bar. The colors are only an alpha mask: opaque keeps
 * the art, clear hides it.
 */
private fun Modifier.vignette() =
    graphicsLayer(compositingStrategy = CompositingStrategy.Offscreen).drawWithContent {
        drawContent()
        drawRect(
            Brush.horizontalGradient(
                0f to Color.Transparent,
                0.12f to Color.Black,
                0.88f to Color.Black,
                1f to Color.Transparent,
            ),
            blendMode = BlendMode.DstIn,
        )
        drawRect(
            Brush.verticalGradient(
                0f to Color.Transparent,
                0.1f to Color.Black,
            ),
            blendMode = BlendMode.DstIn,
        )
    }

private val INLINE_CARD_WIDTH = 112.dp

private const val PICKER_TITLE = "Pick the run's character"

@Composable
private fun Loading() =
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        CircularProgressIndicator(Modifier.size(32.dp))
    }

@Composable
private fun RosterError(
    message: String,
    onRetry: () -> Unit,
) = Column(
    Modifier.fillMaxSize().padding(24.dp),
    verticalArrangement = Arrangement.spacedBy(12.dp, Alignment.CenterVertically),
    horizontalAlignment = Alignment.CenterHorizontally,
) {
    Text("Could not load the characters: $message", color = MaterialTheme.colorScheme.error)
    Button(onClick = onRetry) { Text("Try again") }
}

/** Width to height of Prydwen's character card art (262 by 358 pixels). */
private const val CARD_ASPECT_RATIO = 262f / 358f

@PreviewLightDark
@PreviewFontScale
@Composable
private fun CharacterPickerPreview() =
    CompanionTheme(dark = true) {
        Box(Modifier.size(900.dp, 600.dp)) {
            CharacterPicker(
                PickerState(
                    forReply = true,
                    roster =
                        RosterState.Loaded(
                            listOf("Adelheid", "Mei Lin", "Mika", "Tiphera", "Veronica")
                                .map(::RosterCharacter),
                        ),
                ),
                PickerActions(),
            )
        }
    }
