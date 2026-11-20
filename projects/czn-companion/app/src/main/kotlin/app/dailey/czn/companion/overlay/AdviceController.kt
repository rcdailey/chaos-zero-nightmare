package app.dailey.czn.companion.overlay

import android.util.Log
import app.dailey.czn.companion.claude.ClaudeConversation
import app.dailey.czn.companion.claude.FailureKind
import app.dailey.czn.companion.claude.ReplyEvent
import app.dailey.czn.companion.run.RunEntry
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** What the panel shows. */
data class PanelState(
    val entries: List<RunEntry> = emptyList(),
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
 * Panel state holder: sends questions with a screenshot, streams replies, and handles New run. A
 * request keeps running when the panel closes; its reply is saved to the run when it completes.
 */
class AdviceController(
    private val scope: CoroutineScope,
    private val conversation: ClaudeConversation,
) {
    private val local = MutableStateFlow(PanelState())

    /** Saved entries when the pending question was asked; the reply is saved once this grows. */
    private var entryCountAtAsk = 0

    // A completed reply stays shown as pending until the saved run includes it, so the list never
    // briefly drops both items and jumps.
    val state: StateFlow<PanelState> =
        combine(conversation.entries, local) { entries, panel ->
            val saved = !panel.busy && entries.size > entryCountAtAsk
            if (panel.pendingQuestion != null && saved) {
                panel.copy(
                    entries = entries,
                    pendingQuestion = null,
                    streaming = "",
                    pendingTools = emptyList(),
                )
            } else {
                panel.copy(entries = entries)
            }
        }.stateIn(scope, SharingStarted.Eagerly, PanelState())

    val busy get() = local.value.busy

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
        scope.launch {
            conversation.ask(question, jpeg).collect { event ->
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
