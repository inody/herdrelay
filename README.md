# HerdRelay

HerdRelay — a Discord control surface for Herdr. Each Herdr pane gets its own
Discord thread, and the relay keeps them in sync in both directions: completed
agent responses are relayed into the thread, and messages posted there go to
the pane.

Herdr remains the source of truth for panes, agents, cwd, and status; agent
adapters provide completed responses without scraping terminal output. Discord
threads are per-pane remote control surfaces.

## How it works

```
Herdr pane (agent)  ←→  Discord thread  (auto-created, auto-bound)
        │                       │
        └── agent hook ──────→  thread (completed response)
        ←── messages posted ──  thread (normal posts go to the pane)
        ←── approvals/denies ─  card buttons
```

- **Auto threads**: each Herdr pane gets one Discord thread under
  `thread_parent_channel_id`, named `alias/agent` (prefixed `🔴` while blocked).
  The thread is auto-bound to its pane.
- **Output relay**: agent adapters post completed responses into the bound
  thread. Claude Code and Codex use Stop hooks; Pi uses an `agent_settled`
  extension. None of these adapters reads terminal contents.
- **Thread post send**: any normal message posted in a bound thread is sent to
  its pane (no need to reply — just type).
- **Approvals**: blocked panes post an approval card (Approve / Deny buttons,
  `@mention` so it pushes to mobile).
- **Target cards**: when a pane becomes blocked or finishes, the watcher
  posts a card showing the latest output (up to `max_tail_lines`) plus a status
  header and Approve / Deny / Stop buttons (shown only when relevant).

## Usage

There are no slash commands — this keeps `/status`, `/compact`, `/usage`
(Codex), and other harness commands usable as plain text inside threads.
Each Herdr pane has its own Discord thread (auto-created under
`thread_parent_channel_id`).

- **Post in a thread** to send text to its pane. Harness slash commands
  (`/status`, `/compact`, …) work by typing them as normal messages.
- **Special keys**: type `!esc`, `!enter`, `!up`, `!down`, `!left`, `!right`,
  `!tab`, `!space`, `!ctrl-c`, `!backspace`, `!pgup`, `!pgdn` to send those
  keys instead of text — useful for escaping a pager (e.g. Claude's `/usage`
  shows "esc to exit") or navigating a TUI (e.g. `/model`).
- **Control Herdr** by mentioning the bot (`@herdr`):
  - `@herdr start <name> [options] [-- <argv...>]` — start a pane (agent)
    - options: `--cwd PATH` `--workspace ID` `--tab ID` `--split right|down`
    - inside a pane thread: inherits that pane's workspace/tab/cwd by default
    - elsewhere: uses herdr's focused workspace/tab; argv defaults to `<name>`
  - `@herdr stop [<pane_id>]` — stop (close) a pane (inside a pane thread: closes that pane)
  - `@herdr list` — list panes/agents
  - `@herdr help` — show commands
- **Approve / Deny / Cancel** buttons appear on the target card the watcher
  posts when a pane is blocked or working.
- Supported agent responses are relayed into the thread automatically when a
  turn completes.

Write actions (approve, deny, stop, posting in a bound thread) require an
allowlisted Discord user.

## Requirements

- macOS or another local machine that can reach the Herdr CLI and socket.
- Python 3.12 or newer.
- `uv` for dependency installation.
- A Discord server with a bot installed.
- Herdr running locally with at least one pane or agent visible.

## Discord setup

1. Create a Discord application, add a bot, and copy its token into `.env`:
   ```dotenv
   DISCORD_TOKEN=
   ```
2. In the Bot page, enable **Message Content Intent** (required so the bridge
   can read messages posted in threads and forward them to panes).
3. In OAuth2 URL Generator, select scopes:
   ```text
   bot
   ```
4. Select bot permissions:
   ```text
   View Channels
   Send Messages
   Read Message History
   Create Public Threads
   Send Messages in Threads
   Manage Threads          (rename threads to match status)
   Add Reactions           (✅/⚠️ confirmations on thread posts)
   ```
5. Open the install URL and add the bot to your server.
6. Enable Developer Mode in Discord user settings, then copy the IDs for your
   server, a **thread parent channel** (where pane threads are created), and
   your user profile.

## Local setup

1. Install dependencies:
   ```bash
   uv sync --extra dev
   ```
2. Create local config files:
   ```bash
   cp .env.example .env
   cp config.example.yaml config.yaml
   ```
3. Put the Discord bot token in `.env`.
4. Put the Discord IDs and the thread parent channel in `config.yaml`:
   ```yaml
   allowed_guild_ids:
     - 123456789012345678
   allowed_channel_ids:
     - 123456789012345678        # thread parent channel
   allowed_user_ids:
     - 123456789012345678
   thread_parent_channel_id: 123456789012345678
   ```
5. Enable the features you want:
   ```yaml
   enable_send: true
   enable_approve: true
   enable_watcher: true
   enable_stop: true
   enable_auto_threads: true
   enable_streaming: true
   streaming:
     mode: hooks
   ```
6. Install the agent adapters. The Claude and Codex installers preserve their
   existing settings and create backups; the Codex hook does not replace its
   existing `notify` command.
   ```bash
   python3 scripts/manage_claude_hook.py install
   python3 scripts/manage_pi_adapter.py install
   python3 scripts/manage_codex_hook.py install
   ```
   Run `/reload` in existing Pi sessions. Restart existing Claude Code and
   Codex sessions so they load their new Stop hooks. Each command also supports
   `uninstall`.
7. Run the bot:
   ```bash
   scripts/run_bot.sh
   ```

