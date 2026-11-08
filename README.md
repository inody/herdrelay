# HerdRelay

HerdRelay is a self-hosted Discord control surface for [Herdr](https://herdr.dev/).
It creates one Discord thread for each Herdr pane that contains a detected coding
agent, forwards Discord messages to that pane, and relays completed agent
responses back to the thread.

## What HerdRelay is — and is not

HerdRelay keeps Herdr as the source of truth for panes, agents, working
directories, and lifecycle status. Discord is a remote interface to those local
panes, not a second session manager.

HerdRelay provides:

- automatic public Discord threads for Herdr panes with detected agents;
- text input, selected terminal keys, agent start, and pane close controls;
- completed-response relay for Claude Code, Codex, and Pi through adapters;
- optional blocked/done/working status notifications;
- configurable Approve, Deny, and interrupt controls;
- structured controls for supported Claude `AskUserQuestion` prompts; and
- SQLite-backed thread mappings, deduplication, pending questions, and audit
  records.

HerdRelay is **not**:

- a hosted service or a Discord bot that runs agents by itself;
- a shell sandbox or a replacement for Discord channel permissions;
- a complete terminal mirror in the recommended hook mode;
- a multi-machine Herdr controller (it addresses one local Herdr session);
- a message queue, scheduler, file-upload bridge, or session-resume system; or
- a slash-command bot. Harness commands such as `/status` and `/compact` are
  sent as ordinary thread messages.

## Intended users and use cases

HerdRelay is intended for a single user or a small trusted team who already run
coding agents inside Herdr and want to:

- continue a turn from a phone or another computer;
- receive a Discord notification when an agent blocks or finishes;
- approve, deny, or interrupt an agent without opening the terminal; and
- keep one durable Discord conversation surface per local Herdr pane.

It is not designed for untrusted public servers or for exposing arbitrary shell
access to many users.

## Supported and verified environment

HerdRelay requires a Unix-like host because the watcher connects to Herdr's
local Unix socket. The supplied service scripts are for macOS `launchd`; other
Unix-like systems need their own service manager.

| Component | Requirement or verified version |
|---|---|
| Python | 3.12 or newer |
| Herdr | local CLI and server; verified with client/server 0.9.5 |
| Pi | output adapter verified with Pi 1.5.0 |
| Discord library | `discord.py` 2.x |
| Package manager | `uv` |
| Development tests | Node.js 22.6+ for the Pi adapter test |

The repository also includes hook adapters for Claude Code and Codex. Their hook
interfaces are covered by tests, but those clients are not pinned. Codex hooks
are experimental and must be enabled explicitly.

Check both sides of Herdr before diagnosing compatibility:

```bash
herdr --version
herdr status
```

An updated CLI does not imply that the running server was updated.

## Architecture

```text
Claude/Codex Stop hook ─┐
Pi TUI extension ───────┴─> private JSON inbox ─> HerdRelay ─> Discord threads
Discord thread message ─────────────────────────> HerdRelay ─> Herdr CLI
Herdr Unix event socket ─> status watcher ──────> HerdRelay ─> Discord threads
Herdr pane list ─────────> thread synchronizer ─> Discord threads
HerdRelay <─────────────────────────────────────> SQLite state/audit log
```

At a useful operational level:

1. The thread synchronizer polls `herdr pane list`. A pane gets a public Discord
   thread only after Herdr reports a detected agent in that pane.
2. A normal message in a bound thread is sent with `herdr agent prompt`. The
   optional `allow_pane_send_fallback` setting can fall back to `herdr pane run`.
3. Agent adapters write completed responses to a private local inbox. HerdRelay
   finds the pane's thread, splits long text for Discord, disables mentions, and
   records the event as delivered.
4. The optional watcher subscribes directly to Herdr's Unix socket for pane
   lifecycle events and posts configured status cards.
5. `database_path` stores pane/thread bindings, audit entries, event dedupe
   keys, and pending Claude questions.

## Prerequisites

Before starting, have:

- a local Herdr server and CLI that address the same session;
- Python 3.12+ and `uv`;
- a Discord server where you can install a bot;
- the bot token, guild ID, parent text-channel ID, and your Discord user ID; and
- at least one supported agent running in a Herdr pane for end-to-end testing.

The next two setup sections explain how to obtain the Discord values and install
agent adapters.

## Minimal quick start

The following is the shortest recommended hook-mode setup. Substitute your
actual Discord IDs.

```bash
git clone https://github.com/inody/herdrelay.git
cd herdrelay
uv sync
cp .env.example .env
cp config.example.yaml config.yaml
```

Set the bot token in `.env`:

```dotenv
DISCORD_TOKEN=your-bot-token
HERDR_SOCKET_PATH=
```

Replace `config.yaml` with at least:

```yaml
allowed_guild_ids:
  - 123456789012345678
allowed_channel_ids:
  - 234567890123456789
allowed_user_ids:
  - 345678901234567890
thread_parent_channel_id: 234567890123456789

database_path: herdrelay.sqlite3

enable_send: true
enable_approve: true
enable_watcher: true
enable_stop: true
enable_auto_threads: true
enable_streaming: true

streaming:
  mode: hooks

watcher:
  statuses: ["blocked", "done"]

approval:
  codex: {method: send_text_enter, text: "y"}
  claude: {method: send_keys, keys: ["Enter"]}
  pi: {method: send_keys, keys: ["Enter"]}
deny:
  codex: {method: send_keys, keys: ["Escape"]}
  claude: {method: send_keys, keys: ["Escape"]}
  pi: {method: send_keys, keys: ["Escape"]}
stop:
  method: send_keys
  keys: ["C-c"]
```

Install the adapter for every agent you use:

```bash
uv run python scripts/manage_claude_hook.py install
uv run python scripts/manage_pi_adapter.py install
```

For Codex, first set `[features] hooks = true` in `~/.codex/config.toml`, then
run:

```bash
uv run python scripts/manage_codex_hook.py install
```

Then run HerdRelay from the checkout:

```bash
scripts/run_bot.sh
```

Within `auto_threads.refresh_seconds` (30 seconds by default), an existing Herdr
pane with a detected agent should get a thread named `alias/agent`. Post a
message from an allowlisted account: a successful handoff gets a ✅ reaction.
Complete an agent turn to verify that its final response appears in the thread.
The reaction confirms input submission, not completion of the turn.

## Discord setup

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application and add a bot.
2. Copy the bot token into `.env` as `DISCORD_TOKEN`.
3. On the bot page, enable **Message Content Intent**. It is required for the
   recommended setup so HerdRelay can read normal messages in pane threads.
4. In the OAuth2 URL Generator, select the `bot` scope and these permissions:

   - View Channels
   - Send Messages
   - Read Message History
   - Create Public Threads
   - Send Messages in Threads
   - Manage Threads — needed to rename, archive, and unarchive pane threads
   - Add Reactions — needed for ✅/⚠️ input feedback
   - Change Nickname — optional; HerdRelay tries to name itself `herdr`

5. Open the generated URL and install the bot in your server.
6. Enable Discord Developer Mode and copy:

   - the server (guild) ID;
   - the parent **text channel** ID where pane threads will be created; and
   - each trusted user's ID.

Put the parent channel in both `thread_parent_channel_id` and
`allowed_channel_ids`. A thread is location-allowed when either its own ID or
its parent channel ID is allowlisted.

## Agent adapter setup

Hook-mode response relay is recommended because it does not read terminal
contents. The optional watcher can still read status-card tails when
`watcher.include_output` is true.

### Claude Code

```bash
uv run python scripts/manage_claude_hook.py install
```

The installer merges HerdRelay entries into `~/.claude/settings.json` for
`Stop`, `PreToolUse`, `PostToolUse`, and `UserPromptSubmit`, preserving unrelated
hooks. Restart existing Claude Code sessions after installation.

Claude `AskUserQuestion` support currently requires exactly one question in the
tool call. Up to 25 options receive Discord components; larger questions must be
answered in the terminal, although a single-choice question can still be
answered by posting one option number.

### Pi

```bash
uv run python scripts/manage_pi_adapter.py install
```

This installs `~/.pi/agent/extensions/herdrelay-output.ts`. Run `/reload` in
existing Pi sessions or start a new Pi process.

Only interactive TUI sessions are relayed. RPC, JSON, print, SDK, and unknown
contexts are ignored. After an interactive Pi input, the adapter adds configured
users to the existing public thread; it does not copy the prompt or inject
instructions into the agent.

### Codex

Enable experimental hooks in `~/.codex/config.toml`:

```toml
[features]
hooks = true
```

Then install the Stop hook:

```bash
uv run python scripts/manage_codex_hook.py install
```

The installer preserves `notify` and unrelated configuration, but refuses to
overwrite an existing unrelated `hooks.Stop`; merge that case manually. Restart
existing Codex sessions and any long-lived Codex app-server.

Codex's shared app-server may not inherit `HERDR_PANE_ID`. The adapter therefore
matches the hook to the only Codex pane with the same working directory. It
intentionally drops ambiguous output when multiple Codex panes share that
working directory.

### Adapter paths, backups, and removal

Claude and Codex hook commands contain an absolute path into this checkout.
Reinstall them after moving the repository. All installers create backups before
changing an existing file. The Pi installer replaces the managed adapter rather
than merging local edits.

The installers resolve symlinks before editing. Review linked files and their
owning repository before running them; do not replace a managed link with an
independent copy.

To remove adapters, replace `install` with `uninstall` and reload/restart the
corresponding agent.

If you change `streaming.hook_inbox_path`, also export the same absolute path as
`HERDRELAY_HOOK_INBOX` in every agent environment, including Codex's app-server.
Adapters do not read `config.yaml`.

## Normal usage

### Send text and harness commands

Post a normal message in a pane thread. No reply or mention is needed. Commands
such as `/status`, `/compact`, `/usage`, and `/model` are sent as plain text to
the agent harness; HerdRelay itself registers no slash commands.

Hook mode relays completed assistant responses. Screen-only menus, pagers, and
harness status pages are not returned unless the optional poll mode observes
them.

### Send terminal keys

Post one of these messages instead of text:

```text
!esc  !enter  !tab  !space
!up   !down   !left !right
!ctrl-c  !backspace
!pgup !pgdn !home !end
```

These are useful for pagers and TUI menus. `!pgup`, `!pgdn`, `!home`, and `!end`
are sent as terminal escape sequences because Herdr 0.9 does not expose those
key names.

### Mention controls

Mention the bot to use these commands:

```text
@herdr start <name> [--cwd PATH] [--workspace ID] [--tab ID]
                    [--split right|down] [-- <agent arguments...>]
@herdr stop [<pane_id>]
@herdr list
@herdr help
```

`start` uses `<name>` as both the unique Herdr agent name and its kind, for
example `pi`, `claude`, or `codex`. It does not accept an arbitrary executable.
Inside a pane thread, omitted workspace, tab, and cwd values are inherited and
the new pane splits that thread's pane. Elsewhere, HerdRelay splits the focused
pane or creates a workspace when none is focused. The default split is `right`.

`stop` closes the pane. This is different from a card's **Cancel** button, which
only sends the configured interrupt sequence after confirmation.

`start` requires `enable_send`; `stop` requires `enable_stop`. `list` and `help`
need an allowed Discord location but do not require a write user.

### Status and question controls

- `blocked` cards can show **Approve** and **Deny** when `enable_approve` and the
  corresponding strategies are configured.
- `working` cards can show **Cancel** when `working` is included in
  `watcher.statuses`, `enable_stop` is true, and `stop` is configured.
- Supported Claude questions use buttons or a select menu instead of generic
  approval controls. A single numeric message such as `2` can answer a pending
  question.
- Blocked notifications mention every `allowed_user_ids` entry. Actual push
  delivery depends on each user's Discord notification settings.

## Configuration

Start from [`config.example.yaml`](config.example.yaml). All feature flags
default to false; a copied example is inert until you enable the features you
want.

### Access, state, and feature flags

| Setting | Meaning |
|---|---|
| `allowed_guild_ids` | Guild allowlist for reads and actions; empty means no guild restriction |
| `allowed_channel_ids` | Parent-channel or thread allowlist; empty means no channel restriction |
| `allowed_user_ids` | Users allowed to send or mutate; empty denies all guarded writes |
| `thread_parent_channel_id` | Parent text channel for automatic public threads |
| `database_path` | SQLite state and audit database; keep it stable across upgrades |
| `max_output_chars` | Per-message output budget before Discord chunking/truncation rules |
| `max_message_chars` | Maximum inbound message/control length |
| `enable_send` | Thread input and `@herdr start` |
| `enable_approve` | Approve, Deny, and structured question answers |
| `enable_watcher` | Herdr socket status notifications |
| `enable_stop` | Cancel controls and `@herdr stop` |
| `enable_auto_threads` | Pane discovery, public thread creation, and binding |
| `enable_streaming` | Hook or poll output relay |
| `allow_pane_send_fallback` | Fall back from `agent prompt` to `pane run`; normally keep false |
| `dangerous_text_blocklist` | Case-insensitive substring filter for thread input and `start` text |

If `database_path` is omitted, the code retains the legacy default
`herdr-discord-bridge.sqlite3`; the example explicitly uses
`herdrelay.sqlite3`.

### Nested groups

- `auto_threads.refresh_seconds`: pane/thread reconciliation interval.
- `streaming.mode`: `hooks` (recommended) or `poll`.
- `streaming.refresh_seconds`, `tail_lines`, `initial_tail_lines`, and
  `enable_visible_fallback`: poll-mode behavior.
- `streaming.hook_inbox_path`, `hook_refresh_seconds`,
  `hook_max_event_bytes`, and `hook_max_event_age_seconds`: adapter inbox
  behavior.
- `watcher.statuses`: any Herdr statuses to notify, normally `blocked` and
  `done`; add `working` to expose Cancel cards.
- `watcher.reconnect_delay_seconds` and `resubscribe_interval_seconds`: socket
  reconnect behavior; zero disables periodic resubscription, not
  topology-triggered refreshes.
- `watcher.include_output`, `blocked_tail_lines`, and `done_tail_lines`: whether
  status cards read and attach terminal tails.
- `watcher.blocked_notification_delay_seconds`: short delay that lets a Claude
  question event replace a generic approval card.
- `herdr.cli_path`, `default_source`, and `command_timeout_seconds`: Herdr CLI
  invocation settings.
- `approval.<agent>`, `deny.<agent>`, and `stop`: terminal action strategies.
  Supported methods are `send_keys` and `send_text_enter`.

Environment variables:

| Variable | Meaning |
|---|---|
| `DISCORD_TOKEN` | Required Discord bot token |
| `HERDR_SOCKET_PATH` | Optional socket for a non-default local Herdr session |
| `HERDR_SESSION` | Used by the watcher to derive a named local session socket when no explicit path is set |
| `HERDRELAY_HOOK_INBOX` | Adapter-side override; must match the configured inbox |

See [Operations](docs/operations.md) for the hook event format, state lifecycle,
and lower-noise watcher settings.

## Security and limitations

Treat the Discord parent channel as remote terminal access.

- Restrict the bot with both Discord channel permissions and the configured
  guild, channel, and user allowlists. Empty guild/channel lists are permissive;
  an empty user list denies guarded writes.
- Automatic pane threads are **public threads** within the parent channel, not
  private threads. Discord permissions remain the primary visibility boundary.
- `dangerous_text_blocklist` is a substring filter, not command parsing, policy
  enforcement, or sandboxing.
- Approval and question controls send configured keystrokes to the current TUI.
  Do not operate the same prompt simultaneously from Discord and the terminal.
- Hook files contain assistant responses and question text. Built-in adapters
  create the inbox as mode `0700` and files as `0600`; protect the host account
  and any backups.
- Output messages disable Discord mentions. Blocked status notifications
  intentionally mention allowed users.
- Hook delivery is deduplicated per adapter event, but multi-chunk delivery is
  not transactional. A retry after a partial Discord failure can repeat earlier
  chunks.
- Hook mode does not relay tool progress, terminal menus, or arbitrary screen
  changes. Poll mode can capture more terminal content but increases terminal
  reads and may expose screen-only information.
- HerdRelay does not follow the remote machine selected in the Herdr TUI. CLI
  calls and the socket watcher must address the same local Herdr session.
- Moving a pane to another workspace changes its Herdr pane ID. The old thread
  is archived and a new thread may be created; mappings are not migrated.
- A shell without a detected agent keeps an existing thread but does not create
  a new one. Sending normally still requires a detected agent.
- Control commands and successful actions are audited, but not every rejected
  button click is recorded. If the initial audit write for `start` or `stop`
  fails, HerdRelay refuses the mutation.

## Operations and troubleshooting

Run in the foreground with:

```bash
scripts/run_bot.sh
```

On macOS, install the supplied user LaunchAgent:

```bash
scripts/install_launchd.sh
scripts/status_launchd.sh
```

After changing `.env`, `config.yaml`, or code:

```bash
launchctl kickstart -k "gui/$UID/dev.herdrelay"
```

Logs are written to `logs/launchd.out.log` and `logs/launchd.err.log`. The
installer also runs hourly copy-truncate rotation above 10 MiB and keeps three
compressed backups. Remove both LaunchAgents with:

```bash
scripts/uninstall_launchd.sh
```

Common checks:

| Symptom | Check |
|---|---|
| No pane thread | Enable `enable_auto_threads`; set a valid parent text channel; confirm `herdr pane list` shows a detected agent; wait one refresh interval |
| Thread input gets ⚠️ | Confirm the user/location allowlists, `enable_send`, blocklist, message length, and that Herdr still detects an agent |
| No completed response | Enable `enable_streaming`; install/reload the correct adapter; confirm the inbox paths match; restart Claude/Codex sessions |
| No status card | Enable `enable_watcher`; include the status in `watcher.statuses`; confirm the pane already has a thread binding |
| Wrong or missing events | Compare `HERDR_SOCKET_PATH`/`HERDR_SESSION` with `herdr status`; the CLI and watcher must target the same session |
| Codex output missing | Enable Codex hooks, restart its app-server, and avoid multiple Codex panes with the same cwd |
| Pi output missing | Run `/reload`; only interactive TUI turns are eligible |
| LaunchAgent fails | Check both log files and ensure `uv`, `herdr`, and agent executables are reachable from the LaunchAgent `PATH` |

Detailed thread lifecycle, watcher behavior, hook protocol, state files, and
failure semantics are in [`docs/operations.md`](docs/operations.md).

## Development and tests

Install development dependencies and run the complete suite:

```bash
uv sync --extra dev
uv run pytest -q
```

The Python suite also invokes the Pi adapter regression test when Node.js 22.6+
is available; otherwise that test is skipped. Run it directly with:

```bash
node --experimental-strip-types --test tests/pi_output_adapter.test.mjs
```

The adapter tests execute the real TypeScript extension with isolated event
contexts and inboxes. They do not contact Discord or a model.

Repository layout:

- `src/herdr_discord_bridge/` — bot, Herdr client, thread/watcher/stream logic,
  security policy, and SQLite store;
- `integrations/` — Pi extension source;
- `scripts/` — adapter installers, hook writer, runner, and launchd helpers;
- `tests/` — unit and regression tests; and
- `docs/operations.md` — deeper operational behavior.
