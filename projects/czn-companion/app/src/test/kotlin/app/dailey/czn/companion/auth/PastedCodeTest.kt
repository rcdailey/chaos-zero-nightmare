package app.dailey.czn.companion.auth

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class PastedCodeTest {
    @Test
    fun `code and state as shown on the callback page`() {
        assertEquals(PastedCode("abc", "xyz"), parsePastedCode("  abc#xyz\n").getOrThrow())
    }

    @Test
    fun `full callback url`() {
        val url = "https://platform.claude.com/oauth/code/callback?code=abc&state=xyz"
        assertEquals(PastedCode("abc", "xyz"), parsePastedCode(url).getOrThrow())
    }

    @Test
    fun `query string`() {
        assertEquals(PastedCode("abc", "xyz"), parsePastedCode("code=abc&state=xyz").getOrThrow())
    }

    @Test
    fun `bare code`() {
        assertEquals(PastedCode("abc", null), parsePastedCode("abc").getOrThrow())
    }

    @Test
    fun `empty input and url without code are rejected`() {
        assertTrue(parsePastedCode("  ").isFailure)
        assertTrue(parsePastedCode("https://platform.claude.com/oauth/code/callback?x=1").isFailure)
    }
}
