package app.dailey.czn.companion.overlay

import android.graphics.Region
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.ZeroCornerSize
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledIconButton
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
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.graphics.toAndroidRect
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.AwaitPointerEventScope
import androidx.compose.ui.input.pointer.PointerInputChange
import androidx.compose.ui.input.pointer.PointerInputScope
import androidx.compose.ui.input.pointer.changedToUp
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalHapticFeedback
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
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.roundToIntRect
import app.dailey.czn.companion.CompanionTheme
import app.dailey.czn.companion.R
import app.dailey.czn.companion.run.EntryKind
import app.dailey.czn.companion.run.RunEntry
import com.mikepenz.markdown.m3.Markdown
import com.mikepenz.markdown.model.rememberMarkdownState
import kotlin.math.abs

/**
 * The overlay window's content: the side tab with the panel to its right. Closed, the panel lies
 * past the window's right edge, so only the tab shows; opening slides both in together. The window
 * spans the tab and the open panel, so [onTouchableArea] reports where they are: touches anywhere
 * else in the window must reach the game.
 */
@Composable
fun SideOverlay(
    open: Boolean,
    tabY: Int,
    panelWidth: Dp,
    tabActions: SideTabActions,
    onTouchableArea: (Region) -> Unit,
    panel: @Composable () -> Unit,
) {
    val shift by animateDpAsState(
        if (open) 0.dp else panelWidth,
        MaterialTheme.motionScheme.defaultSpatialSpec(),
    )
    var tabBounds by remember { mutableStateOf(Rect.Zero) }
    var panelBounds by remember { mutableStateOf(Rect.Zero) }
    LaunchedEffect(tabBounds, panelBounds) {
        onTouchableArea(
            Region().apply {
                union(tabBounds.roundToIntRect().toAndroidRect())
                union(panelBounds.roundToIntRect().toAndroidRect())
            },
        )
    }
    Row(Modifier.fillMaxHeight().offset { IntOffset(shift.roundToPx(), 0) }) {
        Box(
            Modifier
                .offset { IntOffset(0, tabY) }
                .onGloballyPositioned { tabBounds = it.boundsInWindow() },
        ) { SideTab(tabActions) }
        Box(
            Modifier
                .width(panelWidth)
                .fillMaxHeight()
                .onGloballyPositioned { panelBounds = it.boundsInWindow() },
        ) { panel() }
    }
}

/** Touch area of the side tab; its visible part is narrower, against the panel or screen edge. */
val SideTabSize = DpSize(48.dp, 72.dp)

/**
 * Side tab callbacks; [onMove] takes how far, in pixels, the tab must move down to stay under the
 * finger.
 */
class SideTabActions(
    val onTap: () -> Unit = {},
    val onHold: () -> Unit = {},
    val onOpen: () -> Unit = {},
    val onClose: () -> Unit = {},
    val onMove: (Float) -> Unit = {},
)

/**
 * The tab on the screen's right edge, which rides on the panel's left edge while the panel is
 * open, so it never covers the panel. Tap toggles the panel. Hold, then let go without moving,
 * reads the screen. Dragging toward the middle of the screen opens the panel and dragging toward
 * the edge closes it; holding first keeps Android's back gesture from taking the drag. Dragging up
 * or down moves the tab.
 */
@Composable
fun SideTab(actions: SideTabActions) {
    val haptics = LocalHapticFeedback.current
    Box(
        contentAlignment = Alignment.CenterEnd,
        modifier =
            Modifier
                .size(SideTabSize)
                .semantics {
                    role = Role.Button
                    contentDescription = "CZN Companion"
                    onClick("Show or hide the panel") {
                        actions.onTap()
                        true
                    }
                    onLongClick("Read the screen") {
                        actions.onHold()
                        true
                    }
                }.pointerInput(actions) {
                    detectSideTabGestures(actions) {
                        haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                    }
                },
    ) {
        Surface(
            shape =
                MaterialTheme.shapes.large.copy(
                    topEnd = ZeroCornerSize,
                    bottomEnd = ZeroCornerSize,
                ),
            color = MaterialTheme.colorScheme.primaryContainer,
            contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
            shadowElevation = 6.dp,
            modifier = Modifier.size(width = 32.dp, height = SideTabSize.height),
        ) {
            Box(contentAlignment = Alignment.Center) {
                Icon(
                    painterResource(R.drawable.ic_auto_awesome),
                    contentDescription = null,
                    modifier = Modifier.size(20.dp),
                )
            }
        }
    }
}

