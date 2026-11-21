package app.dailey.czn.companion.run

import androidx.room3.AutoMigration
import androidx.room3.ColumnInfo
import androidx.room3.Dao
import androidx.room3.Database
import androidx.room3.Entity
import androidx.room3.ForeignKey
import androidx.room3.Index
import androidx.room3.Insert
import androidx.room3.PrimaryKey
import androidx.room3.Query
import androidx.room3.RoomDatabase
import androidx.room3.Upsert
import androidx.room3.migration.AutoMigrationSpec
import androidx.sqlite.SQLiteConnection
import androidx.sqlite.execSQL
import kotlinx.coroutines.flow.Flow

/** One game run. Exactly one run is active; New run closes it and starts another. */
@Entity(tableName = "run")
data class RunRow(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val startedAt: Long,
    val active: Boolean,
    /** The character whose deck this run builds, as the CZN data server names it. */
    val character: String? = null,
)

/**
 * The exchange in progress in a run, saved after every request and every answered app tool, so a
 * reply survives the app being killed (Android ends it freely while another app is in front).
 * Deleted when the reply completes, fails, or is stopped; a run has at most one.
 */
@Entity(
    tableName = "open_turn",
    foreignKeys = [ForeignKey(RunRow::class, ["id"], ["runId"], onDelete = ForeignKey.CASCADE)],
)
class OpenTurnRow(
    @PrimaryKey val runId: Long,
    /** The player's typed text, empty for a screen read. */
    val question: String,
    /** What the app found on the screenshot; see [RunEntryRow.screen]. */
    val screen: String,
    /** The screenshot Claude reads, kept only while this exchange is open. */
    val jpeg: ByteArray?,
    /** As in [RunEntryRow.screenshot]; the app names characters from this full-size copy. */
    @ColumnInfo(defaultValue = "") val screenshot: String,
    /** Reply text so far. */
    val reply: String,
    /** As in [RunEntryRow.tools] and [RunEntryRow.asks]. */
    val tools: String,
    val asks: String,
    /**
     * Messages Claude and the app exchanged after the question, as a JSON array of the Anthropic
     * SDK's message params. When the last is Claude's and calls app tools, those tools still
     * wait for their results.
     */
    val turn: String,
)

/**
 * One message in a run's conversation, in order. Text only; a screenshot is kept only in
 * [OpenTurnRow] while its exchange is open.
 */
@Entity(
    tableName = "run_entry",
    foreignKeys = [ForeignKey(RunRow::class, ["id"], ["runId"], onDelete = ForeignKey.CASCADE)],
    indices = [Index("runId", "position", unique = true)],
)
data class RunEntryRow(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val runId: Long,
    val position: Int,
    val kind: String,
    val text: String,
    val createdAt: Long,
    /** CZN data tool calls behind an assistant reply, one label per line; shown, never sent. */
    @ColumnInfo(defaultValue = "") val tools: String = "",
    /** Questions Claude asked the player during an assistant reply, as `PlayerAsks.encode` JSON. */
    @ColumnInfo(defaultValue = "") val asks: String = "",
    /**
     * What the app found on a user message's screenshot, such as the characters the data server
     * named; sent to Claude with the message and shown in the reply's details.
     */
    @ColumnInfo(defaultValue = "") val screen: String = "",
    /**
     * A user message's screenshot in [app.dailey.czn.companion.run.ScreenshotArchive], by file
     * name; empty without one. The archive keeps only the newest, so the file may be gone.
     */
    @ColumnInfo(defaultValue = "") val screenshot: String = "",
    /**
     * Everything Claude and the app exchanged for an assistant reply, as in [OpenTurnRow.turn]
     * plus Claude's last message: every tool call with its full input and result. Kept for
     * debugging only; never sent or shown.
     */
    @ColumnInfo(defaultValue = "") val exchange: String = "",
)

@Dao
interface RunDao {
    @Query("SELECT * FROM run WHERE active = 1 LIMIT 1")
    suspend fun activeRun(): RunRow?

    @Query("SELECT id FROM run WHERE active = 1 LIMIT 1")
    fun activeRunId(): Flow<Long?>

    @Query("SELECT character FROM run WHERE active = 1 LIMIT 1")
    fun activeCharacter(): Flow<String?>

    @Query("UPDATE run SET character = :character WHERE id = :runId")
    suspend fun setCharacter(
        runId: Long,
        character: String,
    )

    @Insert
    suspend fun insertRun(run: RunRow): Long

    @Query("UPDATE run SET active = 0 WHERE active = 1")
    suspend fun closeActiveRun()

    @Query("SELECT * FROM run_entry WHERE runId = :runId ORDER BY position")
    fun entries(runId: Long): Flow<List<RunEntryRow>>

    @Query("SELECT * FROM run_entry WHERE runId = :runId ORDER BY position")
    suspend fun entriesNow(runId: Long): List<RunEntryRow>

    @Query("SELECT COALESCE(MAX(position), -1) FROM run_entry WHERE runId = :runId")
    suspend fun lastPosition(runId: Long): Int

    @Insert
    suspend fun insertEntries(entries: List<RunEntryRow>)

    @Query("SELECT * FROM open_turn WHERE runId = :runId")
    suspend fun openTurn(runId: Long): OpenTurnRow?

    @Upsert
    suspend fun saveOpenTurn(turn: OpenTurnRow)

    @Query("DELETE FROM open_turn WHERE runId = :runId")
    suspend fun deleteOpenTurn(runId: Long)
}

@Database(
    entities = [RunRow::class, RunEntryRow::class, OpenTurnRow::class],
    version = 8,
    autoMigrations = [
        AutoMigration(from = 1, to = 2),
        AutoMigration(from = 2, to = 3),
        AutoMigration(from = 3, to = 4),
        AutoMigration(from = 4, to = 5),
        AutoMigration(from = 5, to = 6),
        AutoMigration(from = 6, to = 7, spec = MoveNamesToScreen::class),
        AutoMigration(from = 7, to = 8),
    ],
)
abstract class RunDatabase : RoomDatabase() {
    abstract fun runDao(): RunDao
}

/**
 * Before [RunEntryRow.screen] existed, the app saved the names line inside the player's message,
 * after a blank line. Moves it to [RunEntryRow.screen] so every reply shows it the same way.
 */
class MoveNamesToScreen : AutoMigrationSpec {
    override suspend fun onPostMigrate(connection: SQLiteConnection) {
        val at = "instr(text, '$NAMES_PREFIX')"
        connection.execSQL(
            """
            UPDATE run_entry SET
                screen = substr(text, $at),
                text = rtrim(substr(text, 1, $at - 1), char(10))
            WHERE kind = 'user' AND screen = '' AND $at > 0
            """.trimIndent(),
        )
    }

    private companion object {
        const val NAMES_PREFIX = "Characters on screen, top to bottom, left to right: "
    }
}
