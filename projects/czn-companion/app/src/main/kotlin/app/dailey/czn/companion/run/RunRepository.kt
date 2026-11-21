package app.dailey.czn.companion.run

import androidx.room3.immediateTransaction
import androidx.room3.useWriterConnection
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.map

enum class EntryKind { User, Assistant }

data class RunEntry(
    val kind: EntryKind,
    val text: String,
    /** CZN data tool calls behind an assistant reply, for display only. */
    val tools: List<String> = emptyList(),
)

/** The active run's conversation. Survives restarts until [newRun]. */
class RunRepository(
    private val db: RunDatabase,
    private val now: () -> Long = System::currentTimeMillis,
) {
    private val dao = db.runDao()

    @OptIn(ExperimentalCoroutinesApi::class)
    val entries: Flow<List<RunEntry>> =
        dao.activeRunId().flatMapLatest { id ->
            if (id ==
                null
            ) {
                flowOf(emptyList())
            } else {
                dao.entries(id).map { rows -> rows.map(::toEntry) }
            }
        }

    /** The active run's character; null until one is set, and again after [newRun]. */
    val character: Flow<String?> = dao.activeCharacter()

    suspend fun characterNow(): String? = dao.activeRun()?.character

    suspend fun setCharacter(name: String) =
        db.useWriterConnection { connection ->
            connection.immediateTransaction { dao.setCharacter(activeRunId(), name) }
        }

    suspend fun entriesNow(): List<RunEntry> =
        dao
            .activeRun()
            ?.let {
                dao.entriesNow(it.id).map(::toEntry)
            }.orEmpty()

    /** Saves one completed exchange; both messages are written or neither is. */
    suspend fun appendExchange(
        user: String,
        assistant: String,
        tools: List<String>,
    ) = db.useWriterConnection { connection ->
        connection.immediateTransaction {
            val runId = activeRunId()
            val next = dao.lastPosition(runId) + 1
            val time = now()
            dao.insertEntries(
                listOf(
                    RunEntryRow(
                        runId = runId,
                        position = next,
                        kind = USER,
                        text = user,
                        createdAt = time,
                    ),
                    RunEntryRow(
                        runId = runId,
                        position = next + 1,
                        kind = ASSISTANT,
                        text = assistant,
                        createdAt = time,
                        tools = tools.joinToString("\n") { it.replace('\n', ' ') },
                    ),
                ),
            )
        }
    }

    suspend fun newRun() =
        db.useWriterConnection { connection ->
            connection.immediateTransaction {
                dao.closeActiveRun()
                dao.insertRun(RunRow(startedAt = now(), active = true))
            }
        }

    /** The active run, started first if there is none; call inside a write transaction. */
    private suspend fun activeRunId() =
        dao.activeRun()?.id ?: dao.insertRun(RunRow(startedAt = now(), active = true))

    private fun toEntry(row: RunEntryRow) =
        RunEntry(
            if (row.kind == USER) EntryKind.User else EntryKind.Assistant,
            row.text,
            row.tools.lines().filter(String::isNotBlank),
        )

    private companion object {
        const val USER = "user"
        const val ASSISTANT = "assistant"
    }
}
