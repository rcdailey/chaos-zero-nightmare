package app.dailey.czn.companion.run

import android.content.Context
import androidx.sqlite.driver.AndroidSQLiteDriver
import androidx.sqlite.execSQL
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File

/**
 * Saved runs survive each schema upgrade; a lost migration would wipe the player's runs. Runs on
 * the JVM under Robolectric, never on a device: device test runs uninstall the app afterwards,
 * which deletes its data.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [36])
class RunDatabaseMigrationTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val path = context.getDatabasePath("migration-test.db").absolutePath

    /** A database as a build at [version] created it, from its exported schema, plus [rows]. */
    private fun createAt(
        version: Int,
        vararg rows: String,
    ) {
        val schema =
            Json
                .parseToJsonElement(
                    File("schemas/${RunDatabase::class.qualifiedName}/$version.json").readText(),
                ).jsonObject["database"]!!
                .jsonObject
        File(path).parentFile!!.mkdirs()
        AndroidSQLiteDriver().open(path).use { connection ->
            for (entity in schema["entities"]!!.jsonArray.map { it.jsonObject }) {
                val table = entity["tableName"]!!.jsonPrimitive.content

                fun sql(element: JsonElement) =
                    element.jsonPrimitive.content.replace("\${TABLE_NAME}", table)
                connection.execSQL(sql(entity["createSql"]!!))
                entity["indices"]?.jsonArray?.forEach {
                    connection.execSQL(sql(it.jsonObject["createSql"]!!))
                }
            }
            schema["setupQueries"]!!.jsonArray.forEach {
                connection.execSQL(
                    it.jsonPrimitive.content,
                )
            }
            rows.forEach(connection::execSQL)
            connection.execSQL("PRAGMA user_version = $version")
        }
    }

    @Test
    fun aVersion9RunKeepsItsCharacterAndConversationButStartsWithoutDeckState() =
        runTest {
            createAt(
                9,
                "INSERT INTO run (id, startedAt, active, character, plan)" +
                    " VALUES (1, 0, 1, 'Rossweisse', 'Radiant Sword Flow II x3')",
                "INSERT INTO run_entry (runId, position, kind, text, createdAt)" +
                    " VALUES (1, 0, 'user', 'Which card?', 0)",
            )

            // Opened as the app opens it, so every migration since version 9 runs.
            val db = RunDatabase.open(context, path)
            val run = RunRepository(db)

            assertEquals("Rossweisse", run.characterNow())
            assertEquals(listOf("Which card?"), run.entriesNow().map { it.text })
            assertNull(run.deckNow())
            db.close()
        }

    @Test
    fun aVersion11RunDropsItsOldDeckStateAndKeepsItsConversation() =
        runTest {
            createAt(
                11,
                "INSERT INTO run (id, startedAt, active, character, plan, deck)" +
                    " VALUES (1, 0, 1, 'Rossweisse', 'old plan'," +
                    " '{\"plan\": \"old\", \"characters\": [{\"name\": \"Rossweisse\"}]}')",
                "INSERT INTO run_entry (runId, position, kind, text, createdAt)" +
                    " VALUES (1, 0, 'user', 'Which card?', 0)",
            )

            val db = RunDatabase.open(context, path)
            val run = RunRepository(db)

            assertEquals("Rossweisse", run.characterNow())
            assertEquals(listOf("Which card?"), run.entriesNow().map { it.text })
            assertNull(run.deckNow())
            db.close()
        }

    @Test
    fun aVersion12ReplyInProgressKeepsItsScreenshot() =
        runTest {
            createAt(
                12,
                "INSERT INTO run (id, startedAt, active) VALUES (1, 0, 1)",
                "INSERT INTO open_turn (runId, question, screen, jpeg, screenshot, reply, tools," +
                    " asks, changes, turn) VALUES (1, 'Which card?', '', x'0102', '1000.jpg'," +
                    " 'Take', '', '[]', '', '[]')",
            )

            val db = RunDatabase.open(context, path)
            val open = RunRepository(db).openTurn()!!

            assertEquals("Which card?", open.question)
            assertEquals("Take", open.reply)
            assertEquals(listOf("1000.jpg"), open.shots.map { it.screenshot })
            assertEquals(listOf(listOf<Byte>(1, 2)), open.shots.map { it.jpeg.toList() })
            db.close()
        }

    @Test
    fun aReplyInProgressKeepsItsScreenshotsInOrderUntilItIsSaved() =
        runTest {
            val db = RunDatabase.open(context, path)
            val run = RunRepository(db)
            val shots = listOf(OpenShot(byteArrayOf(1), "1.jpg"), OpenShot(byteArrayOf(2), "2.jpg"))
            run.startOpenTurn(OpenTurn("Which?", "", shots, "", emptyList(), emptyList(), "[]"))
            run.saveOpenTurn(OpenTurn("Which?", "", shots, "Take", emptyList(), emptyList(), "[]"))

            val open = run.openTurn()!!
            assertEquals("Take", open.reply)
            assertEquals(listOf("1.jpg", "2.jpg"), open.shots.map { it.screenshot })

            run.appendExchange("Which?", "Take B.", emptyList(), screenshots = listOf("1.jpg"))
            assertNull(run.openTurn())
            db.close()
        }
}
