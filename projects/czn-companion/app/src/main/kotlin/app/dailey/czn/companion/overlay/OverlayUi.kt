package app.dailey.czn.companion.overlay

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.MutableTransitionState
import androidx.compose.animation.slideInHorizontally
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import app.dailey.czn.companion.run.EntryKind
import app.dailey.czn.companion.run.RunEntry
import com.mikepenz.markdown.m3.Markdown
import com.mikepenz.markdown.model.rememberMarkdownState

/** The floating button: tap toggles the panel, long press reads the screen, drag moves it. */
@Composable
fun FloatingButton(
    onTap: () -> Unit,
    onLongPress: () -> Unit,
    onDrag: (Offset) -> Unit,
) {
    Surface(
        shape = CircleShape,
        color = MaterialTheme.colorScheme.primary.copy(alpha = 0.85f),
        shadowElevation = 6.dp,
        modifier =
            Modifier
                .size(56.dp)
                .pointerInput(Unit) {
                    detectTapGestures(onTap = { onTap() }, onLongPress = { onLongPress() })
                }.pointerInput(Unit) {
                    detectDragGestures {
                        change,
                        amount,
                        ->
                        change.consume()
                        onDrag(amount)
                    }
                },
    ) {
        Box(contentAlignment = Alignment.Center) {
            Text(
                "CZN",
                color = MaterialTheme.colorScheme.onPrimary,
                style = MaterialTheme.typography.labelLarge,
            )
        }
    }
}

/** Panel callbacks; [onSend] reads the screen, with the typed text or an empty string. */
class PanelActions(
    val onClose: () -> Unit = {},
    val onHideButton: () -> Unit = {},
    val onNewRun: () -> Unit = {},
    val onConfirmNewRun: () -> Unit = {},
    val onCancelNewRun: () -> Unit = {},
    val onCompose: () -> Unit = {},
    val onSend: (String) -> Unit = {},
)

/** The side panel; slides in from the screen edge when its window is added. */
@Composable
fun Panel(
    state: PanelState,
    actions: PanelActions,
) {
    val visible = remember { MutableTransitionState(false).apply { targetState = true } }
    AnimatedVisibility(visibleState = visible, enter = slideInHorizontally { it }) {
        Surface(
            color = MaterialTheme.colorScheme.surfaceContainer,
            shadowElevation = 8.dp,
            modifier = Modifier.fillMaxSize(),
        ) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "CZN Companion",
                        style = MaterialTheme.typography.titleMedium,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(
                        onClick = actions.onNewRun,
                        enabled = !state.busy,
                    ) { Text("New run") }
                    TextButton(onClick = actions.onHideButton) { Text("Hide button") }
                    TextButton(onClick = actions.onClose) { Text("Close") }
                }
                if (state.confirmingNewRun) {
                    Text("Start a new run? This clears the conversation.")
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = actions.onConfirmNewRun) { Text("New run") }
                        OutlinedButton(onClick = actions.onCancelNewRun) { Text("Keep this run") }
                    }
                }
                Conversation(state, Modifier.weight(1f))
                state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                AskRow(state, actions)
            }
        }
    }
}

@Composable
private fun Conversation(
    state: PanelState,
    modifier: Modifier,
) {
    val listState = rememberLazyListState()
    // A new question jumps to the newest message even if the user had scrolled back.
    LaunchedEffect(state.pendingQuestion) {
        if (state.pendingQuestion != null) listState.scrollToItem(0)
    }
    // Reversed layout anchors the list at its bottom, so a growing reply stays in view while
    // the user is at the end, and stays put when they have scrolled back. Items go newest first.
    LazyColumn(
        modifier,
        listState,
        reverseLayout = true,
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        state.pendingQuestion?.let { question ->
            item(key = "pending-reply") {
                Reply(state.streaming, state.pendingTools, complete = false)
            }
            item(key = "pending-question") { Question(question) }
        }
        items(state.entries.asReversed()) { entry ->
            if (entry.kind == EntryKind.User) {
                Question(entry.text)
            } else {
                Reply(entry.text, entry.tools, complete = true)
            }
        }
    }
}

