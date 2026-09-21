# ContextStore

Local, persistent memory for AI agents — exposed as an [MCP](https://modelcontextprotocol.io/) server.

Hierarchical notes in a single SQLite database (WAL + FTS5 full-text search), plus read-only
access to OpenCode session history. **Everything is model-initiated**: no cron, no timers,
no background jobs. The store changes only when a model calls a tool, e.g. when you ask
*"store a summary of this session"* or *"store actual api docs for fastapi"*.

## Features

- **Zero external servers** — one SQLite file, no Postgres, no vector DB, no containers
- **MCP-first** — the AI decides what to store and query; tools are the only interface
- **BM25 search** — SQLite's built-in FTS5 (phrases, `OR`, prefix `term*`), zero extra dependencies
- **Session history** — read-only, compact transcripts of past OpenCode sessions
- **Provenance** — notes can record which session/project created them
- **Lifecycle** — browse, upsert, and delete so stale knowledge never lingers

## Requirements

- Python 3.10+ (3.12 tested)
- [OpenCode](https://opencode.ai/) or any MCP client (stdio transport)

## Install

```sh
git clone <repo-url> ~/context-store   # anywhere works
cd ~/context-store
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m pytest -q          # 31 tests should pass
```

> On PEP 668 systems (Debian/Ubuntu) without `python3-venv`, either
> `apt install python3.12-venv`, or `pip install --user --break-system-packages virtualenv`
> and create the env with `python3 -m virtualenv .venv`.

## Register with OpenCode

```sh
opencode mcp add context-store --global -- \
  /path/to/context-store/.venv/bin/python -m context_store.main
opencode mcp list        # → ✓ context-store  connected
```

Equivalent `opencode.json(c)` config:

```jsonc
{
  "mcp": {
    "servers": {
      "context-store": {
        "type": "local",
        "command": ["/path/to/context-store/.venv/bin/python", "-m", "context_store.main"],
      },
    },
  },
}
```

Tools appear as `tools["context-store"].save(…)` under Code Mode, or `context_store_save`
with `codemode: false`. The `/context-store:summarize-session` prompt command guides the
model through the summarize-and-store workflow.

## Tools

| Tool | Purpose |
|---|---|
| `save(path, content, tags?, kind?, title?, source_url?)` | Create or overwrite (upsert) a note. |
| `read(path)` | Fetch a note by exact path, with full metadata. |
| `search(query, kind?, tag?, limit?=5)` | BM25 search; snippets with `**highlighted**` matches; lower score = better. |
| `list(prefix?, kind?, tag?, limit?=50)` | Browse stored paths — call before saving to avoid duplicates. |
| `delete(path)` | Permanently remove a note; returns a preview of what was deleted. |
| `stats()` | Totals by kind, top tags, recent saves, DB size. |
| `sessions_list(project?, since?, limit?=20)` | OpenCode sessions, newest first (read-only). |
| `session_read(session_id, include_tools?=false, max_chars?=24000)` | Compact transcript of a past session. |

### Path conventions

```
sessions/<project>/<yyyy-mm-dd>-<slug>   session summaries
decisions/<project>/<topic>              what was chosen and why
docs/<library>/<topic>                   fetched API docs (set source_url)
notes/<project>/<topic>                  general project knowledge
howto/<task>                             reusable procedures / workarounds
```

### Example workflows

- *"store a summary of this session"* → the model summarizes the conversation and calls
  `save("sessions/<project>/<date>-<slug>", …, kind="session-summary")`
- *"summarize the session where we fixed the voice assistant crash"* → `sessions_list` →
  `session_read` → model summarizes → `save`
- *"store actual api docs for fastapi middleware"* → the model fetches the docs page with
  its own web tools → `save("docs/fastapi/middleware", …, source_url=…)`
- *"what do we know about auth decisions here?"* → `search("auth decision")` or `read` by path

## Configuration

| Env variable | Default | Purpose |
|---|---|---|
| `CONTEXT_DB` | `~/.local/share/opencode/context.db` | Note database location |
| `OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | OpenCode session database (read-only) |

## CLI

The CLI shares the library with the MCP tools — handy for debugging and for future
automation (a timer could run the same code paths; none is included):

```sh
.venv/bin/python -m context_store.cli save notes/demo --content "hello" --tags demo
.venv/bin/python -m context_store.cli search "hello"
.venv/bin/python -m context_store.cli sessions --project myproject --limit 5
.venv/bin/python -m context_store.cli --help
```

## Design

`SPEC.md` (kept locally, not tracked) holds the full design rationale.

## License

[MIT](LICENSE) © 2026 Ihar Kharashun
