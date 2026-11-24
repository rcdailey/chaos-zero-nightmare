package app.dailey.czn.companion.overlay

import androidx.annotation.DrawableRes
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyGridScope
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Card
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.PrimaryTabRow
import androidx.compose.material3.Surface
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.tooling.preview.PreviewFontScale
import androidx.compose.ui.tooling.preview.PreviewLightDark
import androidx.compose.ui.unit.dp
import app.dailey.czn.companion.CompanionTheme
import app.dailey.czn.companion.R
import app.dailey.czn.companion.run.DeckComparison
import app.dailey.czn.companion.run.MatchState
import app.dailey.czn.companion.run.PartKind
import app.dailey.czn.companion.run.RunDeck
import app.dailey.czn.companion.run.RunDecks
import coil3.compose.AsyncImage

@DrawableRes
private fun MatchState.icon() =
    when (this) {
        MatchState.Match -> R.drawable.ic_check
        MatchState.Fixable -> R.drawable.ic_upgrade
        MatchState.Wrong -> R.drawable.ic_error
        MatchState.Missing -> R.drawable.ic_do_not_disturb_on
        MatchState.Extra -> R.drawable.ic_add
        MatchState.Unchecked -> R.drawable.ic_help
    }

/**
 * One card, removal, or equipment slot. [detail] is the version or slot ("Epiphany III",
 * "Weapon"); [state] is null on the target side, which has nothing to compare with. [image] is
 * the card art or equipment icon and [background] the equipment's rarity frame, each as a Coil
 * model (a URL, or a drawable resource in previews).
 */
data class DeckLine(
    val name: String,
    val detail: String? = null,
    val copies: Int = 1,
    val state: MatchState? = null,
    val note: String? = null,
    val cost: String? = null,
    val image: Any? = null,
    val background: Any? = null,
)

/** How a section draws its lines: tall card tiles, square equipment tiles, or plain text. */
enum class SectionKind { Cards, Equipment, Text }

data class DeckSection(
    val title: String,
    val kind: SectionKind,
    val lines: List<DeckLine>,
)

/** What the run deck view shows: the target and the current deck of the run's character. */
data class RunDeckState(
    val character: String? = null,
    val limit: Int? = null,
    val targetPoints: Int? = null,
    val currentPoints: Int? = null,
    val target: List<DeckSection> = emptyList(),
    val current: List<DeckSection> = emptyList(),
)

/** The deck view of [deck] (none saved yet when null) for the run's [character]. */
fun runDeckState(
    character: String?,
    deck: RunDeck?,
): RunDeckState {
    if (deck == null) return RunDeckState(character)

    fun card(
        name: String,
        version: String?,
        divine: Boolean,
    ): DeckLine {
        val info = deck.cardInfo[RunDecks.infoKey(name, version)]
        val epiphany = (version ?: info?.variant?.ifEmpty { null })?.let { "Epiphany $it" }
        return DeckLine(
            info?.baseName ?: name,
            listOfNotNull(epiphany ?: "Base", "Divine".takeIf { divine }).joinToString(", "),
            cost = info?.cost,
            image = info?.imageUrl,
        )
    }

    fun gear(
        name: String?,
        slot: String,
    ): DeckLine {
        val info = name?.let { deck.gearInfo[RunDecks.infoKey(it)] }
        return DeckLine(
            info?.name ?: name ?: slot,
            slot.takeIf { name != null },
            image = info?.imageUrl,
            background = info?.backgroundUrl,
        )
    }

    fun removal(name: String) = card(name, null, false).copy(detail = "Removed")

    val target =
        listOf(
            DeckSection(
                "Equipment",
                SectionKind.Equipment,
                deck.target.gear
                    .slots()
                    .map { (slot, name) -> gear(name, slot) },
            ),
            DeckSection(
                "Cards",
                SectionKind.Cards,
                deck.target.cards.map {
                    card(it.card, it.version, it.divine).copy(copies = it.copies)
                },
            ),
            DeckSection("Removals", SectionKind.Cards, deck.target.removed.map(::removal)),
            DeckSection(
                "Drop first when over the limit",
                SectionKind.Text,
                deck.dropFirst.mapIndexed { index, part -> DeckLine("${index + 1}. $part") },
            ),
        )
    val compared = DeckComparison.compare(deck)
    val current =
        PartKind.entries.map { kind ->
            val parts = compared.filter { it.kind == kind }
            val lines =
                parts.map { part ->
                    val line =
                        when (kind) {
                            PartKind.Gear -> {
                                gear(
                                    part.name.takeIf { part.state != MatchState.Unchecked },
                                    part.slot.orEmpty(),
                                )
                            }

                            PartKind.Card -> {
                                card(part.name, part.version, part.divine)
                            }

                            PartKind.Removal -> {
                                removal(part.name)
                            }
                        }
                    line.copy(copies = part.copies, state = part.state, note = part.note)
                }
            when (kind) {
                PartKind.Gear -> DeckSection("Equipment", SectionKind.Equipment, lines)
                PartKind.Card -> DeckSection("Cards", SectionKind.Cards, lines)
                PartKind.Removal -> DeckSection("Removals", SectionKind.Cards, lines)
            }
        }
    return RunDeckState(
        character,
        deck.limit,
        deck.targetPoints,
        deck.points,
        target.filter { it.lines.isNotEmpty() },
        current.filter { it.lines.isNotEmpty() },
    )
}

