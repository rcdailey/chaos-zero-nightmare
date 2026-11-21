package app.dailey.czn.companion.ask

/** The player's unsent answer to one question; [other] means the typed-answer option is chosen. */
data class AnswerDraft(
    val picked: Set<Int> = emptySet(),
    val other: Boolean = false,
    val typed: String = "",
) {
    val answered get() = picked.isNotEmpty() || (other && typed.isNotBlank())
}

/**
 * The player working through an ask, one question at a time. A single-choice tap picks and moves
 * on; a multiple-choice question or a typed answer moves on with [next]. One question is sent as
 * soon as it is answered. Several end on a review step ([reviewing]) where the player can go back
 * to any question and then [confirm]; after an edit, moving on returns to the review.
 */
data class AskProgress(
    val questions: List<Question>,
    val drafts: List<AnswerDraft> = questions.map { AnswerDraft() },
    /** The question shown; `questions.size` is the review step. */
    val step: Int = 0,
    /** Whether the player has reached the review step, so moving on returns to it. */
    val reviewed: Boolean = false,
    private val confirmed: Boolean = false,
) {
    val reviewing get() = step == questions.size

    val current get() = questions.getOrNull(step)

    val draft get() = drafts.getOrNull(step)

    val canGoBack get() = step > 0

    /** Whether every answer is in and the player sent them. */
    val complete get() = confirmed || (questions.size == 1 && step == 1)

    fun pick(option: Int): AskProgress {
        val question = current ?: return this
        if (question.multiSelect) return toggle(option)
        return update { AnswerDraft(picked = setOf(option)) }.next()
    }

    private fun toggle(option: Int) =
        update {
            it.copy(
                picked =
                    if (option in
                        it.picked
                    ) {
                        it.picked - option
                    } else {
                        it.picked + option
                    },
            )
        }

    /** Chooses the typed-answer option; single choice drops the picked option, multiple keeps it. */
    fun toggleOther(): AskProgress {
        val question = current ?: return this
        return update {
            if (question.multiSelect) {
                it.copy(
                    other = !it.other,
                )
            } else {
                it.copy(picked = emptySet(), other = true)
            }
        }
    }

    fun type(text: String) = update { it.copy(typed = text) }

    /** Moves on once the shown question has an answer. */
    fun next(): AskProgress {
        if (draft?.answered != true) return this
        val target = if (reviewed) questions.size else step + 1
        return copy(step = target, reviewed = reviewed || target == questions.size)
    }

    fun back() = if (canGoBack) copy(step = step - 1) else this

    fun edit(question: Int) = if (question in questions.indices) copy(step = question) else this

    fun confirm() = if (reviewing && drafts.all { it.answered }) copy(confirmed = true) else this

    fun answers(): List<Answer> =
        questions.zip(drafts).map { (question, draft) ->
            Answer(
                picked = draft.picked.sorted().map { question.options[it].label },
                typed = draft.typed.trim().takeIf { draft.other && it.isNotEmpty() },
            )
        }

    private fun update(change: (AnswerDraft) -> AnswerDraft): AskProgress {
        if (current == null) return this
        return copy(drafts = drafts.toMutableList().also { it[step] = change(it[step]) })
    }
}
