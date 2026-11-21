package app.dailey.czn.companion.overlay

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.view.Display
import androidx.core.graphics.scale
import app.dailey.czn.companion.claude.Screenshot
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import java.io.ByteArrayOutputStream
import kotlin.coroutines.resume
import kotlin.math.max
import kotlin.math.roundToInt

/** Full-display screenshots have no secure-window failure; protected content is blacked out. */
enum class CaptureFailure { RateLimited, Other }

class CaptureException(
    val kind: CaptureFailure,
) : Exception("Screenshot failed: $kind")

/**
 * Screenshot of the default display as a software bitmap. Android rate-limits accessibility
 * screenshots; a rate-limited call is retried once after a short wait.
 */
suspend fun AccessibilityService.capture(): Result<Bitmap> {
    val first = captureOnce()
    if ((first.exceptionOrNull() as? CaptureException)?.kind !=
        CaptureFailure.RateLimited
    ) {
        return first
    }
    delay(RATE_LIMIT_WAIT_MILLIS)
    return captureOnce()
}

private suspend fun AccessibilityService.captureOnce(): Result<Bitmap> =
    suspendCancellableCoroutine { continuation ->
        takeScreenshot(
            Display.DEFAULT_DISPLAY,
            mainExecutor,
            object : AccessibilityService.TakeScreenshotCallback {
                override fun onSuccess(screenshot: AccessibilityService.ScreenshotResult) {
                    val buffer = screenshot.hardwareBuffer
                    val bitmap =
                        buffer.use {
                            Bitmap
                                .wrapHardwareBuffer(it, screenshot.colorSpace)
                                ?.copy(Bitmap.Config.ARGB_8888, false)
                        }
                    continuation.resume(
                        bitmap?.let { Result.success(it) }
                            ?: Result.failure(CaptureException(CaptureFailure.Other)),
                    )
                }

                override fun onFailure(errorCode: Int) {
                    val kind =
                        if (errorCode ==
                            AccessibilityService.ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT
                        ) {
                            CaptureFailure.RateLimited
                        } else {
                            CaptureFailure.Other
                        }
                    continuation.resume(Result.failure(CaptureException(kind)))
                }
            },
        )
    }

/** The two JPEGs of one screenshot: Claude's copy, and full resolution for naming characters. */
fun Bitmap.toScreenshot() = Screenshot(toJpeg(), toJpeg(maxLongSide = Int.MAX_VALUE, quality = 90))

/**
 * JPEG scaled so the long side is at most [maxLongSide] pixels, the size sent to Claude. 1280 read
 * every fact on a dense save-data screen at about 1,330 image tokens; 1024 misread numbers.
 */
fun Bitmap.toJpeg(
    maxLongSide: Int = 1280,
    quality: Int = 85,
): ByteArray {
    val scale = maxLongSide.toFloat() / max(width, height)
    val scaled =
        if (scale < 1f) {
            scale((width * scale).roundToInt(), (height * scale).roundToInt())
        } else {
            this
        }
    return ByteArrayOutputStream()
        .also { scaled.compress(Bitmap.CompressFormat.JPEG, quality, it) }
        .toByteArray()
}

private const val RATE_LIMIT_WAIT_MILLIS = 1_000L