private enum class Press { Tap, Drag, Hold }

private suspend fun PointerInputScope.detectSideTabGestures(
    actions: SideTabActions,
    onHeld: () -> Unit,
) = awaitEachGesture {
    val down = awaitFirstDown()
    when (withTimeoutOrNull(viewConfiguration.longPressTimeoutMillis) { awaitTapOrDrag(down) }) {
        Press.Tap -> {
            actions.onTap()
        }

        Press.Drag -> {
            trackDrag(down, actions)
        }

        else -> {
            onHeld()
            when (awaitTapOrDrag(down)) {
                Press.Drag -> trackDrag(down, actions)
                else -> actions.onHold()
            }
        }
    }
}

/** Waits until the finger lifts ([Press.Tap]) or moves past the touch slop ([Press.Drag]). */
private suspend fun AwaitPointerEventScope.awaitTapOrDrag(down: PointerInputChange): Press {
    while (true) {
        val change =
            awaitPointerEvent().changes.firstOrNull { it.id == down.id } ?: return Press.Tap
        if (change.changedToUp()) return Press.Tap
        val moved = change.position - down.position
        if (moved.getDistance() > viewConfiguration.touchSlop) return Press.Drag
    }
}

/**
 * Follows a drag until the finger lifts. A mostly vertical drag moves the tab; a horizontal one
 * opens or closes the panel once it passes [SWIPE_DISTANCE].
 */
private suspend fun AwaitPointerEventScope.trackDrag(
    down: PointerInputChange,
    actions: SideTabActions,
) {
    var vertical: Boolean? = null
    var swiped = false
    while (true) {
        val change = awaitPointerEvent().changes.firstOrNull { it.id == down.id } ?: return
        if (change.changedToUp()) return
        val moved = change.position - down.position
        val isVertical = vertical ?: (abs(moved.y) > abs(moved.x)).also { vertical = it }
        change.consume()
        if (isVertical) {
            // The tab moves under the finger, so positions are relative to where it is now: the
            // finger's offset from its starting point on the tab is how far the tab lags behind.
            actions.onMove(moved.y)
        } else if (!swiped && abs(moved.x) > SWIPE_DISTANCE.toPx()) {
            swiped = true
            if (moved.x < 0) actions.onOpen() else actions.onClose()
        }
    }
}

private val SWIPE_DISTANCE = 24.dp

/** Panel callbacks; [onSend] reads the screen, with the typed text or an empty string. */
class PanelActions(
    val onClose: () -> Unit = {},
    val onHideButton: () -> Unit = {},
    val onNewRun: () -> Unit = {},
    val onConfirmNewRun: () -> Unit = {},
    val onCancelNewRun: () -> Unit = {},
    val onCompose: () -> Unit = {},
    val onSend: (String) -> Unit = {},
    val onStop: () -> Unit = {},
    val onChangeCharacter: () -> Unit = {},
)

/** Below this height the top bar would leave the conversation almost no room. */
private val TOP_BAR_MIN_PANEL_HEIGHT = 240.dp

/** The side panel, in the side tab's color so the two read as one piece. */
@Composable
fun Panel(
    state: PanelState,
    draft: TextFieldState,
    actions: PanelActions,
    pickerActions: PickerActions,
) = Surface(
    color = MaterialTheme.colorScheme.primaryContainer,
    contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
    shadowElevation = 8.dp,
    modifier = Modifier.fillMaxSize(),
) {
    // The keyboard covers most of a phone's landscape height: the panel shrinks to the space above
    // it and drops the top bar when that space is too short for it.
    BoxWithConstraints(Modifier.imePadding()) {
        val roomForTopBar = maxHeight >= TOP_BAR_MIN_PANEL_HEIGHT
        Column {
            if (roomForTopBar) PanelTopBar(state, actions)
            PanelBody(state, draft, actions, pickerActions)
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
                contentDescription = "Hide side tab",
            )
        }
        IconButton(onClick = actions.onClose) {
            Icon(painterResource(R.drawable.ic_close), contentDescription = "Close panel")
        }
    },
    windowInsets = WindowInsets(0),
    colors =
        TopAppBarDefaults.topAppBarColors(
            containerColor = MaterialTheme.colorScheme.primaryContainer,
            titleContentColor = MaterialTheme.colorScheme.onPrimaryContainer,
            actionIconContentColor = MaterialTheme.colorScheme.onPrimaryContainer,
        ),
)

