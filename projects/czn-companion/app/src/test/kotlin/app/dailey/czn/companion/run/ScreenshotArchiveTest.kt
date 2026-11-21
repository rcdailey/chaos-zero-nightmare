package app.dailey.czn.companion.run

import org.junit.jupiter.api.Assertions.assertArrayEquals
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.io.File

/** Debugging and resumed exchanges read screenshots back; only the newest are kept. */
class ScreenshotArchiveTest {
    @TempDir
    lateinit var dir: File

    @Test
    fun `keeps the newest screenshots with their identify answers and drops older ones`() {
        var time = 1_000L
        val archive = ScreenshotArchive(dir) { time++ }
        val first = archive.save(byteArrayOf(0))
        archive.saveIdentify(first, "{}")
        val names = (1..ScreenshotArchive.KEEP).map { archive.save(byteArrayOf(it.toByte())) }

        assertNull(archive.read(first))
        assertArrayEquals(byteArrayOf(1), archive.read(names.first()))
        assertArrayEquals(byteArrayOf(ScreenshotArchive.KEEP.toByte()), archive.read(names.last()))
        assertEquals(ScreenshotArchive.KEEP, dir.listFiles()!!.size)
    }
}