/**
 * A reply: the CZN data lookups behind it, its collapsed seen block, then the advice; a spinner
 * while no advice is shown yet.
 */
@Composable
private fun Reply(
    text: String,
    tools: List<String>,
    complete: Boolean,
) {
    val parts = ReplyParts.parse(text, complete)
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (tools.isNotEmpty()) ToolCalls(tools)
        parts.seen?.let { SeenBlock(it) }
        if (parts.advice.isNotEmpty()) {
            SteadyMarkdown(parts.advice)
        } else if (!complete) {
            Thinking()
        }
    }
}

/**
 * Markdown that keeps showing the previous render while new text parses. Without this, each
 * streamed piece briefly empties the view, and the list jumps up and down.
 */
@Composable
private fun SteadyMarkdown(
    text: String,
    modifier: Modifier = Modifier,
) = Markdown(rememberMarkdownState(text, retainState = true), modifier = modifier)

@Composable
private fun ToolCalls(tools: List<String>) =
    Column {
        tools.forEach { label ->
            Text(
                "Looked up $label",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }

@Composable
private fun SeenBlock(text: String) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    Surface(
        onClick = { expanded = !expanded },
        shape = MaterialTheme.shapes.small,
        color = MaterialTheme.colorScheme.surfaceContainerHighest,
    ) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            Text(
                if (expanded) "Seen on screen  \u25B4" else "Seen on screen  \u25BE",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            AnimatedVisibility(expanded) {
                SteadyMarkdown(text, Modifier.padding(top = 4.dp))
            }
        }
    }
}

@Composable
private fun Thinking() =
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
        Text("Reading the screen...", color = MaterialTheme.colorScheme.onSurfaceVariant)
    }

/** A typed question, or a divider for a screen read sent without text. */
@Composable
private fun Question(text: String) {
    if (text.isBlank()) {
        HorizontalDivider(Modifier.padding(vertical = 4.dp))
        return
    }
    Text(
        text,
        style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.primary,
    )
}

/**
 * The one way to ask: every send includes a new screenshot, with the typed text if any. The
 * panel window is not focusable until the text box is tapped, so a tap first goes to [onCompose].
 */
@Composable
private fun AskRow(
    state: PanelState,
    actions: PanelActions,
) {
    var text by rememberSaveable { mutableStateOf("") }
    val focus = remember { FocusRequester() }
    LaunchedEffect(state.composing) { if (state.composing) focus.requestFocus() }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.weight(1f)) {
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                placeholder = { Text("Optional question") },
                modifier = Modifier.fillMaxWidth().focusRequester(focus),
            )
            if (!state.composing) {
                Box(Modifier.matchParentSize().clickable(onClick = actions.onCompose))
            }
        }
        Button(
            onClick = {
                actions.onSend(text.trim())
                text = ""
            },
            enabled = !state.busy,
            modifier = Modifier.padding(start = 8.dp),
        ) { Text(if (text.isBlank()) "Read screen" else "Send") }
    }
}

@Preview(widthDp = 420, heightDp = 600)
@Composable
private fun PanelPreview() =
    MaterialTheme {
        Panel(
            PanelState(
                entries =
                    listOf(
                        RunEntry(EntryKind.User, ""),
                        RunEntry(
                            EntryKind.Assistant,
                            "<seen>Epiphany offer, 3 cards.</seen>\n\nPick **Blade of the Cycle**.",
                        ),
                    ),
                pendingQuestion = "Why not Radiant Sword Flow?",
                pendingTools = listOf("get_card: Radiant Sword Flow", "find_teams: Rossweisse"),
                streaming = "<seen>Same offer.</seen>\n\nRadiant Sword Flow needs more Justice",
                busy = true,
            ),
            PanelActions(),
        )
    }

@Preview
@Composable
private fun FloatingButtonPreview() =
    MaterialTheme {
        FloatingButton(onTap = {}, onLongPress = {}, onDrag = {})
    }
