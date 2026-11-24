package app.dailey.czn.companion.run

import java.io.File

/**
 * The newest [KEEP] screenshots at full resolution, each with what the data server found on it,
 * so a run can be debugged from the device after its replies finish. An open exchange also reads
 * its screenshot back from here to name the characters on it, even after the app was killed.
 *
 * Files are `<millis>.jpg` and `<millis>.identify.json` (the screenshot's entry in the raw
 * `POST /identify-screenshots` answer: characters and gear), so file names sort by age.
 */
class ScreenshotArchive(
    private val dir: File,
    private val now: () -> Long = System::currentTimeMillis,
) {
    /**
     * Saves [jpeg], drops the screenshots beyond the newest [KEEP], and returns its file name,
     * unique even for saves within one millisecond.
     */
    fun save(jpeg: ByteArray): String {
        dir.mkdirs()
        val stamp = generateSequence(now()) { it + 1 }.first { !File(dir, "$it.jpg").exists() }
        val name = "$stamp.jpg"
        File(dir, name).writeBytes(jpeg)
        val stems =
            dir
                .listFiles()
                .orEmpty()
                .map { it.name.substringBefore('.') }
                .distinct()
        val kept = stems.sortedDescending().take(KEEP).toSet()
        dir.listFiles().orEmpty().filter { it.name.substringBefore('.') !in kept }.forEach {
            it.delete()
        }
        return name
    }

    /** The screenshot saved as [name], or null when it has been dropped. */
    fun read(name: String): ByteArray? = File(dir, name).takeIf(File::isFile)?.readBytes()

    /** Keeps the data server's answer for the screenshot saved as [name]. */
    fun saveIdentify(
        name: String,
        response: String,
    ) {
        File(dir, "${name.substringBefore('.')}.identify.json").writeText(response)
    }

    companion object {
        const val KEEP = 20
    }
}
