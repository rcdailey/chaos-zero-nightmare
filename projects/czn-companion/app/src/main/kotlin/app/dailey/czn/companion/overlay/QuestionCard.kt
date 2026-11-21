package app.dailey.czn.companion.overlay

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.foundation.text.input.rememberTextFieldState
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.SegmentedListItem
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.key
import androidx.compose.runtime.remember
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.tooling.preview.PreviewFontScale
import androidx.compose.ui.tooling.preview.PreviewLightDark
import androidx.compose.ui.unit.dp
import app.dailey.czn.companion.CompanionTheme
import app.dailey.czn.companion.R
import app.dailey.czn.companion.ask.AnsweredAsk
import app.dailey.czn.companion.ask.AskProgress
import app.dailey.czn.companion.ask.Option
import app.dailey.czn.companion.ask.PlayerAsks
import app.dailey.czn.companion.ask.Question
import app.dailey.czn.companion.characters.RosterCharacter
import app.dailey.czn.companion.extraColors

/**
 * Callbacks of the question card. [onChange] applies one step of the player's answer;
 * [onStartTyping] gives the panel the keyboard for a typed answer.
 */
class AskActions(
    val onChange: ((AskProgress) -> AskProgress) -> Unit = {},
    val onSkip: () -> Unit = {},
    val onStartTyping: () -> Unit = {},
)

/**
 * Claude's questions waiting for the player, as the newest item in the conversation: one
 * question at a time, then a review of every answer when there are several. Skip skips them all.
 * While the player types an answer in a [short] panel (the phone's keyboard covers most of the
 * screen), the card keeps only the question, the text box, and its buttons.
 */
@Composable
fun AskCard(
    progress: AskProgress,
    roster: RosterState,
    typing: Boolean,
    short: Boolean,
    actions: AskActions,
) = Surface(
    shape = MaterialTheme.shapes.large,
    color = MaterialTheme.colorScheme.surfaceContainerHigh,
    contentColor = MaterialTheme.colorScheme.onSurface,
) {
    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        StepLine(progress)
        val question = progress.current
        if (question == null) {
            Review(progress, actions)
        } else {
            key(progress.questions, progress.step) {
                QuestionStep(progress, question, roster, typing, short, actions)
            }
        }
        AskButtons(progress, actions)
    }
}

@Composable
private fun StepLine(progress: AskProgress) {
    val count = progress.questions.size
    val text =
        when {
            progress.reviewing -> "Check your answers"
            count > 1 -> "${progress.step + 1} of $count: ${progress.current?.header}"
            else -> progress.current?.header
        } ?: return
    Text(
        text,
        style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.primary,
    )
}

@Composable
private fun QuestionStep(
    progress: AskProgress,
    question: Question,
    roster: RosterState,
    typing: Boolean,
    short: Boolean,
    actions: AskActions,
) {
    val draft = progress.draft ?: return
    Text(question.question, style = MaterialTheme.typography.bodyLarge)
    if (!(typing && short)) {
        if (question.characters) CharacterOptions(question, roster, draft.picked, actions)
        // One segmented group: each option is its own tile, the typed answer last.
        val listed = if (question.characters) emptyList() else question.options
        val count = listed.size + 1
        Column(
            Modifier.selectableGroup(),
            verticalArrangement = Arrangement.spacedBy(ListItemDefaults.SegmentedGap),
        ) {
            listed.forEachIndexed { index, option ->
                OptionRow(option, index in draft.picked, question.multiSelect, index, count) {
                    actions.onChange { it.pick(index) }
                }
            }
            OptionRow(Option(OTHER_LABEL), draft.other, question.multiSelect, count - 1, count) {
                actions.onChange { it.toggleOther() }
            }
        }
    }
    if (draft.other) {
        val field = rememberTextFieldState(draft.typed)
        LaunchedEffect(field) {
            snapshotFlow { field.text.toString() }.collect { text ->
                actions.onChange { it.type(text) }
            }
        }
        TypingField(field, "Your answer", typing, actions.onStartTyping, Modifier.fillMaxWidth())
    }
}

