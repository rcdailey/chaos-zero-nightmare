package app.dailey.czn.companion.auth

import java.security.MessageDigest
import java.security.SecureRandom
import kotlin.io.encoding.Base64

/** RFC 7636 proof key: the verifier stays on the device, the challenge goes to the browser. */
class Pkce private constructor(
    val verifier: String,
) {
    val challenge: String =
        base64Url.encode(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()))

    companion object {
        private val base64Url = Base64.UrlSafe.withPadding(Base64.PaddingOption.ABSENT)

        fun create(): Pkce = Pkce(base64Url.encode(ByteArray(32).also(SecureRandom()::nextBytes)))

        fun fromVerifier(verifier: String): Pkce = Pkce(verifier)
    }
}
