package app.dailey.czn.companion.overlay

import android.util.Log
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.foundation.text.input.clearText
import androidx.compose.foundation.text.input.setTextAndPlaceCursorAtEnd
import app.dailey.czn.companion.characters.CharacterRoster
import app.dailey.czn.companion.characters.RosterCharacter
import app.dailey.czn.companion.claude.ClaudeConversation
import app.dailey.czn.companion.claude.FailureKind
import app.dailey.czn.companion.claude.ReplyEvent
import app.dailey.czn.companion.run.RunEntry
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** What the panel and the character picker show. */
data class PanelState(
    val entries: List<RunEntry> = emptyList(),
    /** The run's character; null until Claude or the player sets it. */
    val character: String? = null,
    /** The character picker, while it is open. */
    val picker: PickerState? = null,
    /** The question being answered, until its reply completes. */
    val pendingQuestion: String? = null,
    /** Reply text streamed so far for [pendingQuestion]. */
    val streaming: String = "",
    /** CZN data tool calls made so far for [pendingQuestion]. */
    val pendingTools: List<String> = emptyList(),
    val busy: Boolean = false,
    val error: String? = null,
    val composing: Boolean = false,
    val confirmingNewRun: Boolean = false,
)

/**
 * The character picker. [candidates] limits the choice when Claude names them (for example the
 * team of three it saw); empty offers the whole roster.
 */
data class PickerState(
    val candidates: List<String> = emptyList(),
    /** Whether a reply waits for the pick; the picker then also offers to stop the reply. */
    val forReply: Boolean = false,
    val roster: RosterState = RosterState.Loading,
) {
    /**
     * Whether the picker shows in the conversation: Claude asks with a few named characters. The
     * whole roster needs the full-screen grid.
     */
    val inline get() = forReply && candidates.isNotEmpty()

    /** The cards to show: the candidates, or every character, with art where the roster has it. */
    val options: List<RosterCharacter>
        get() {
            val loaded = (roster as? RosterState.Loaded)?.characters.orEmpty()
            if (candidates.isEmpty()) return loaded
            return candidates.map { name ->
                loaded.firstOrNull { it.name.equals(name, ignoreCase = true) }
                    ?: RosterCharacter(name)
            }
        }
}

sealed interface RosterState {
    data object Loading : RosterState

    data class Loaded(
        val characters: List<RosterCharacter>,
    ) : RosterState

    data class Failed(
        val message: String,
    ) : RosterState
}

/**
 * Panel state holder: sends questions with a screenshot, streams replies, stops them, handles New
 * run, and runs the character picker for Claude and the player. A request keeps running when the
 * panel closes; its reply is saved to the run when it completes.
 */