@Composable
private fun PanelBody(
    state: PanelState,
    draft: TextFieldState,
    actions: PanelActions,
    pickerActions: PickerActions,
) = Column(
    Modifier.padding(start = 16.dp, end = 16.dp, bottom = 16.dp),
    verticalArrangement = Arrangement.spacedBy(8.dp),
) {
    RunCharacterChip(state.character, actions.onChangeCharacter)
    if (state.confirmingNewRun) {
        Text("Start a new run? This clears the conversation.")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = actions.onConfirmNewRun) { Text("New run") }
            OutlinedButton(onClick = actions.onCancelNewRun) { Text("Keep this run") }
        }
    }
    Conversation(state, pickerActions, Modifier.weight(1f))
    state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    AskRow(state, draft, actions)
}

/** The run's character, which Claude's advice assumes; a tap opens the picker to change it. */
@Composable
private fun RunCharacterChip(
    character: String?,
    onClick: () -> Unit,
) = AssistChip(
    onClick = onClick,
    label = { Text(character?.let { "Run: $it" } ?: "Set run character") },
    leadingIcon = {
        Icon(
            painterResource(R.drawable.ic_person),
            contentDescription = null,
            modifier = Modifier.size(AssistChipDefaults.IconSize),
        )
    },
)

@Composable
private fun Conversation(
    state: PanelState,
    pickerActions: PickerActions,
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
        state.picker?.takeIf { it.inline }?.let { picker ->
            item(key = "inline-picker") { InlineCharacterChoice(picker, pickerActions) }
        }
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
        color = MaterialTheme.colorScheme.secondaryContainer,
        contentColor = MaterialTheme.colorScheme.onSecondaryContainer,
    ) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text(
                    "Seen on screen",
                    style = MaterialTheme.typography.labelMedium,
                )
                Icon(
                    painterResource(
                        if (expanded) R.drawable.ic_expand_less else R.drawable.ic_expand_more,
                    ),
                    contentDescription = if (expanded) "Collapse" else "Expand",
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
        Text("Reading the screen...")
    }

/** A typed question, or a divider for a screen read sent without text. */
@Composable
private fun Question(text: String) {
    if (text.isBlank()) {
        HorizontalDivider(Modifier.padding(vertical = 4.dp))
        return
    }
    Text(text, style = MaterialTheme.typography.labelLarge)
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
        AskButton(state.busy, draft, actions)
    }
}

/** Stop while a reply runs; otherwise read the screen (no text) or send the typed question. */
@Composable
private fun AskButton(
    busy: Boolean,
    draft: TextFieldState,
    actions: PanelActions,
) {
    val (icon, label) =
        when {
            busy -> R.drawable.ic_stop to "Stop"
            draft.text.isBlank() -> R.drawable.ic_photo_camera to "Read screen"
            else -> R.drawable.ic_send to "Send"
        }
    FilledIconButton(
        onClick = { if (busy) actions.onStop() else actions.onSend(draft.text.trim().toString()) },
        modifier = Modifier.padding(start = 8.dp),
    ) { Icon(painterResource(icon), contentDescription = label) }
}

@PreviewLightDark
@PreviewFontScale
@Composable
private fun PanelPreview() =
    CompanionTheme {
        Box(Modifier.size(468.dp, 600.dp)) {
            SideOverlay(
                open = true,
                tabY = 0,
                panelWidth = 420.dp,
                tabActions = SideTabActions(),
                onTouchableArea = {},
            ) { PanelPreviewContent() }
        }
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
            character = "Rossweisse",
            picker =
                PickerState(
                    candidates = listOf("Mei Lin", "Veronica", "Tiphera"),
                    forReply = true,
                ),
        ),
        TextFieldState(),
        PanelActions(),
        PickerActions(),
    )
