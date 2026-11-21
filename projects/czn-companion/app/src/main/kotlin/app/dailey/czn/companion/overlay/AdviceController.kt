package app.dailey.czn.companion.overlay

import android.util.Log
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.foundation.text.input.clearText
import androidx.compose.foundation.text.input.setTextAndPlaceCursorAtEnd
import app.dailey.czn.companion.ask.Answer
import app.dailey.czn.companion.ask.AnsweredAsk
import app.dailey.czn.companion.ask.AskProgress
import app.dailey.czn.companion.ask.Question
import app.dailey.czn.companion.characters.CharacterRoster
import app.dailey.czn.companion.characters.RosterCharacter
import app.dailey.czn.companion.claude.ClaudeConversation
import app.dailey.czn.companion.claude.FailureKind
import app.dailey.czn.companion.claude.PlayerInput
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
    /** The full-screen character picker, while it is open. */
    val picker: PickerState? = null,
    /** Every character with card art, for the picker and character questions. */
    val roster: RosterState = RosterState.Loading,
    /** The question being answered, until its reply completes. */
    val pendingQuestion: String? = null,
    /** Reply text streamed so far for [pendingQuestion]. */
    val streaming: String = "",
    /** CZN data tool calls made so far for [pendingQuestion]. */
    val pendingTools: List<String> = emptyList(),
    /** Questions Claude asked so far for [pendingQuestion], with the answers. */
    val pendingAsks: List<AnsweredAsk> = emptyList(),
    /** Questions Claude asked that wait for the player, shown in the conversation. */
    val ask: AskProgress? = null,
    val busy: Boolean = false,
    val error: String? = null,
    /** Which text box has the keyboard; null while none does. */
    val typing: Typing? = null,
    val confirmingNewRun: Boolean = false,
)

/** A text box in the panel: the question for Claude, or a typed answer to Claude's question. */
enum class Typing { Question, Answer }

/**
 * The full-screen character picker over the whole roster. [forReply] means a reply waits for the
 * pick; the picker then also offers to stop the reply.
 */
data class PickerState(
    val forReply: Boolean = false,
)

sealed interface RosterState {
    data object Loading : RosterState

    data class Loaded(
        val characters: List<RosterCharacter>,
    ) : RosterState

    data class Failed(
        val message: String,
    ) : RosterState
}

/** The roster entries for [names], with card art where the roster has it. */
fun RosterState.characters(names: List<String>): List<RosterCharacter> {
    val loaded = (this as? RosterState.Loaded)?.characters.orEmpty()
    return names.map { name ->
        loaded.firstOrNull { it.name.equals(name, ignoreCase = true) } ?: RosterCharacter(name)
    }
}

/**
 * Panel state holder: sends questions with a screenshot, streams replies, stops them, handles New
 * run, runs the character picker for Claude and the player, and walks the player through
 * Claude's questions. A request keeps running when the panel closes; its reply is saved to the
 * run when it completes.
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

    /** A reply waiting for the player to pick the run's character from the whole roster. */
    private val replyPick = PlayerWait<String>()

    /** A reply waiting for the player to answer Claude's questions. */
    private val replyAsk = PlayerWait<List<Answer>>()

    private val playerInput =
        object : PlayerInput {
            override suspend fun pickCharacter() =
                replyPick.await(
                    show = { openPicker(PickerState(forReply = true)) },
                    hide = { local.update { it.copy(picker = null) } },
                )

            override suspend fun ask(questions: List<Question>) =
                replyAsk.await(
                    show = {
                        local.update { it.copy(ask = AskProgress(questions)) }
                        if (questions.any { it.characters }) loadRoster()
                    },
                    hide = {
                        local.update {
                            it.copy(
                                ask = null,
                                typing =
                                    it.typing.takeIf { t ->
                                        t != Typing.Answer
                                    },
                            )
                        }
                    },
                )
        }

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
                    pendingAsks = emptyList(),
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
                pendingAsks = emptyList(),
                busy = true,
                error = null,
                typing = null,
            )
        }
        reply =
            scope.launch {
                conversation.ask(question, jpeg, playerInput).collect { event ->
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

                            is ReplyEvent.Asked -> {
                                Log.d(TAG, "ask answered, skipped=${event.ask.answers == null}")
                                state.copy(pendingAsks = state.pendingAsks + event.ask)
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

    /** Changes the shown question's answer; sends the answers once the player completes them. */
    fun updateAsk(change: (AskProgress) -> AskProgress) {
        val progress = local.value.ask?.let(change) ?: return
        if (progress.complete) {
            replyAsk.answer(progress.answers())
            return
        }
        // The keyboard stays only while the shown question still has its text box; otherwise
        // a short panel would keep hiding the options of the question Next or Back moved to.
        val keepTyping = progress.step == local.value.ask?.step && progress.draft?.other == true
        local.update {
            it.copy(
                ask = progress,
                typing =
                    it.typing.takeIf { t ->
                        t != Typing.Answer ||
                            keepTyping
                    },
            )
        }
    }

    fun skipAsk() {
        replyAsk.answer(null)
    }

    fun retryRoster() = loadRoster()

    private fun openPicker(picker: PickerState) {
        local.update { it.copy(picker = picker) }
        loadRoster()
    }

    /** Loads the roster unless it is loaded; a failure shows in the picker with a retry. */
    private fun loadRoster() {
        if (local.value.roster is RosterState.Loaded) return
        local.update { it.copy(roster = RosterState.Loading) }
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
            local.update { it.copy(roster = loaded) }
        }
    }

    fun startTyping(target: Typing) = local.update { it.copy(typing = target) }

    fun stopTyping() = local.update { it.copy(typing = null) }

    fun requestNewRun() = local.update { it.copy(confirmingNewRun = true) }

    fun cancelNewRun() = local.update { it.copy(confirmingNewRun = false) }

    fun confirmNewRun() {
        Log.d(TAG, "new run")
        local.update { it.copy(confirmingNewRun = false, error = null).finished() }
        scope.launch { conversation.newRun() }
    }

    private fun PanelState.finished() =
        copy(
            pendingQuestion = null,
            streaming = "",
            pendingTools = emptyList(),
            pendingAsks = emptyList(),
            busy = false,
        )

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