enum class RunDeckTab(
    val title: String,
) {
    Target("Target"),
    Current("Current"),
}

/**
 * The run deck view, in place of the conversation: a Target tab with what the run farms for,
 * and a Current tab with what the player has now, each part marked with how it compares.
 */
@Composable
fun RunDeckPanel(
    state: RunDeckState,
    tab: RunDeckTab,
    onTab: (RunDeckTab) -> Unit,
    onBack: () -> Unit,
) = Surface(
    color = MaterialTheme.colorScheme.surfaceContainer,
    contentColor = MaterialTheme.colorScheme.onSurface,
    shadowElevation = 8.dp,
    modifier = Modifier.fillMaxSize(),
) {
    Column {
        TopAppBar(
            title = {
                Text(
                    state.character?.let { "$it's deck" } ?: "Run deck",
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            },
            navigationIcon = {
                IconButton(onClick = onBack) {
                    Icon(
                        painterResource(R.drawable.ic_arrow_back),
                        contentDescription = "Back to advice",
                    )
                }
            },
            windowInsets = WindowInsets(0),
            colors =
                TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surfaceContainer,
                ),
        )
        PrimaryTabRow(
            selectedTabIndex = tab.ordinal,
            containerColor = MaterialTheme.colorScheme.surfaceContainer,
        ) {
            RunDeckTab.entries.forEach {
                Tab(tab == it, onClick = { onTab(it) }, text = { Text(it.title) })
            }
        }
        LazyVerticalGrid(
            columns = GridCells.Adaptive(TILE_MIN_WIDTH),
            contentPadding = PaddingValues(16.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.weight(1f),
        ) {
            if (tab == RunDeckTab.Target && state.target.isEmpty()) {
                fullWidth { Text("Claude has not saved a target deck for this run yet.") }
            } else if (tab == RunDeckTab.Target) {
                fullWidth { Points("Target costs", state.targetPoints, state.limit) }
                sections(state.target)
            } else if (state.current.isEmpty()) {
                fullWidth { Text("Claude has not saved this run's deck yet.") }
            } else {
                fullWidth { Points("Save Data holds", state.currentPoints, state.limit) }
                fullWidth { MatchSummary(state.current) }
                sections(state.current)
            }
        }
    }
}

/** Faint Memory points against the Save Data limit, as a number and a bar. */
@Composable
private fun Points(
    label: String,
    points: Int?,
    limit: Int?,
) = Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
    val shown = points?.toString() ?: "?"
    Text(
        "$label $shown of ${limit ?: "?"} Faint Memory points",
        style = MaterialTheme.typography.bodyMedium,
    )
    if (points != null && limit != null) {
        LinearProgressIndicator(
            progress = { (points.toFloat() / limit).coerceIn(0f, 1f) },
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

/** Counts of each match state, so the player sees progress without reading every line. */
@Composable
private fun MatchSummary(sections: List<DeckSection>) {
    val states = sections.flatMap { it.lines }.mapNotNull { it.state }
    val counts =
        MatchState.entries
            .map { state -> state to states.count { it == state } }
            .filter { it.second > 0 }
    Text(
        counts.joinToString(", ") { (state, count) -> "$count ${state.label.lowercase()}" },
        style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

private val TILE_MIN_WIDTH = 112.dp

/** Width to height of the game's card art (420 by 632 pixels). */
private const val CARD_ART_RATIO = 420f / 632f

private fun LazyGridScope.fullWidth(content: @Composable () -> Unit) =
    item(span = { GridItemSpan(maxLineSpan) }) { content() }

private fun LazyGridScope.sections(sections: List<DeckSection>) =
    sections.forEach { section ->
        fullWidth {
            Text(
                section.title,
                style = MaterialTheme.typography.titleSmall,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(top = 8.dp),
            )
        }
        when (section.kind) {
            SectionKind.Cards -> items(section.lines) { DeckTile(it, CARD_ART_RATIO) }
            SectionKind.Equipment -> items(section.lines) { DeckTile(it, 1f) }
            SectionKind.Text -> section.lines.forEach { line -> fullWidth { DeckText(line) } }
        }
    }

/**
 * A card or equipment tile: the picture filling the tile, with the cost, copies, and match state
 * (icon, then in words, so the state never rests on color) at the top, and the name and version
 * at the bottom over a shade that darkens toward the tile's bottom edge. The text is drawn in
 * the dark theme's light text color with an outline, so it reads on any art in either theme.
 */
@Composable
private fun DeckTile(
    line: DeckLine,
    aspectRatio: Float,
) = Card(Modifier.fillMaxWidth().aspectRatio(aspectRatio)) {
    Box(Modifier.fillMaxSize()) {
        line.background?.let { TileImage(it) }
        line.image?.let { TileImage(it) }
        CompanionTheme(dark = true) {
            val scrim = MaterialTheme.colorScheme.scrim
            Box(
                Modifier
                    .align(Alignment.BottomCenter)
                    .fillMaxWidth()
                    .fillMaxHeight(BOTTOM_SHADE_HEIGHT)
                    .background(
                        Brush.verticalGradient(
                            0f to scrim.copy(alpha = 0f),
                            1f to scrim.copy(alpha = 0.85f),
                        ),
                    ),
            )
            Column(Modifier.fillMaxSize().padding(6.dp)) {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    line.cost?.let { CostBadge(it) }
                    Spacer(Modifier.weight(1f))
                    line.copies.takeIf { it > 1 }?.let {
                        TileText("x$it", MaterialTheme.typography.titleMedium)
                    }
                    line.state?.let { StateIcon(it) }
                }
                line.state?.let { state ->
                    TileText(
                        state.label,
                        MaterialTheme.typography.labelMedium,
                        Modifier.align(Alignment.End),
                    )
                }
                Spacer(Modifier.weight(1f))
                TileText(line.name, MaterialTheme.typography.labelLarge)
                line.detail?.let { TileText(it, MaterialTheme.typography.labelSmall) }
            }
        }
    }
}

/** Share of the tile's height the bottom shade covers, fading in from its top edge. */
private const val BOTTOM_SHADE_HEIGHT = 0.5f

/** The card's cost in a rounded box in the top-left corner, where the game prints it. */
@Composable
private fun CostBadge(cost: String) =
    Surface(
        shape = MaterialTheme.shapes.small,
        color = MaterialTheme.colorScheme.surfaceContainerHighest.copy(alpha = 0.88f),
        contentColor = MaterialTheme.colorScheme.onSurface,
    ) {
        Text(
            cost,
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
        )
    }

/**
 * Text on a tile's picture: a dark outline with a shadow drawn under the light text, which keeps
 * it readable on bright art.
 */
@Composable
private fun TileText(
    text: String,
    style: TextStyle,
    modifier: Modifier = Modifier,
) = Box(modifier) {
    val dark = MaterialTheme.colorScheme.scrim
    Text(
        text,
        style =
            style.copy(
                color = dark,
                drawStyle = Stroke(width = 6f, join = StrokeJoin.Round),
                shadow = Shadow(dark, Offset(0f, 1f), blurRadius = 8f),
            ),
        maxLines = 2,
        overflow = TextOverflow.Ellipsis,
    )
    Text(
        text,
        style = style.copy(color = MaterialTheme.colorScheme.onSurface),
        maxLines = 2,
        overflow = TextOverflow.Ellipsis,
    )
}

/** The match state's icon on a small disc in the state's container color. */
@Composable
private fun StateIcon(state: MatchState) {
    val (container, content) = state.colors()
    Surface(shape = CircleShape, color = container, contentColor = content) {
        Icon(
            painterResource(state.icon()),
            contentDescription = null,
            modifier = Modifier.padding(3.dp).size(18.dp),
        )
    }
}

@Composable
private fun TileImage(model: Any) =
    AsyncImage(
        model = model,
        contentDescription = null,
        contentScale = ContentScale.Crop,
        modifier = Modifier.fillMaxSize(),
    )

/** A line without a picture, such as a step in the order of what to drop first. */
@Composable
private fun DeckText(line: DeckLine) =
    Column {
        Text(line.name, style = MaterialTheme.typography.bodyLarge)
        line.note?.let {
            Text(
                it,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }

/** Container and content color of the state's icon disc. */
@Composable
private fun MatchState.colors() =
    with(MaterialTheme.colorScheme) {
        when (this@colors) {
            MatchState.Match -> {
                primaryContainer to onPrimaryContainer
            }

            MatchState.Fixable -> {
                tertiaryContainer to onTertiaryContainer
            }

            MatchState.Wrong -> {
                errorContainer to onErrorContainer
            }

            MatchState.Missing, MatchState.Extra, MatchState.Unchecked -> {
                surfaceVariant to onSurfaceVariant
            }
        }
    }

// Sample pictures are e7bot's card art and equipment icons, shrunk, in res/drawable-nodpi.
private val HAMMER = DeckLine("Runic Hammer", "Weapon", image = R.drawable.preview_relic_5084)
private val SUIT = DeckLine("Titan Combat Suit", "Armor", image = R.drawable.preview_relic_1136)
private val TETRAHEDRON =
    DeckLine("Absolute Zero Tetrahedron", "Trinket", image = R.drawable.preview_relic_1063)
private val BLADE =
    DeckLine(
        "Blade of the Cycle",
        "Epiphany III",
        copies = 2,
        cost = "1",
        image = R.drawable.preview_card_unique_30134_04,
    )
private val FLOW =
    DeckLine(
        "Radiant Sword Flow",
        "Epiphany II",
        cost = "1",
        image = R.drawable.preview_card_unique_30134_03,
    )
private val RESONANCE =
    DeckLine(
        "Sword Resonance",
        "Epiphany I, Divine",
        cost = "1",
        image = R.drawable.preview_card_unique_30134_02,
    )
private val APOSTLE =
    DeckLine(
        "Kyrken's Apostle",
        "Base",
        cost = "0",
        image = R.drawable.preview_card_unique_30134_05,
    )
private val FLICKER =
    DeckLine("Flicker Blade", "Base", cost = "0", image = R.drawable.preview_card_unique_30134_01)
private val AIMED =
    DeckLine("Aimed Fire", "Base", cost = "2", image = R.drawable.preview_card_job_00290)
private val SLASH =
    DeckLine("Sword Slash", "Removed", cost = "1", image = R.drawable.preview_card_start_30134_01)
private val STANDING =
    DeckLine(
        "Standing Blade",
        "Removed",
        cost = "1",
        image = R.drawable.preview_card_start_30134_02,
    )

private fun DeckLine.rare() = copy(background = R.drawable.preview_rarity_rare)

private fun DeckLine.legend() = copy(background = R.drawable.preview_rarity_legend)

private val SAMPLE_DECK =
    RunDeckState(
        character = "Rossweisse",
        limit = 180,
        targetPoints = 170,
        currentPoints = 120,
        target =
            listOf(
                DeckSection(
                    "Equipment",
                    SectionKind.Equipment,
                    listOf(HAMMER.legend(), SUIT.rare(), TETRAHEDRON.rare()),
                ),
                DeckSection(
                    "Cards",
                    SectionKind.Cards,
                    listOf(BLADE, FLOW, RESONANCE, APOSTLE, FLICKER, AIMED),
                ),
                DeckSection("Removals", SectionKind.Cards, listOf(SLASH, STANDING)),
                DeckSection(
                    "Drop first when over the limit",
                    SectionKind.Text,
                    listOf(
                        DeckLine("1. Aimed Fire", note = "Saves 20 points"),
                        DeckLine("2. Second Blade of the Cycle", note = "Saves 30 points"),
                    ),
                ),
            ),
        current =
            listOf(
                DeckSection(
                    "Equipment",
                    SectionKind.Equipment,
                    listOf(
                        HAMMER.legend().copy(state = MatchState.Match),
                        DeckLine(
                            "Mask of Emotions",
                            "Armor",
                            state = MatchState.Wrong,
                            image = R.drawable.preview_relic_5044,
                        ).legend(),
                        DeckLine("Trinket", state = MatchState.Unchecked),
                    ),
                ),
                DeckSection(
                    "Cards",
                    SectionKind.Cards,
                    listOf(
                        BLADE.copy(copies = 1, state = MatchState.Missing),
                        FLOW.copy(detail = "Epiphany I", state = MatchState.Fixable),
                        RESONANCE.copy(state = MatchState.Missing),
                        APOSTLE.copy(state = MatchState.Match),
                        FLICKER.copy(state = MatchState.Match),
                        AIMED.copy(state = MatchState.Missing),
                        DeckLine(
                            "Ambush",
                            "Base",
                            cost = "0",
                            state = MatchState.Extra,
                            image = R.drawable.preview_card_job_00226,
                        ),
                    ),
                ),
                DeckSection(
                    "Removals",
                    SectionKind.Cards,
                    listOf(
                        SLASH.copy(state = MatchState.Match),
                        STANDING.copy(state = MatchState.Missing),
                    ),
                ),
            ),
    )

@PreviewLightDark
@PreviewFontScale
@Composable
private fun RunDeckTargetPreview() = RunDeckPreview(RunDeckTab.Target)

@PreviewLightDark
@PreviewFontScale
@Composable
private fun RunDeckCurrentPreview() = RunDeckPreview(RunDeckTab.Current)

@Composable
private fun RunDeckPreview(tab: RunDeckTab) =
    CompanionTheme(dark = true) {
        Box(Modifier.size(420.dp, 720.dp)) {
            RunDeckPanel(SAMPLE_DECK, tab, onTab = {}, onBack = {})
        }
    }
