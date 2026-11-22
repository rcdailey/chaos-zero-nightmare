# Repository Rules

- Choose a solution in this order:
  1. Decide the best tool, format, design, or implementation for the job, without regard to what
     a library offers.
  2. Use a popular, maintained library or framework that implements that choice; hand-write it
     when none exists.
  3. When a library only comes close, or suggests a different approach, present it with the
     tradeoffs and wait for a decision; MUST NOT reshape the design to fit it.
- Before reporting work complete, run the checks of every project you changed and fix every
  problem they report, including problems that predate your change or fall outside its scope.
- Pushing `main` deploys: `projects/czn-tools` to GitHub Pages, and `projects/czn-data` as the
  `latest` image the cluster runs.

## Layout

Independent projects, each with its own `mise.toml` toolchain; run commands from the project
directory. Before working in a project, read its `AGENTS.md`; shell commands do not load it.

- `projects/czn-data`: Python (uv) scrapers (e7bot game data, Prydwen guides, Stove posts, Game8
  system guides, in-game Help from the game's files), merge build, and MCP server. `README.md` there
  owns behavior, configuration, and deployment.
- `projects/czn-tools`: Vite/React static site.
- `projects/czn-companion`: Android app that screenshots the game and shows Claude's advice in an
  overlay.
- Companion sessions (runs) live in the app's Room database on the attached device. Copy `runs.db`,
  `runs.db-wal`, and `runs.db-shm` (recent writes sit in the WAL) into one directory, each with
  `adb exec-out run-as app.dailey.czn.companion cat databases/<file> > <file>`, then query with
  `sqlite3 runs.db`. The current session is `run.active = 1`; its messages are `run_entry` by
  `position` (`screen`: characters the app named; `exchange`: every tool call's full input and
  result), and an unfinished reply is in `open_turn`. The newest 20 screenshots, at full size, are
  in `files/screenshots` (`run_entry.screenshot` names one; when Claude asked for character
  names, `<millis>.identify.json` beside it holds each figure's candidates and match scores);
  copy them with `run-as` too.
- `docs/game-data-investigation.md`: formats of CZN's own game files and the open build-folder
  lookup; only the Help text is implemented.
- Markdown anywhere: check with `mise run lint` from the repository root.

## Tool documentation for Claude

The companion is the client Claude uses these tools through. Its Messages API MCP connector
passes Claude each tool's name, description, and parameter schema, and documents tool calls only.

- Tool description (czn-data docstrings, companion tool definitions): what the tool does and
  returns, parameter values and caps, what result fields mean, known data gaps, and when to pick
  it over a similar tool.
- Companion app prompt (`APP_PROMPT` in `claude/MessagesRequest.kt`): when and why to use tools
  while advising in the app, such as reading screenshots, run flow, reply format, and rules that
  span all czn tool results.
- MUST NOT set MCP server `instructions`; put the guidance in one of the places above.
- MCP tools serve Claude only and reply in plain text; programs MUST call REST routes, which reply
  in JSON (`projects/czn-data/README.md`).
