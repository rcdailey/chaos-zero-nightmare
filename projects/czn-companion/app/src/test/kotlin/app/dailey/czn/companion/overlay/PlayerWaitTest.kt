package app.dailey.czn.companion.overlay

import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runTest
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** How a reply waits for the player: what each ask kind (pick, question) relies on. */
class PlayerWaitTest {
    private val wait = PlayerWait<String>()
    private var shown = false

    /** Starts a waiting reply; it runs until it suspends, so the ask is shown on return. */
    private fun TestScope.ask() =
        async(start = CoroutineStart.UNDISPATCHED) {
            wait.await(show = { shown = true }, hide = { shown = false })
        }

    @Test
    fun `the answer resumes the reply and hides the ask`() =
        runTest {
            val reply = ask()
            assertTrue(shown)
            assertTrue(wait.answer("Mika"))
            assertEquals("Mika", reply.await())
            assertFalse(shown)
        }

    @Test
    fun `a skip resumes the reply with no answer`() =
        runTest {
            val reply = ask()
            wait.answer(null)
            assertNull(reply.await())
            assertFalse(shown)
        }

    @Test
    fun `stopping the reply hides the ask and drops a late answer`() =
        runTest {
            val reply = ask()
            reply.cancelAndJoin()
            assertFalse(shown)
            assertFalse(wait.answer("Mika"))
        }

    @Test
    fun `an answer with nothing waiting is refused`() {
        assertFalse(wait.answer("Mika"))
    }
}
