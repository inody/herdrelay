# HerdRelay

HerdRelay — a Discord control surface for Herdr. Each Herdr pane with a detected
agent gets its own Discord thread. Completed agent responses are relayed into
the thread, and messages posted there go to the pane.

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

- **Auto threads**: each Herdr pane with a detected agent gets one Discord
  thread under `thread_parent_channel_id`, named `alias/agent` (prefixed `🔴`
  while blocked). The thread is auto-bound to its pane. Existing threads and
  their names survive agent exits or detection loss while the pane remains;
  empty shells do not create new threads.
- **Output relay**: agent adapters post completed responses into the bound
  thread as ordinary message text, without adding code-block fences. Newlines
  and agent-authored Markdown are preserved; long output is split to fit Discord,
  and relayed text cannot trigger mentions. Claude Code and Codex use Stop hooks;
  Pi uses an extension for interactive TUI sessions only. Its `input` event
  marks prompts whose source is `interactive`, and `agent_settled` relays the
  completed response. The adapter does not modify prompts or tell the user
  where to reply; ordinary text questions can be answered from the terminal or
  Discord.
  Supported Claude `AskUserQuestion` prompts get dedicated choice controls;
  the watcher briefly delays blocked notifications to let question hooks arrive.
  None of these adapters reads terminal contents.
- **Thread post send**: any normal message posted in a bound thread is sent to
  its pane (no need to reply — just type).
- **Status cards**: with `enable_watcher: true`, the watcher posts notifications
  for `watcher.statuses` (by default `blocked` and `done`). Terminal tails are
  attached only when `watcher.include_output: true`, using `blocked_tail_lines`
  or `done_tail_lines`. Blocked cards mention `allowed_user_ids`; whether they
  push to mobile depends on Discord notification settings.
- **Controls**: blocked cards can show Approve / Deny buttons. Pending Claude
  questions use their own choices instead. Working cards can show a Cancel
  button, but require `working` in `watcher.statuses` and `enable_stop: true`.
  Cancel sends an interrupt after confirmation; it does not close the pane.

## Usage

There are no slash commands — this keeps `/status`, `/compact`, `/usage`
(Codex), and other harness commands usable as plain text inside threads.
Agent threads are auto-created under `thread_parent_channel_id` when
`enable_auto_threads: true`. Existing threads remain bound when an agent exits
but its pane stays open. Creation alone does not add the allowed users to a
public thread: for Pi, an allowed user is added when that session receives an
interactive TUI input, so unused or autonomous-only Pi sessions do not fill the
Discord sidebar.

- **Post in a thread** to send text to its pane. Harness slash commands
  (`/status`, `/compact`, …) work by typing them as normal messages.
- **Special keys**: type `!esc`, `!enter`, `!up`, `!down`, `!left`, `!right`,
  `!tab`, `!space`, `!ctrl-c`, `!backspace`, `!pgup`, `!pgdn`, `!home`, `!end` to send those
  keys instead of text — useful for escaping a pager (e.g. Claude's `/usage`
  shows "esc to exit") or navigating a TUI (e.g. `/model`).
