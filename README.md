# herdr-discord-bridge

A Discord control surface for Herdr. Each Herdr pane gets its own Discord
thread, and the bridge keeps them in sync in both directions: pane output
streams into the thread, and messages posted in the thread go to the pane.

Herdr remains the source of truth for panes, agents, output, cwd, and status.
Discord threads are per-pane remote control surfaces.

## How it works

```
Herdr pane (agent)  ←→  Discord thread  (auto-created, auto-bound)
        │                       │
        └── output streams ──→  thread (new lines, ~8s polling)
        ←── messages posted ──  thread (normal posts go to the pane)
        ←── approvals/denies ─  card buttons
```

- **Auto threads**: each Herdr pane gets one Discord thread under
  `thread_parent_channel_id`, named `alias/agent` (prefixed `🔴` while blocked).
  The thread is auto-bound to its pane.
- **Streaming**: new pane output is posted into the bound thread.
- **Thread post send**: any normal message posted in a bound thread is sent to
  its pane (no need to reply — just type).
- **Approvals**: blocked panes post an approval card (Approve / Deny buttons,
  `@mention` so it pushes to mobile).
- **Target cards**: `/herdr current` opens a card with Tail / Bind / Refresh /
  Ask / Approve / Deny / Stop buttons.

## Slash commands

- `/herdr status` — list visible Herdr agents and panes
- `/herdr ids` — show Discord IDs for this location
- `/herdr current` — open the target card for the pane bound here
- `/herdr tail [target] [lines]` — show recent output
- `/herdr bind target [label]` — bind this thread/channel to a Herdr target (manual fallback)
- `/herdr bindings` — list bindings for this server
- `/herdr unbind` — remove the binding for this location
- `/herdr send message [target]` — send text to a Herdr target
- `/herdr approve [target]` — preview output, then approve via button

Write actions (send, approve, deny, stop, post-in-bound-thread) require an
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
   applications.commands
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
   Use Application Commands
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
   ```
6. Run the bot:
   ```bash
   scripts/run_bot.sh
   ```

## First verification

1. Confirm the bot can see Herdr:
   ```text
   /herdr status
   ```
2. With `enable_auto_threads: true`, pane threads appear under the thread parent
   channel, named `alias/agent` (🔴 while blocked).
3. Open a pane's thread and post a message — it is sent to the pane and gets a
   ✅ reaction.
4. Run `/herdr current` in a thread to open its target card.

## Configuration reference

```yaml
allowed_guild_ids: []
allowed_channel_ids: []
allowed_user_ids: []
thread_parent_channel_id: null

database_path: herdr-discord-bridge.sqlite3

max_tail_lines: 80
max_output_chars: 1800
max_message_chars: 2000

enable_send: false
enable_approve: false
enable_watcher: false
enable_stop: false
enable_auto_threads: false
enable_streaming: false
allow_pane_send_fallback: false
submit_after_agent_send: true
submit_after_agent_send_delay_seconds: 0.5

auto_threads:
  refresh_seconds: 30          # how often panes are synced to threads

streaming:
  refresh_seconds: 8           # how often new pane output is pushed to threads
  tail_lines: 60

watcher:
  statuses: ["blocked", "done"]
  reconnect_delay_seconds: 5
  resubscribe_interval_seconds: 300
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

## Running in the background

```bash
scripts/install_launchd.sh
scripts/status_launchd.sh
```

After changing `.env`, `config.yaml`, or code:

```bash
launchctl kickstart -k "gui/$UID/dev.herdr.discord-bridge"
```

Logs: `logs/launchd.out.log` and `logs/launchd.err.log`.

## Operational notes

- `.env`, `config.yaml`, and the SQLite database are gitignored.
- Read/tail/send use Herdr CLI wrappers. Event notifications and streaming use
  the Herdr CLI and raw socket API.
- Thread names are renamed to `🔴 alias/agent` only while blocked; other status
  changes do not rename threads (Discord rate-limits channel edits).
- Notification dedupe is stored in SQLite using `pane_id + status + tail hash`.
- Pane ↔ thread mapping and per-pane stream state are stored in SQLite
  (`agent_threads`, `pane_streams`).
