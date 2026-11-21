# Repository Rules

- Use a popular, maintained library for any capability one provides (rendering, parsing, protocols,
  UI components); hand-roll only when no such library exists.
- Before reporting work complete, run the checks of every project you changed and fix every
  problem they report, including problems that predate your change or fall outside its scope.
- Pushing `main` deploys: `projects/czn-tools` to GitHub Pages, and `projects/czn-data` as the
  `latest` image the cluster runs.

## Layout

Independent projects, each with its own `mise.toml` toolchain; run commands from the project
directory. Before working in a project, read its `AGENTS.md`; shell commands do not load it.

- `projects/czn-data`: Python (uv) scrapers (e7bot game data, Prydwen guides), merge build, and MCP
  server. `README.md` there owns behavior, configuration, and deployment.
- `projects/czn-tools`: Vite/React static site.
- `projects/czn-companion`: Android app that screenshots the game and shows Claude's advice in an
  overlay.
- `docs/game-data-investigation.md`: findings on reading CZN's own game files; nothing implemented.
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
