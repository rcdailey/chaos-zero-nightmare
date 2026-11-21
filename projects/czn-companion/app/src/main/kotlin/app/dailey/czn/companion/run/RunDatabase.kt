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

/** One message in a run's conversation, in order. Text only; screenshots are never stored. */
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
}

@Database(
    entities = [RunRow::class, RunEntryRow::class],
    version = 3,
    autoMigrations = [AutoMigration(from = 1, to = 2), AutoMigration(from = 2, to = 3)],
)
abstract class RunDatabase : RoomDatabase() {
    abstract fun runDao(): RunDao
}