/** The candidates as card art in one row that scrolls sideways; a tap picks one. */
@Composable
private fun CharacterOptions(
    question: Question,
    roster: RosterState,
    picked: Set<Int>,
    actions: AskActions,
) = LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
    val characters = roster.characters(question.options.map { it.label })
    itemsIndexed(characters, key = { _, character -> character.name }) { index, character ->
        CharacterCard(
            character,
            onPick = { actions.onChange { it.pick(index) } },
            modifier = Modifier.width(CHARACTER_CARD_WIDTH),
            selected = index in picked,
        )
    }
}

/**
 * One option as a tile of a segmented list ([index] of [count]): a radio button for single
 * choice, a checkbox for multiple. The whole tile takes the tap, a picked tile changes its
 * fill as well as its control, and the label and description wrap to any length.
 */
@Composable
private fun OptionRow(
    option: Option,
    selected: Boolean,
    multiSelect: Boolean,
    index: Int,
    count: Int,
    onClick: () -> Unit,
) {
    val supporting: (@Composable () -> Unit)? = option.description?.let { { Text(it) } }
    val overline: (@Composable () -> Unit)? =
        if (option.recommended) {
            (
                {
                    RecommendedLabel()
                }
            )
        } else {
            null
        }
    val colors =
        ListItemDefaults.colors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerHighest,
        )
    val shapes = ListItemDefaults.segmentedShapes(index = index, count = count)
    if (multiSelect) {
        SegmentedListItem(
            checked = selected,
            onCheckedChange = { onClick() },
            leadingContent = { Checkbox(checked = selected, onCheckedChange = null) },
            overlineContent = overline,
            supportingContent = supporting,
            colors = colors,
            shapes = shapes,
        ) { Text(option.label) }
    } else {
        SegmentedListItem(
            selected = selected,
            onClick = onClick,
            leadingContent = { RadioButton(selected = selected, onClick = null) },
            overlineContent = overline,
            supportingContent = supporting,
            colors = colors,
            shapes = shapes,
        ) { Text(option.label) }
    }
}

/** Marks Claude's suggested option in green, with a thumbs-up and the word, not color alone. */
@Composable
private fun RecommendedLabel() =
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        val green = extraColors.recommended
        Icon(
            painterResource(R.drawable.ic_thumb_up),
            contentDescription = null,
            tint = green,
            modifier = Modifier.size(16.dp),
        )
        Text("Recommended", color = green, style = MaterialTheme.typography.labelLarge)
    }

/** Every answer by its short label; a tap goes back to that question to change it. */
@Composable
private fun Review(
    progress: AskProgress,
    actions: AskActions,
) {
    val answers = progress.answers()
    val count = progress.questions.size
    Column(verticalArrangement = Arrangement.spacedBy(ListItemDefaults.SegmentedGap)) {
        progress.questions.forEachIndexed { index, question ->
            SegmentedListItem(
                onClick = { actions.onChange { it.edit(index) } },
                overlineContent = { Text(question.header) },
                trailingContent = { Text("Change", color = MaterialTheme.colorScheme.primary) },
                colors =
                    ListItemDefaults.colors(
                        containerColor = MaterialTheme.colorScheme.surfaceContainerHighest,
                    ),
                shapes = ListItemDefaults.segmentedShapes(index = index, count = count),
            ) { Text(PlayerAsks.answerText(answers[index])) }
        }
    }
}

/**
 * Back and Skip, then the button that moves on: Send for the last step, Next before it. A
 * single-choice tap moves on by itself, so its button shows only for a typed answer.
 */
@Composable
private fun AskButtons(
    progress: AskProgress,
    actions: AskActions,
) = FlowRow(
    horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End),
    verticalArrangement = Arrangement.spacedBy(8.dp),
    modifier = Modifier.fillMaxWidth(),
) {
    if (progress.canGoBack) {
        TextButton(onClick = { actions.onChange { it.back() } }) { Text("Back") }
    }
    TextButton(onClick = actions.onSkip) { Text("Skip") }
    val question = progress.current
    when {
        progress.reviewing -> {
            Button(onClick = { actions.onChange { it.confirm() } }) { Text("Send") }
        }

        question != null && (question.multiSelect || progress.draft?.other == true) -> {
            val last = progress.questions.size == 1
            Button(
                onClick = { actions.onChange { it.next() } },
                enabled = progress.draft?.answered == true,
            ) { Text(if (last) "Send" else "Next") }
        }
    }
}

