package app.dailey.czn.companion.run

import androidx.room3.immediateTransaction
import androidx.room3.useWriterConnection
import app.dailey.czn.companion.ask.AnsweredAsk
import app.dailey.czn.companion.ask.PlayerAsks
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
    /** Questions Claude asked the player during an assistant reply, with the answers. */
    val asks: List<AnsweredAsk> = emptyList(),
    /** What the app found on a user message's screenshot (see [RunEntryRow.screen]). */
    val screen: String = "",
    /** An assistant reply's whole exchange as JSON (see [RunEntryRow.exchange]). */
    val exchange: String = "",
    /** What the reply's run state updates changed (see [RunEntryRow.changes]). */
    val changes: List<String> = emptyList(),
)

/** The exchange in progress in the active run; see [OpenTurnRow]. */
class OpenTurn(
    val question: String,
    val screen: String,
    val jpeg: ByteArray?,
    /** The full-size screenshot's file name in [ScreenshotArchive]; empty without one. */
    val screenshot: String,
    val reply: String,
    val tools: List<String>,
    val asks: List<AnsweredAsk>,
    /** JSON array of SDK message params; see [OpenTurnRow.turn]. */
    val turn: String,
    val changes: List<String> = emptyList(),
)

/** The active run's conversation, with any exchange in progress. Survives restarts until [newRun]. */
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

    /** The active run's deck state; null until Claude saves one, and again after [newRun]. */
    val deck: Flow<RunDeck?> = dao.activeDeck().map { it?.let(RunDecks::decode) }

    suspend fun deckNow(): RunDeck? = dao.activeRun()?.deck?.let(RunDecks::decode)

    /**
     * Saves [change] of the active run's deck state (null while none is saved) in one
     * transaction; the states before and after it.
     */
    suspend fun changeDeck(change: (RunDeck?) -> RunDeck): Pair<RunDeck?, RunDeck> =
        db.useWriterConnection { connection ->
            connection.immediateTransaction {
                val runId = activeRunId()
                val saved = dao.activeRun()?.deck?.let(RunDecks::decode)
                val changed = change(saved)
                dao.setDeck(runId, RunDecks.encode(changed))
                saved to changed
            }
        }

    /** The newest response id of the active run's requests, for cache diagnostics. */
    suspend fun lastMessageId(): String? = dao.activeRun()?.let { dao.lastMessageId(it.id) }

    /** Saves one request's usage in the active run; the row's runId is replaced. */
    suspend fun saveUsage(usage: RequestUsageRow) =
        db.useWriterConnection { connection ->
            connection.immediateTransaction { dao.insertUsage(usage.copy(runId = activeRunId())) }
        }

    suspend fun entriesNow(): List<RunEntry> =
        dao
            .activeRun()
            ?.let {
                dao.entriesNow(it.id).map(::toEntry)
            }.orEmpty()

    suspend fun openTurn(): OpenTurn? =
        dao.activeRun()?.let { dao.openTurn(it.id) }?.let { row ->
            OpenTurn(
                row.question,
                row.screen,
                row.jpeg,
                row.screenshot,
                row.reply,
                splitTools(row.tools),
                PlayerAsks.decode(row.asks),
                row.turn,
                splitTools(row.changes),
            )
        }

    /** Replaces the active run's open exchange with [turn]. */
    suspend fun saveOpenTurn(turn: OpenTurn) =
        db.useWriterConnection { connection ->
            connection.immediateTransaction {
                dao.saveOpenTurn(
                    OpenTurnRow(
                        runId = activeRunId(),
                        question = turn.question,
                        screen = turn.screen,
                        jpeg = turn.jpeg,
                        screenshot = turn.screenshot,
                        reply = turn.reply,
                        tools = joinTools(turn.tools),
                        asks = PlayerAsks.encode(turn.asks),
                        changes = joinTools(turn.changes),
                        turn = turn.turn,
                    ),
                )
            }
        }

    suspend fun discardOpenTurn() =
        db.useWriterConnection { connection ->
            connection.immediateTransaction { dao.activeRun()?.let { dao.deleteOpenTurn(it.id) } }
        }

    /**
     * Saves one completed exchange and closes the open one; both messages are written or neither
     * is. [screenshot] and [exchange] are kept for debugging (see [RunEntryRow]).
     */
    suspend fun appendExchange(
        user: String,
        assistant: String,
        tools: List<String>,
        asks: List<AnsweredAsk> = emptyList(),
        screen: String = "",
        screenshot: String = "",
        exchange: String = "",
        changes: List<String> = emptyList(),
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
                        screen = screen,
                        screenshot = screenshot,
                    ),
                    RunEntryRow(
                        runId = runId,
                        position = next + 1,
                        kind = ASSISTANT,
                        text = assistant,
                        createdAt = time,
                        tools = joinTools(tools),
                        asks = PlayerAsks.encode(asks),
                        exchange = exchange,
                        changes = joinTools(changes),
                    ),
                ),
            )
            dao.deleteOpenTurn(runId)
        }
    }

    suspend fun newRun() =
        db.useWriterConnection { connection ->
            connection.immediateTransaction {
                // A closed run cannot resume its open exchange; drop it with its screenshot.
                dao.activeRun()?.let { dao.deleteOpenTurn(it.id) }
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
            splitTools(row.tools),
            PlayerAsks.decode(row.asks),
            row.screen,
            row.exchange,
            splitTools(row.changes),
        )

    private fun joinTools(tools: List<String>) = tools.joinToString("\n") { it.replace('\n', ' ') }

    private fun splitTools(tools: String) = tools.lines().filter(String::isNotBlank)

    private companion object {
        const val USER = "user"
        const val ASSISTANT = "assistant"
    }
}
