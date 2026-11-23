package app.dailey.czn.companion.run

import android.content.Context
import androidx.room3.Room
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
    fun aVersion9RunKeepsItsCharacterAndPlanAndHasNoDeckState() =
        runTest {
            createAt(
                9,
                "INSERT INTO run (id, startedAt, active, character, plan)" +
                    " VALUES (1, 0, 1, 'Rossweisse', 'Radiant Sword Flow II x3')",
            )

            // Opened as the app opens it, so every migration since version 9 runs.
            val db =
                Room
                    .databaseBuilder<RunDatabase>(context, path)
                    .setDriver(AndroidSQLiteDriver())
                    .build()
            val run = RunRepository(db)

            assertEquals("Rossweisse", run.characterNow())
            assertEquals("Radiant Sword Flow II x3", run.planNow())
            assertNull(run.deckNow())
            db.close()
        }
}