- **Control Herdr** by mentioning the bot (`@herdr`). See the
  [security policy](#security) for the required permissions:
  - `@herdr start <name> [options] [-- <argv...>]` — create a pane and start an agent
    - `<name>` is both the Herdr agent name and kind, such as `pi`, `claude`, or
      `codex`; it is not an arbitrary executable or a separate custom label
    - options: `--cwd PATH` `--workspace ID` `--tab ID` `--split right|down`
    - inside a pane thread: inherits missing workspace/tab/cwd; without an
      explicit workspace or tab, splits that thread's pane
    - elsewhere, without location options: splits the focused pane, or creates
      a workspace if none is focused; split direction defaults to `right`
    - arguments after `--` go to the selected agent; a redundant leading agent
      executable name is accepted and stripped
  - `@herdr stop [<pane_id>]` — **close the pane**, unlike the Cancel button;
    inside a pane thread, omitting the ID closes that thread's pane
  - `@herdr list` — list detected agents (falls back to panes if agent listing fails)
  - `@herdr help` — show commands
- **Approve / Deny** require `enable_approve: true`; Deny also needs a strategy
  in `deny`. **Cancel** requires `enable_stop: true` and a `stop` strategy.
- Supported agent responses are relayed into the thread automatically when a
  turn completes.

Thread posts, action buttons, and mutating mention commands require an
allowlisted Discord user. `@herdr start` requires `enable_send: true`;
`@herdr stop` requires `enable_stop: true`. Read-only `list`/`help` commands
require an allowed location, but not a write-user entry. Also restrict access
through Discord channel permissions (see [Security](#security)).

Sending a harness command and seeing its TUI output are separate: hook mode
relays assistant responses, not `/usage` pages, model menus, or other screen-only
updates. Inspect those in the terminal, or use the optional `poll` mode.

## Requirements

- macOS or another Unix-like machine that can reach a local Herdr CLI and Unix
  socket. The supplied background-service scripts use macOS launchd.
- Python 3.12 or newer.
- `uv` for dependency installation.
- A Discord server with a bot installed.
- Herdr running locally. With no panes, the watcher waits; a detected agent is
  needed before an automatic Discord thread is created.

Compatibility was checked with Herdr 0.9.5 and Pi 1.5.0 on 2026-11-03; these are
verified versions, not a promise to track the latest release automatically.
The Pi output adapter requires `ExtensionContext.mode`; contexts without it
are ignored. Check `herdr --version` and `herdr status` separately: an updated
CLI does not necessarily mean the running server has been updated. The official
stable release is listed at <https://herdr.dev/latest.json>.

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
   Manage Threads          (rename, archive, and unarchive pane threads)
   Add Reactions           (✅/⚠️ confirmations on thread posts)
   Change Nickname         (optional: let the bot name itself "herdr")
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
3. Put the Discord bot token in `.env`. For a non-default local Herdr session,
   set `HERDR_SOCKET_PATH` to that session's socket; the CLI calls and watcher
   must address the same session. The bridge does not follow Herdr's selected
   remote machine in the TUI.
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
6. Install the adapters for the agents you use. Existing settings/files are
   backed up before modification. Claude's installer merges its own hooks;
   Codex's installer preserves `notify`, but refuses to overwrite an existing
   unrelated `hooks.Stop` entry, which must be merged manually.
   ```bash
   uv run python scripts/manage_claude_hook.py install
   uv run python scripts/manage_pi_adapter.py install
   uv run python scripts/manage_codex_hook.py install
   ```
   Claude/Codex hooks invoke `python3` from the agent's `PATH`, so keep a suitable
   Python installation available there too. Their hook commands point into this
   checkout; reinstall them if you move it.

   The Pi installer replaces the installed adapter rather than merging local
   edits. Installers resolve symlinks, so review linked files and their owning
   repository before installing or uninstalling; do not replace a managed link
   with a separate copy. These adapters are separate from Herdr's built-in
   agent integrations: updating Herdr alone does not update them.
   Run `/reload` in existing Pi sessions. Restart existing Claude Code and
   Codex sessions so they load their new hooks. Each command also supports
   `uninstall`.

   Codex hooks are experimental and must be enabled in `~/.codex/config.toml`:
   ```toml
   [features]
   hooks = true
   ```
   Use `hooks`, not the deprecated `codex_hooks` alias. Codex clients can share
   a long-lived app-server; after upgrading Codex or changing this setting,
   restart that app-server as well as the Codex pane.
7. Run the bot:
   ```bash
   scripts/run_bot.sh
   ```

## First verification

1. With `enable_auto_threads: true`, pane threads appear under the thread
   parent channel for detected agents, named `alias/agent` (🔴 while blocked).
   Allow for `auto_threads.refresh_seconds` (30 seconds by default).
2. Open a pane's thread and post a message from an allowlisted account — it is
   sent to the pane and gets a ✅ reaction (⚠️ on failure). This confirms the
   send operation, not completion of an agent turn. Harness commands can also be
   sent as normal messages; hook mode will not mirror screen-only results.
3. Complete a turn in a reloaded/restarted Claude Code, Pi, or Codex pane — its
   full final response appears in the corresponding thread.
4. With watcher and approval enabled, a blocked pane posts a target card with
   Approve / Deny buttons and mentions the allowed users. A supported pending
   Claude question uses its own choices instead of generic approval buttons.

## Configuration reference

The following matches `config.example.yaml`; enable the features you need.
Keep `database_path` stable when upgrading to retain thread bindings and
notification history. If omitted, the code still uses the legacy filename
`herdr-discord-bridge.sqlite3`, not the example's `herdrelay.sqlite3`.

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
  blocked_notification_delay_seconds: 1.5 # lets question hooks arrive first
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
stop:                          # interrupt sequence for the Cancel button
  method: send_keys
  keys: ["C-c"]
```

## Security

Mention commands check `allowed_guild_ids` / `allowed_channel_ids` before
reading Herdr state or resolving a bound pane, including help and unknown
commands. `start` additionally checks `allowed_user_ids`, `enable_send`, the
message length limit, and `dangerous_text_blocklist`; `stop` checks
`allowed_user_ids` and `enable_stop`. A thread is allowed when its parent channel
or its own ID is allowlisted. Restrict bot visibility and channel membership
through Discord permissions as well.

- Normal bound-thread posts check `allowed_guild_ids` / `allowed_channel_ids`,
  `allowed_user_ids`, and `enable_send`. Empty guild/channel lists impose no
  location restriction; an empty user list denies guarded write actions.
- Approval, denial, question-choice, and Cancel buttons check the user allowlist
  and their feature flags. Watcher-generated approval/choice buttons use
  user-level checks rather than rechecking guild/channel allowlists.
- `dangerous_text_blocklist` rejects matching phrases in normal forwarded
  messages and `@herdr start` commands. It is a text filter, not a shell sandbox.
- Ambiguous target queries are rejected rather than choosing a matching pane.
- Mention-command successes, rejections, and failures are recorded in
  `audit_log` as `control_start`, `control_stop`, `control_list`, `control_help`,
  or `control_unknown`. Start/stop record `attempt` before Herdr calls and `ok`
  with the resulting/closed pane ID before replying to Discord. If the initial
  audit write fails, the mutation is not attempted. An error after a Herdr call
  does not prove that nothing happened; inspect the pane before retrying.
- Bound-thread send attempts and successful button actions are also recorded;
  not every rejected button action is recorded.
- Hook files contain assistant responses and questions. Built-in adapters write
  files with mode `0600` and create the inbox with mode `0700`. Delivered and
  duplicate events are removed; invalid events and expired events without a
  thread mapping are also discarded. Restrict access to the Discord parent
  channel: automatic pane threads are public threads within that channel.

## Running in the background

On macOS:

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
three gzip-compressed backups. On other Unix-like systems, use your own service
manager to run `scripts/run_bot.sh` with access to the same Herdr session.

## Operational notes

- `.env`, `config.yaml`, and the SQLite database are gitignored.
- Sending uses the Herdr CLI (`agent prompt`). Hook-mode output relay does
  not call `pane read` or `agent read`. The optional legacy `poll` mode does,
  and its `visible` fallback can capture pager/TUI output such as `/usage`.
  Status notifications use the raw socket API.
- Thread names add `🔴` on entering blocked status and remove it on leaving.
  Changes between working/idle/done do not change the status prefix; alias or
  agent-name changes can still rename a thread.
- Status-notification dedupe is stored in SQLite using the pane ID, status, and
  a hash of the tail (or state metadata when `include_output: false`). Hook
  response dedupe instead uses the agent and adapter-provided event ID.
- Pane ↔ thread mappings are stored in SQLite (`agent_threads`).
- Thread archival uses a successful `pane list` snapshot, not the detected-agent
  list. A restart that restores shells without agents does not change their
  threads' names or archive states. Previously archived threads are reactivated
  only when an agent returns. Failed or malformed pane snapshots do not trigger
  archival. Once an agent returns, its existing thread is reused. A move to
  another workspace changes the Herdr pane ID; thread mappings are not migrated,
  so the old thread is archived and a new agent thread can be created.
- Status subscriptions include existing shell panes so agents launched there
  later are monitored. Pane creation, closure, and movement events trigger a
  refresh even with `resubscribe_interval_seconds: 0`. A post-acknowledgement
  snapshot catches topology changes during subscription setup. With no panes,
  the watcher waits and retries without sending an empty subscription or
  logging repeated crash tracebacks.
- This does not restart agents or enable shell input: with
  `allow_pane_send_fallback: false`, ordinary messages still require an agent.
  After a Herdr restart, resume agents in their existing panes as needed.

### Agent output adapters

Hook mode is agent-neutral. An adapter atomically places a `.json` file in
`streaming.hook_inbox_path` with this format:

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
event deduplication. Delivery is not transactional across message chunks: a
retry after partial delivery can repeat earlier chunks. If you change
`streaming.hook_inbox_path`, also set `HERDRELAY_HOOK_INBOX` to the same absolute
path in the agent environments (including Codex's app-server); adapters do not
read `config.yaml`. Events wait for a thread mapping, up to
`hook_max_event_age_seconds`; invalid or oversized events are discarded.

`scripts/agent_stop_hook.py` handles Claude Code and Codex Stop events and
supported Claude `AskUserQuestion` prompts. The question adapter currently
requires exactly one question per tool call; multi-question calls are not
relayed. A single-choice question with up to five options uses buttons; larger
option lists and multi-select questions use a select menu (up to 25 options).
A single numeric post such as `2` can also answer a pending question; use the
select menu for multiple selections. Choices have no timeout and are restored
from SQLite after a bridge restart.

Claude's `PostToolUse` clears a question after it is answered; `UserPromptSubmit`
clears it when a new prompt is submitted, and a delivered final response also
clears it. Pressing Esc alone does not immediately send a resolution event.
Generic approval controls are suppressed while a question is pending. Question
selection sends terminal keys assuming the dialog cursor and selections have
not already been changed manually; avoid mixing terminal navigation with
Discord choice buttons for the same question.

`integrations/pi-herdrelay-output.ts` handles
Pi's `input` and `agent_settled` events. A `human_input` adapter event is emitted
only for `ctx.mode === "tui"` with `event.source === "interactive"`; it contains
no prompt text and makes `allowed_user_ids` members of the existing Discord
thread without posting a message. RPC and extension-generated input, retries,
continuations, and tool/subagent activity do not make a thread appear in the
Discord sidebar. Input delivered by typing into the Pi TUI—including text
forwarded into that terminal from a bound Discord thread—is interactive input.

Output relay also requires `ctx.mode === "tui"`; RPC, JSON, print,
SDK/unknown contexts are excluded even if they inherit Herdr's pane environment.
In particular, `ctx.hasUI` is not a TUI check because it is also true in RPC mode.
The adapter only relays output: it never injects a system prompt or forces a
Discord reply. This project does not depend on Pi's optional modal `ask_user`
package. After reinstalling the adapter, run `/reload` in existing Pi sessions
(or start a new Pi process) to load it. Sessions that have not reloaded cannot
emit the new interactive-input marker; they become eligible after reload and
then after the next submitted prompt. Adding a member does not let HerdRelay set
an arbitrary Discord sidebar order. Threads move naturally with Discord message
activity when their relayed response arrives. Existing memberships are not
removed automatically.

Codex hooks run in its shared app-server,
which does not inherit `HERDR_PANE_ID`; HerdRelay resolves the hook to the only Codex pane
with the same cwd using Herdr pane metadata. If multiple Codex panes share that
cwd, it intentionally does not deliver an ambiguous response. Additional agents
can use the same protocol without changing the Discord relay or reading terminal
contents.

### Tests

Run `uv run pytest -q` for the Python tests and Pi adapter regression tests.
The adapter tests need Node.js 22.6 or newer (they are skipped without it).
They execute the actual TypeScript adapter with isolated event contexts and
inboxes, without contacting a model or Discord. To run them directly:

```bash
node --experimental-strip-types --test tests/pi_output_adapter.test.mjs
```

### Reduce terminal reads and periodic reconnects

These optional settings were introduced while investigating fullscreen redraws
on Herdr 0.8. They remain useful for a quieter hook-only setup; this is not a
claim that the old redraw issue persists in Herdr 0.9.5. They leave message send
and hook output relay enabled when the corresponding feature flags are on:

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
watcher tail reads, and periodic subscription reconnects. Topology-triggered
subscription refreshes remain enabled so new panes are monitored without a
HerdRelay restart. Auto-thread creation and hook output relay continue.
Set `enable_watcher: false` to disable status subscriptions, or
`enable_streaming: false` to disable response relay.
