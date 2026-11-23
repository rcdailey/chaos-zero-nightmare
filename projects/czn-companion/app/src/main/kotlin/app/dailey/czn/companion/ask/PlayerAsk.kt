package app.dailey.czn.companion.ask

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

/**
 * One question Claude asks the player mid-reply, shown as a card in the conversation. The same
 * shape is Claude's `ask_player` input, the card's content, and what the run saves, so the field
 * names are the tool's wire names.
 */
@Serializable
data class Question(
    /** The full question, which may wrap over several lines. */
    val question: String,
    /** A short label for the question, shown in the step line and the answer summary. */
    val header: String,
    val options: List<Option> = emptyList(),
    @SerialName("multi_select") val multiSelect: Boolean = false,
    /**
     * Asks for the run's character: the options are character names, shown with their card
     * art, and the pick is saved as the run's character. Without options the player picks
     * from the whole roster, which only a question asked alone can do.
     */
    val characters: Boolean = false,
)

@Serializable
data class Option(
    val label: String,
    val description: String? = null,
    /** Claude's suggestion; the card marks it in words, never by color alone. */
    val recommended: Boolean = false,
)

/** The player's answer to one question: the labels they picked and any text they typed. */
@Serializable
data class Answer(
    val picked: List<String> = emptyList(),
    val typed: String? = null,
)

/**
 * A set of questions Claude asked during a reply, and the player's answers, or null when the
 * player skipped them. [offset] is the length of the reply text when Claude asked, which places
 * the card between the text before and after it.
 */
@Serializable
data class AnsweredAsk(
    val offset: Int,
    val questions: List<Question>,
    val answers: List<Answer>?,
)

/** Rules and text formats for asks: Claude's input, Claude's results, and the saved form. */
object PlayerAsks {
    const val MAX_QUESTIONS = 4
    const val MIN_OPTIONS = 2
    const val MAX_OPTIONS = 4

    private val json =
        Json {
            ignoreUnknownKeys = true
            explicitNulls = false
        }

    @Serializable
    private data class Input(
        val questions: List<Question>,
    )

    /**
     * Reads Claude's `ask_player` input. Fails with a message for Claude when the input breaks
     * the limits the card is sized for.
     */
    fun parse(inputJson: String): Result<List<Question>> {
        val questions =
            try {
                json.decodeFromString<Input>(inputJson).questions
            } catch (e: SerializationException) {
                return Result.failure(IllegalArgumentException("Invalid input: ${e.message}"))
            } catch (e: IllegalArgumentException) {
                return Result.failure(IllegalArgumentException("Invalid input: ${e.message}"))
            }
        val problem =
            when {
                questions.size !in 1..MAX_QUESTIONS -> {
                    "Ask 1 to $MAX_QUESTIONS questions."
                }

                questions.size > 1 && questions.any { it.characters && it.options.isEmpty() } -> {
                    "A run character question without options must be asked alone."
                }

                questions.count { it.characters } > 1 -> {
                    "Ask for the run character in one question."
                }

                questions.any {
                    it.options.size !in MIN_OPTIONS..MAX_OPTIONS &&
                        !(it.characters && it.options.isEmpty())
                } -> {
                    "Give each question $MIN_OPTIONS to $MAX_OPTIONS options."
                }

                questions.any { q ->
                    q.question.isBlank() || q.options.any { it.label.isBlank() }
                } -> {
                    "Questions and option labels must not be empty."
                }

                else -> {
                    null
                }
            }
        if (problem != null) return Result.failure(IllegalArgumentException(problem))
        return Result.success(questions)
    }

    /** The answers as Claude reads them, in its tool result and in later turns' history. */
    fun describe(
        questions: List<Question>,
        answers: List<Answer>?,
    ): String {
        if (answers == null) return "The player skipped the questions."
        return questions.zip(answers).joinToString("\n") { (question, answer) ->
            "Q: ${question.question}\nA: ${answerText(answer)}"
        }
    }

    /** One answer in words, for the player and for Claude. */
    fun answerText(answer: Answer): String =
        listOfNotNull(
            answer.picked.joinToString(", ").ifEmpty { null },
            answer.typed?.let { "typed \"$it\"" },
        ).joinToString("; ")

    fun encode(asks: List<AnsweredAsk>): String =
        if (asks.isEmpty()) "" else json.encodeToString(asks)

    fun decode(saved: String): List<AnsweredAsk> =
        if (saved.isEmpty()) emptyList() else json.decodeFromString(saved)
}