/** Questions Claude asked earlier in a reply, with the player's answers. */
@Composable
fun AnsweredAskCard(ask: AnsweredAsk) =
    Surface(
        shape = MaterialTheme.shapes.small,
        color = MaterialTheme.colorScheme.surfaceContainerHighest,
        contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
    ) {
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            val answers = ask.answers
            if (answers == null) {
                Text(
                    "Skipped: ${ask.questions.joinToString { it.header }}",
                    style = MaterialTheme.typography.labelMedium,
                )
                return@Column
            }
            ask.questions.zip(answers).forEach { (question, answer) ->
                Column {
                    Text(question.header, style = MaterialTheme.typography.labelMedium)
                    Text(PlayerAsks.answerText(answer), style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
    }

/**
 * A text box in the panel window, which is not focusable until the player taps a box: until
 * [typing], a tap goes to [onStartTyping], which makes the window focusable, and the box then
 * takes focus and the keyboard.
 */
@Composable
fun TypingField(
    state: TextFieldState,
    placeholder: String,
    typing: Boolean,
    onStartTyping: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val focus = remember { FocusRequester() }
    LaunchedEffect(typing) { if (typing) focus.requestFocus() }
    Box(modifier) {
        OutlinedTextField(
            state = state,
            placeholder = { Text(placeholder) },
            modifier = Modifier.fillMaxWidth().focusRequester(focus),
        )
        if (!typing) Box(Modifier.matchParentSize().clickable(onClick = onStartTyping))
    }
}

private const val OTHER_LABEL = "Type my own answer"

private val CHARACTER_CARD_WIDTH = 112.dp

private val previewQuestions =
    listOf(
        Question(
            question =
                "Which path do you want to take after this fight? The elite gives an " +
                    "Epiphany, the shop lets you remove a card.",
            header = "Path",
            options =
                listOf(
                    Option("Elite fight", "Harder, but Epiphany reward", recommended = true),
                    Option("Shop", "Remove Strike for 75 credits"),
                    Option("Rest"),
                ),
        ),
        Question(
            question = "Which cards do you want to keep no matter what?",
            header = "Keep",
            options = listOf(Option("Blade of the Cycle"), Option("Radiant Sword Flow")),
            multiSelect = true,
        ),
    )

@PreviewLightDark
@PreviewFontScale
@Composable
private fun AskCardPreview() =
    CompanionTheme(dark = true) {
        Box(Modifier.width(380.dp)) {
            AskCard(
                AskProgress(previewQuestions).pick(0).toggleOther().type("Keep both"),
                RosterState.Loading,
                typing = false,
                short = false,
                AskActions(),
            )
        }
    }

@PreviewLightDark
@Composable
private fun AskReviewPreview() =
    CompanionTheme(dark = true) {
        Box(Modifier.width(380.dp)) {
            AskCard(
                AskProgress(previewQuestions).pick(0).pick(1).next(),
                RosterState.Loading,
                typing = false,
                short = false,
                AskActions(),
            )
        }
    }

@PreviewLightDark
@Composable
private fun CharacterAskPreview() =
    CompanionTheme(dark = true) {
        Box(Modifier.width(380.dp)) {
            AskCard(
                AskProgress(
                    listOf(
                        Question(
                            "Which character's deck does this run build?",
                            "Run character",
                            listOf(Option("Mei Lin"), Option("Veronica"), Option("Tiphera")),
                            characters = true,
                        ),
                    ),
                ),
                RosterState.Loaded(listOf(RosterCharacter("Mei Lin"))),
                typing = false,
                short = false,
                AskActions(),
            )
        }
    }