class AdviceController(
    private val scope: CoroutineScope,
    private val conversation: ClaudeConversation,
    private val roster: CharacterRoster,
) {
    private val local = MutableStateFlow(PanelState())

    /** Saved entries when the pending question was asked; the reply is saved once this grows. */
    private var entryCountAtAsk = 0

    private var reply: Job? = null

    /** A reply waiting for the player to pick the run's character. */
    private val replyPick = PlayerWait<String>()

    // A completed reply stays shown as pending until the saved run includes it, so the list never
    // briefly drops both items and jumps.
    val state: StateFlow<PanelState> =
        combine(conversation.entries, conversation.character, local) { entries, character, panel ->
            val saved = !panel.busy && entries.size > entryCountAtAsk
            if (panel.pendingQuestion != null && saved) {
                panel.copy(
                    entries = entries,
                    character = character,
                    pendingQuestion = null,
                    streaming = "",
                    pendingTools = emptyList(),
                )
            } else {
                panel.copy(entries = entries, character = character)
            }
        }.stateIn(scope, SharingStarted.Eagerly, PanelState())

    val busy get() = local.value.busy

    /**
     * The question being typed. It outlives the panel window, and a typed question that fails is
     * put back so it can be sent again.
     */
    val draft = TextFieldState()

    /** Asks [question] about [screenshot]; ignored while a reply is in progress. */
    fun ask(
        question: String,
        screenshot: Result<ByteArray>,
    ) {
        if (local.value.busy) {
            Log.d(TAG, "ask ignored, reply in progress: $question")
            return
        }
        val jpeg =
            screenshot.getOrElse { failure ->
                Log.w(TAG, "screenshot failed", failure)
                local.update { it.copy(error = "Could not take a screenshot: ${failure.message}") }
                return
            }
        entryCountAtAsk = state.value.entries.size
        Log.d(TAG, "ask '$question', ${jpeg.size} B jpeg, $entryCountAtAsk saved entries")
        if (question.isNotEmpty()) draft.clearText()
        local.update {
            it.copy(
                pendingQuestion = question,
                streaming = "",
                pendingTools = emptyList(),
                busy = true,
                error = null,
                composing = false,
            )
        }
        reply =
            scope.launch {
                conversation.ask(question, jpeg, ::pickForReply).collect { event ->
                    if (event is ReplyEvent.Failed && draft.text.isEmpty()) {
                        draft.setTextAndPlaceCursorAtEnd(question)
                    }
                    local.update { state ->
                        when (event) {
                            is ReplyEvent.TextDelta -> {
                                if (state.streaming.isEmpty()) Log.d(TAG, "first reply text")
                                state.copy(
                                    streaming =
                                        state.streaming + event.text,
                                )
                            }

                            is ReplyEvent.ToolCall -> {
                                Log.d(TAG, "tool call ${event.label}")
                                state.copy(pendingTools = state.pendingTools + event.label)
                            }

                            ReplyEvent.Done -> {
                                Log.d(TAG, "reply done, ${state.streaming.length} chars")
                                state.copy(busy = false)
                            }

                            is ReplyEvent.Failed -> {
                                Log.w(TAG, "reply failed: ${event.kind} ${event.message}")
                                state.finished().copy(error = describe(event))
                            }
                        }
                    }
                }
            }
    }

    /**
     * Stops the reply in progress, including one waiting for the picker, which closes. Nothing is
     * saved, and a typed question goes back into the text box.
     */
    fun stop() {
        val question = local.value.pendingQuestion ?: return
        Log.d(TAG, "stop")
        reply?.cancel()
        reply = null
        if (question.isNotEmpty() && draft.text.isEmpty()) {
            draft.setTextAndPlaceCursorAtEnd(question)
        }
        local.update { it.finished().copy(picker = null) }
    }

    /** Opens the picker for the player to set or change the run's character. */
    fun openCharacterPicker() = openPicker(PickerState())

    fun pickCharacter(name: String) {
        Log.d(TAG, "picked $name, reply waiting=${replyPick.waiting}")
        local.update { it.copy(picker = null) }
        // A waiting reply saves the pick itself, with the tool result.
        if (!replyPick.answer(name)) scope.launch { conversation.setCharacter(name) }
    }

    fun closePicker() {
        local.update { it.copy(picker = null) }
        replyPick.answer(null)
    }

    fun retryRoster() = local.value.picker?.let { openPicker(it) }

    private suspend fun pickForReply(candidates: List<String>): String? =
        replyPick.await(
            show = { openPicker(PickerState(candidates, forReply = true)) },
            hide = { local.update { it.copy(picker = null) } },
        )

    private fun openPicker(picker: PickerState) {
        local.update { it.copy(picker = picker.copy(roster = RosterState.Loading)) }
        scope.launch {
            val loaded =
                try {
                    RosterState.Loaded(roster.characters())
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    Log.w(TAG, "roster failed", e)
                    RosterState.Failed(e.message ?: "Could not load the characters.")
                }
            local.update { state -> state.copy(picker = state.picker?.copy(roster = loaded)) }
        }
    }

    fun startComposing() = local.update { it.copy(composing = true) }

    fun stopComposing() = local.update { it.copy(composing = false) }

    fun requestNewRun() = local.update { it.copy(confirmingNewRun = true) }

    fun cancelNewRun() = local.update { it.copy(confirmingNewRun = false) }

    fun confirmNewRun() {
        Log.d(TAG, "new run")
        local.update { it.copy(confirmingNewRun = false, error = null).finished() }
        scope.launch { conversation.newRun() }
    }

    private fun PanelState.finished() =
        copy(pendingQuestion = null, streaming = "", pendingTools = emptyList(), busy = false)

    private fun describe(failure: ReplyEvent.Failed) =
        when (failure.kind) {
            FailureKind.AnthropicSignIn -> "Sign in to Claude again in the CZN Companion app."
            FailureKind.CznSignIn -> "Sign in to the CZN data server again in the app."
            FailureKind.Network -> "No connection (${failure.message}). Nothing saved; try again."
            FailureKind.Api -> "Claude returned an error: ${failure.message}"
        }

    private companion object {
        const val TAG = "CznAdvice"
    }
}
