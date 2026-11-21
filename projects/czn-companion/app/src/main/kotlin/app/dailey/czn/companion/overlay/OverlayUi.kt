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
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
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
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.onLongClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.tooling.preview.PreviewFontScale
import androidx.compose.ui.tooling.preview.PreviewLightDark
import androidx.compose.ui.unit.dp
import app.dailey.czn.companion.CompanionTheme
import app.dailey.czn.companion.R
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
        shape = MaterialTheme.shapes.large,
        color = MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.85f),
        contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
        shadowElevation = 6.dp,
        modifier =
            Modifier
                .size(56.dp)
                .semantics {
                    role = Role.Button
                    contentDescription = "CZN Companion"
                    onClick("Show or hide the panel") {
                        onTap()
                        true
                    }
                    onLongClick("Read the screen") {
                        onLongPress()
                        true
                    }
                }.pointerInput(Unit) {
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
            Icon(painterResource(R.drawable.ic_auto_awesome), contentDescription = null)
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
    draft: TextFieldState,
    actions: PanelActions,
) {
    val visible = remember { MutableTransitionState(false).apply { targetState = true } }
    AnimatedVisibility(visibleState = visible, enter = slideInHorizontally { it }) {
        Surface(
            color = MaterialTheme.colorScheme.surfaceContainer,
            shadowElevation = 8.dp,
            modifier = Modifier.fillMaxSize(),
        ) {
            Column {
                PanelTopBar(state, actions)
                PanelBody(state, draft, actions)
            }
        }
    }
}

@Composable
private fun PanelTopBar(
    state: PanelState,
    actions: PanelActions,
) = TopAppBar(
    title = { Text("CZN Companion", maxLines = 1, overflow = TextOverflow.Ellipsis) },
    actions = {
        IconButton(onClick = actions.onNewRun, enabled = !state.busy) {
            Icon(painterResource(R.drawable.ic_add), contentDescription = "New run")
        }
        IconButton(onClick = actions.onHideButton) {
            Icon(
                painterResource(R.drawable.ic_visibility_off),
                contentDescription = "Hide floating button",
            )
        }
        IconButton(onClick = actions.onClose) {
            Icon(painterResource(R.drawable.ic_close), contentDescription = "Close panel")
        }
    },
    windowInsets = WindowInsets(0),
    colors =
        TopAppBarDefaults.topAppBarColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainer,
        ),
)

@Composable
private fun PanelBody(
    state: PanelState,
    draft: TextFieldState,
    actions: PanelActions,
) = Column(
    Modifier.padding(start = 16.dp, end = 16.dp, bottom = 16.dp),
    verticalArrangement = Arrangement.spacedBy(8.dp),
) {
    if (state.confirmingNewRun) {
        Text("Start a new run? This clears the conversation.")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = actions.onConfirmNewRun) { Text("New run") }
            OutlinedButton(onClick = actions.onCancelNewRun) { Text("Keep this run") }
        }
    }
    Conversation(state, Modifier.weight(1f))
    state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    AskRow(state, draft, actions)
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
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text(
                    "Seen on screen",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Icon(
                    painterResource(
                        if (expanded) R.drawable.ic_expand_less else R.drawable.ic_expand_more,
                    ),
                    contentDescription = if (expanded) "Collapse" else "Expand",
                    tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.size(18.dp),
                )
            }
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
    draft: TextFieldState,
    actions: PanelActions,
) {
    val focus = remember { FocusRequester() }
    LaunchedEffect(state.composing) { if (state.composing) focus.requestFocus() }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.weight(1f)) {
            OutlinedTextField(
                state = draft,
                placeholder = { Text("Optional question") },
                modifier = Modifier.fillMaxWidth().focusRequester(focus),
            )
            if (!state.composing) {
                Box(Modifier.matchParentSize().clickable(onClick = actions.onCompose))
            }
        }
        Button(
            onClick = { actions.onSend(draft.text.trim().toString()) },
            enabled = !state.busy,
            modifier = Modifier.padding(start = 8.dp),
        ) { Text(if (draft.text.isBlank()) "Read screen" else "Send") }
    }
}

@PreviewLightDark
@PreviewFontScale
@Composable
private fun PanelPreview() =
    CompanionTheme {
        Box(Modifier.size(420.dp, 600.dp)) { PanelPreviewContent() }
    }

@Composable
private fun PanelPreviewContent() =
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
        TextFieldState(),
        PanelActions(),
    )

@PreviewLightDark
@Composable
private fun FloatingButtonPreview() =
    CompanionTheme {
        FloatingButton(onTap = {}, onLongPress = {}, onDrag = {})
    }
