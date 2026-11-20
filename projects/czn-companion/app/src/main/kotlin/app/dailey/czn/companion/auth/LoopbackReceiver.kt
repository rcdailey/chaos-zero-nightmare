package app.dailey.czn.companion.auth

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.Closeable
import java.net.InetAddress
import java.net.ServerSocket
import java.net.URLDecoder

/**
 * Receives one OAuth redirect on a local port (RFC 8252 loopback redirect), so the user does not
 * have to copy a code out of the browser. The page it serves sends the browser on to
 * [returnUri], which closes an Auth Tab and returns to the app.
 */
class LoopbackReceiver private constructor(
    private val server: ServerSocket,
    private val returnUri: String,
) : Closeable {
    val redirectUri = "http://localhost:${server.localPort}$PATH"

    /**
     * Waits for the browser to load [redirectUri] and returns its query parameters. Accepting a
     * connection blocks a thread and ignores coroutine cancellation; [close] ends the wait.
     */
    suspend fun await(): Map<String, String> =
        withContext(Dispatchers.IO) {
            var params: Map<String, String>? = null
            while (params == null) {
                server.accept().use { socket ->
                    val requestLine =
                        socket
                            .getInputStream()
                            .bufferedReader()
                            .readLine()
                            .orEmpty()
                    val target = requestLine.split(' ').getOrNull(1).orEmpty()
                    val found = target.substringBefore('?') == PATH
                    socket.getOutputStream().write(response(found).toByteArray())
                    if (found) params = parseQuery(target.substringAfter('?', ""))
                }
            }
            params
        }

    override fun close() = server.close()

    private fun response(found: Boolean): String {
        val body =
            if (found) {
                """<!doctype html><meta name="viewport" content="width=device-width">
                |<p>Signed in. Returning to CZN Companion...</p>
                |<p><a href="$returnUri">Return to the app</a></p>
                |<script>location.href = "$returnUri"</script>
                """.trimMargin()
            } else {
                "Not found"
            }
        val status = if (found) "200 OK" else "404 Not Found"
        return "HTTP/1.1 $status\r\nContent-Type: text/html; charset=utf-8\r\n" +
            "Content-Length: ${body.toByteArray().size}\r\nConnection: close\r\n\r\n$body"
    }

    companion object {
        private const val PATH = "/callback"

        /** Binds a free port on the loopback interface. */
        fun open(returnUri: String): LoopbackReceiver =
            LoopbackReceiver(ServerSocket(0, 1, InetAddress.getLoopbackAddress()), returnUri)

        internal fun parseQuery(query: String): Map<String, String> =
            query
                .split('&')
                .filter { '=' in it }
                .associate { pair ->
                    val (name, value) = pair.split('=', limit = 2)
                    URLDecoder.decode(name, Charsets.UTF_8) to
                        URLDecoder.decode(value, Charsets.UTF_8)
                }
    }
}
