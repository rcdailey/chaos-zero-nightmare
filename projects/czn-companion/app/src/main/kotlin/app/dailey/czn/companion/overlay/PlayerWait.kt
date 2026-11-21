package app.dailey.czn.companion.overlay

import kotlinx.coroutines.CompletableDeferred

/**
 * A reply waiting for one kind of player answer, such as a character pick; each kind of ask owns
 * one instance. [await] shows the ask, suspends the reply until [answer], and hides the ask
 * again, also when the reply is cancelled (Stop). A null answer means the player skipped.
 * One ask of a kind waits at a time, as a reply runs its tool calls one after another.
 */
class PlayerWait<T : Any> {
    private var pending: CompletableDeferred<T?>? = null

    val waiting get() = pending != null

    suspend fun await(
        show: () -> Unit,
        hide: () -> Unit,
    ): T? {
        check(pending == null) { "An ask of this kind is already waiting." }
        val answer = CompletableDeferred<T?>()
        pending = answer
        show()
        return try {
            answer.await()
        } finally {
            pending = null
            hide()
        }
    }

    /** Answers the waiting ask; false when none waits. */
    fun answer(value: T?): Boolean = pending?.complete(value) ?: false
}