## First verification

1. With `enable_auto_threads: true`, pane threads appear under the thread
   parent channel, named `alias/agent` (🔴 while blocked).
2. Open a pane's thread and post a message — it is sent to the pane and gets a
   ✅ reaction. Harness commands like `/status` or `/compact` also work as
   normal messages.
3. Complete a turn in a reloaded/restarted Claude Code, Pi, or Codex pane — its
   full final response appears in the corresponding thread.
4. When a pane becomes blocked, the watcher posts a target card with Approve /
   Deny buttons (and `@mention`).

## Configuration reference

```yaml
allowed_guild_ids: []
allowed_channel_ids: []
allowed_user_ids: []
thread_parent_channel_id: null

database_path: herdrelay.sqlite3

max_output_chars: 1800
max_message_chars: 2000

enable_send: false
enable_approve: false
enable_watcher: false
enable_stop: false
enable_auto_threads: false
enable_streaming: false
allow_pane_send_fallback: false

auto_threads:
  refresh_seconds: 30          # how often panes are synced to threads

streaming:
  mode: hooks                  # hooks (recommended) or poll
  refresh_seconds: 8           # poll mode only
  tail_lines: 1000             # poll mode only
  initial_tail_lines: 60       # poll mode only
  enable_visible_fallback: true # poll mode only; captures pager/TUI updates
  hook_inbox_path: ~/.cache/herdrelay/agent-output
  hook_refresh_seconds: 1
  hook_max_event_bytes: 2000000
  hook_max_event_age_seconds: 86400

watcher:
  statuses: ["blocked", "done"]
  reconnect_delay_seconds: 5
  resubscribe_interval_seconds: 300
  include_output: true         # read and attach a tail to status notifications
  blocked_tail_lines: 80
  done_tail_lines: 60

dangerous_text_blocklist:
  - "rm -rf"
  - "sudo"
  - "git reset --hard"

herdr:
  cli_path: herdr
  default_source: recent-unwrapped
  command_timeout_seconds: 20

approval:                      # how each agent is approved (blocked panes)
  codex:
    method: send_text_enter
    text: "y"
  claude:
    method: send_keys
    keys: ["Enter"]
  pi:
    method: send_keys
    keys: ["Enter"]
deny:                          # how each agent's prompt is dismissed
  codex:
    method: send_keys
    keys: ["Escape"]
  claude:
    method: send_keys
    keys: ["Escape"]
  pi:
    method: send_keys
    keys: ["Escape"]
stop:                          # how a pane is interrupted (Stop button)
  method: send_keys
  keys: ["C-c"]
```

## Security

- Read-only UI is allowed only in `allowed_guild_ids` / `allowed_channel_ids`.
- Send, approve, deny, stop, and posting in a bound thread require an
  `allowed_user_ids` entry.
- The `dangerous_text_blocklist` rejects messages containing blocked phrases.
- A target is never auto-resolved when multiple panes match a query.
- All write actions are recorded in the audit log (`audit_log` table).
- Hook events contain full assistant responses. They are written with mode
  `0600` inside a `0700` inbox and removed after successful delivery.

## Running in the background

```bash
scripts/install_launchd.sh
scripts/status_launchd.sh
```

After changing `.env`, `config.yaml`, or code:

```bash
launchctl kickstart -k "gui/$UID/dev.herdrelay"
```

Logs: `logs/launchd.out.log` and `logs/launchd.err.log`. The launchd
installation also checks them hourly, rotates files above 10 MiB, and keeps
three gzip-compressed backups.

## Operational notes

- `.env`, `config.yaml`, and the SQLite database are gitignored.
- Sending uses the Herdr 0.8 CLI (`agent prompt`). Hook-mode output relay does
  not call `pane read` or `agent read`. The optional legacy `poll` mode does,
  and its `visible` fallback can capture pager/TUI output such as `/usage`.
  Status notifications use the raw socket API.
- Thread names are renamed to `🔴 alias/agent` only while blocked; other status
  changes do not rename threads (Discord rate-limits channel edits).
- Notification dedupe is stored in SQLite using `pane_id + status + tail hash`.
- Pane ↔ thread mappings are stored in SQLite (`agent_threads`).

### Agent output adapters

Hook mode is agent-neutral. An adapter atomically places a `.json` file in
`hook_inbox_path` with this format:

```json
{
  "version": 1,
  "event_id": "stable-id-for-this-turn",
  "agent": "claude",
  "pane_id": "w1:p1",
  "text": "complete assistant response"
}
```

HerdRelay handles pane/thread lookup, Discord chunking, retries, and persistent
deduplication. `scripts/agent_stop_hook.py` handles Claude Code and Codex Stop
events; `integrations/pi-herdrelay-output.ts` handles Pi's `agent_settled`
event. Additional agents can use the same protocol without changing the
Discord relay or reading terminal contents.

### Herdr 0.8 fullscreen redraw workaround

If a fullscreen/alternate-screen agent appears to jump upward when a turn ends,
avoid the watcher operations correlated with the redraw while keeping Discord
send and output streaming enabled:

```yaml
streaming:
  mode: hooks
watcher:
  statuses: ["blocked"]
  resubscribe_interval_seconds: 0
  include_output: false
```

Hook mode receives completed responses directly from agent-specific adapters;
it never reads terminal output. The watcher settings disable done cards,
watcher tail reads, and periodic subscription reconnects. New panes will not
receive watcher notifications until HerdRelay restarts, but auto-thread
creation and hook output relay continue. Set `enable_watcher: false` to disable
status subscriptions, or `enable_streaming: false` to disable response relay.
