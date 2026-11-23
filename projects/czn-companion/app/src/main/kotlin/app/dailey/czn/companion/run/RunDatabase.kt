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
    /**
     * The target deck Claude saved with the retired set_run_plan tool; runs saved before [deck]
     * existed still send it.
     */
    val plan: String? = null,
    /** The run's deck state Claude saved (update_run_state), as [RunDecks.encode] JSON. */
    val deck: String? = null,
)

/**
 * Token use and cache behavior of one Messages API request, kept for diagnosing cost and
 * caching; never sent or shown. Token fields are the response's `usage`: [inputTokens] counts
 * only the tokens after the last cache breakpoint, so a request's whole prompt is
 * [inputTokens] + [cacheWrite5mTokens] + [cacheWrite1hTokens] + [cacheReadTokens].
 */
@Entity(
    tableName = "request_usage",
    foreignKeys = [ForeignKey(RunRow::class, ["id"], ["runId"], onDelete = ForeignKey.CASCADE)],
    indices = [Index("runId")],
)
data class RequestUsageRow(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val runId: Long,
    /** When the request started, in epoch milliseconds. */
    val startedAt: Long,
    val durationMs: Long,
    /** The response's message id; the next request names it for cache diagnostics. */
    val messageId: String,
    /** 1 for a turn's first request, then one more per request after tool calls. */
    val round: Int,
    val stopReason: String,
    /** Messages sent, counting the history and this turn's exchange. */
    val messages: Int,
    val inputTokens: Long,
    val cacheWrite5mTokens: Long,
    val cacheWrite1hTokens: Long,
    val cacheReadTokens: Long,
    val outputTokens: Long,
    /** Old tool results the API cleared from this request (context editing), and their size. */
    val clearedToolUses: Long,
    val clearedInputTokens: Long,
    /**
     * The API's reason the prompt diverged from the previous request's (cache diagnostics),
     * such as `messages_changed`; empty when nothing diverged or no comparison was made.
     */
    val cacheMissReason: String,
    val cacheMissedTokens: Long,
    /**
     * Sampling passes the API ran for this request: one, plus one after each round of CZN data
     * tool calls it ran itself. The token fields above add up every pass, so a request with 3
     * passes reports about 3 times its prompt; 0 when the API reported no passes, and for rows saved
     * before this was kept.
     */
    @ColumnInfo(defaultValue = "0") val iterations: Int = 0,
    /** The largest single pass's prompt, which is what the context-editing trigger compares. */
    @ColumnInfo(defaultValue = "0") val largestPromptTokens: Long = 0,
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
 * One message in a run's conversation, in order. A screenshot is sent only while its exchange
 * is open ([OpenTurnRow]); later requests carry the message without it.
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
     * plus Claude's last message: every tool call with its full input and result. Later requests
     * send it as the reply (see `MessagesRequest`), so Claude keeps its earlier lookups; empty
     * for replies saved before it existed, which are sent as [text].
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

    @Query("UPDATE run SET deck = :deck WHERE id = :runId")
    suspend fun setDeck(
        runId: Long,
        deck: String,
    )

    @Insert
    suspend fun insertUsage(usage: RequestUsageRow)

    @Query("SELECT messageId FROM request_usage WHERE runId = :runId ORDER BY id DESC LIMIT 1")
    suspend fun lastMessageId(runId: Long): String?

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
    entities = [RunRow::class, RunEntryRow::class, OpenTurnRow::class, RequestUsageRow::class],
    version = 11,
    autoMigrations = [
        AutoMigration(from = 1, to = 2),
        AutoMigration(from = 2, to = 3),
        AutoMigration(from = 3, to = 4),
        AutoMigration(from = 4, to = 5),
        AutoMigration(from = 5, to = 6),
        AutoMigration(from = 6, to = 7, spec = MoveNamesToScreen::class),
        AutoMigration(from = 7, to = 8),
        AutoMigration(from = 8, to = 9),
        AutoMigration(from = 9, to = 10),
        AutoMigration(from = 10, to = 11),
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
