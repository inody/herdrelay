# HerdRelay

HerdRelay connects [Herdr](https://herdr.dev/) runtimes to authorized chat
transports. It routes prompts, lifecycle notifications, and completed agent
responses between Herdr Core or the legacy TUI and Discord or Telegram.

## What HerdRelay is — and is not

HerdRelay keeps Herdr as the source of truth for terminals, panes, agents,
working directories, and lifecycle status. Discord and Telegram are remote
interfaces to those local runtimes, not additional session managers.

HerdRelay provides:

- Discord threads and Telegram forum topics for detected agents;
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

The supported production path is Herdr Core with Discord and/or Telegram.
Legacy Herdr TUI with Discord remains available for compatibility. TUI with
Telegram has automated contract coverage but no live acceptance guarantee and
is outside the supported operational matrix. Core provides pinned Discord
bindings, native status events, normal prompt submission, and interrupt.
Telegram provides `/agents`, `/help`, blocked/done notifications, persisted
topic lifecycle, and opt-in prompt submission; one-time confirmed interrupt
remains Core-only. Adapter output fans out
independently to every persisted Discord and Telegram route. Physical
Apple Watch behavior remains unverified. See
[Herdr Core and Telegram architecture](docs/core-telegram-design.md) and the
[implementation plan](docs/core-telegram-plan.md) for the current boundary and
remaining stages.

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
contexts are ignored. In Herdr Core terminals, the adapter emits version 2
identity using `HC_TERMINAL_ID` and the stable Core ID. After an interactive Pi
input, the adapter adds configured users to the existing public thread; it does
not copy the prompt or inject instructions into the agent.

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

For the legacy TUI, Codex's shared app-server may not inherit
`HERDR_PANE_ID`. The adapter therefore matches the hook to the only Codex pane
with the same working directory and drops ambiguous output. Core events require
an inherited `HC_TERMINAL_ID` plus a verifiable Core ID; HerdRelay does not use
working-directory matching to guess a Core terminal.

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

The preferred schema groups settings by responsibility:

- `runtime`: TUI/Core selection and runtime-specific connection policy;
- `storage`: the stable SQLite database path;
- `relay`: shared output limits, hook streaming, and text safety policy; and
- `transports.discord` / `transports.telegram`: destination-specific access,
  features, and controls.

For backward compatibility, the former flat keys remain accepted. New
configurations should use the grouped form in `config.example.yaml`.

If `storage.database_path` is omitted, the preferred default is
`herdrelay.sqlite3`. An existing legacy `herdr-discord-bridge.sqlite3` is
reused only when the preferred file does not yet exist.

### Nested groups

- `runtime.provider`: `herdr-tui` (default) or `herdr-core`.
- `transports.discord`: gateway enablement, guild/channel/user allowlists,
  automatic threads, watcher controls, mutation flags, and approval strategies.
  `enabled` defaults to true for existing installations; set it false to run
  Telegram without opening a Discord gateway connection.
- `runtime.herdr_tui`: legacy CLI path, read source, and command timeout.
  The former top-level `herdr` group remains accepted for compatibility.
- `runtime.herdr_core`: existing socket selection, timeouts, visibility policy,
  and exact target allowlists. Inline `allowed_terminal_addresses` and the
  optional private `allowed_terminal_addresses_file` are merged; an empty
  result denies all Core targets. The former `runtime.core` name remains
  accepted for compatibility.
- `transports.telegram`: supported with Herdr Core; legacy TUI compatibility is
  automated-test-only and not operationally guaranteed. Enabling it requires
  exact nonempty chat/user allowlists, `forum_chat_id`, and
  `TELEGRAM_BOT_TOKEN`; `statuses` is limited to `blocked` and `done`, while
  transport-specific `enable_send` and `enable_interrupt` default to false.
  With `relay.enable_streaming: true` and
  `relay.streaming.mode: hooks`, legacy version 1
  and Core-aware version 2 adapter responses are delivered to the exact
  persisted target topic. `status_refresh_seconds` controls legacy TUI
  reconciliation; Core uses its native event stream.
  Interrupt confirmations are actor/topic bound, single-use, and expire after
  `confirmation_ttl_seconds`. Per-minute actor, chat, and target mutation limits
  are configured independently.
- `transports.discord.auto_threads.refresh_seconds`: pane/thread reconciliation
  interval.
- `relay.streaming.mode`: `hooks` (recommended) or `poll`; the remaining
  `relay.streaming` fields configure polling and the private adapter inbox.
- `transports.discord.watcher.statuses`: any Herdr statuses to notify, normally
  `blocked` and `done`; add `working` to expose Cancel cards.
- `transports.discord.watcher.reconnect_delay_seconds` and
  `resubscribe_interval_seconds`: socket
  reconnect behavior; zero disables periodic resubscription, not
  topology-triggered refreshes.
- `transports.discord.watcher.include_output`, `blocked_tail_lines`, and
  `done_tail_lines`: whether
  status cards read and attach terminal tails.
- `transports.discord.watcher.blocked_notification_delay_seconds`: short delay
  that lets a Claude question event replace a generic approval card.
- `transports.discord.approval.<agent>`, `deny.<agent>`, and `stop`: terminal
  action strategies.
  Supported methods are `send_keys` and `send_text_enter`.

Environment variables:

| Variable | Meaning |
|---|---|
| `DISCORD_TOKEN` | Required only when `transports.discord.enabled` is true |
| `TELEGRAM_BOT_TOKEN` | Required only when `transports.telegram.enabled` is true |
| `HERDR_SOCKET_PATH` | Optional socket for a non-default local Herdr session |
| `HERDR_SESSION` | Used by the watcher to derive a named local session socket when no explicit path is set |
| `HERDRELAY_HOOK_INBOX` | Adapter-side override; must match the configured inbox |

See [Operations](docs/operations.md) for the hook event format, state lifecycle,
and lower-noise watcher settings.

## Security and limitations

Treat every enabled Discord or Telegram destination as remote terminal access.

- Restrict Discord with both channel permissions and the configured guild,
  channel, and user allowlists. Empty Discord guild/channel lists are
  permissive; an empty user list denies guarded writes.
- Telegram supports `/agents`, `/help`, blocked/done notifications, and
  adapter responses in persisted forum topics. Herdr Core is the supported
  production runtime; legacy TUI behavior is best-effort only. It requires an
  allowlisted forum supergroup and exact user/chat IDs. Ordinary text performs
  no runtime mutation unless `transports.telegram.enable_send` is explicitly
  enabled. `/interrupt` remains Core-only and additionally requires
  `enable_interrupt` and a one-time confirmation button.
- Telegram prompts and Core interrupts are atomically claimed and audited before
  the runtime is called. A timeout or lost response is recorded as
  `outcome_unknown` and is never retried automatically.
- Core Discord bindings persist the Core identity with the terminal address.
  A binding from a different Core instance is rejected instead of being applied
  to a reused terminal address.
- Version 2 output dedupe is route-specific. Discord delivery never suppresses
  Telegram delivery, or vice versa; the inbox file is removed only after every
  currently persisted route is complete.
- Telegram bots cannot use Secret Chats. Messages and agent metadata sent there
  are stored by Telegram's cloud service.
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

Run the configured transports in the foreground with:

```bash
scripts/run_bot.sh
```

Run Telegram without starting or connecting a Discord client with:

```bash
uv run herdrelay-telegram --config /path/to/telegram-config.yaml
```

Setting `transports.discord.enabled: false` makes the main `herdrelay` command
run Telegram alone. The explicit standalone command also does not require
`DISCORD_TOKEN`, but it still requires
an enabled `transports.telegram` block, `TELEGRAM_BOT_TOKEN`, an available
configured runtime, exact chat/user allowlists, and a separate stable SQLite
database. Core additionally requires exact terminal allowlists. Set
`enable_streaming: true` with `streaming.mode: hooks` to relay adapter responses.
Use it for isolated acceptance testing
before enabling Telegram in the Discord service.

On macOS, install the supplied user LaunchAgent:

```bash
scripts/install_launchd.sh
scripts/status_launchd.sh
```

Manage file-backed Core targets locally without exposing discovery over a chat
transport:

```bash
uv run herdrelay-targets --config config.yaml list
uv run herdrelay-targets --config config.yaml allow t12
uv run herdrelay-targets --config config.yaml remove t12
```

`allow` accepts only an eligible live agent from the configured Core. The file
is replaced atomically with mode `0600`. Restart HerdRelay after a change.
Inline targets remain readable but are intentionally not removed by this CLI.

After changing `.env`, `config.yaml`, the target allowlist, or code:

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

For development of the proposed Herdr Core backend, check the installed `hc`
CLI schema with:

```bash
uv run python scripts/check_hc_compatibility.py
```

This static probe runs only `hc --version` and `hc schema`; it does not contact
or start Herdr Core. It checks the methods and parameters required by the MVP.
The experimental runtime backend separately performs a generation-1 `hello`
handshake over an existing local Core socket. It never runs `hc server status`
or another command that may start Core.

An isolated live contract test is also available:

```bash
HERDRELAY_RUN_HC_LIVE_TEST=1 uv run pytest -q tests/test_hc_live.py
```

It creates a temporary HOME/XDG/session/socket, explicitly starts a test-owned
Core, launches one synthetic status-reporting agent, verifies
list/read/send/event subscription, and removes the test processes and files. It
never types into an existing
terminal.

Herdr Core is selected with `runtime.provider: herdr-core`. Status watching
uses Core event subscriptions and hook output uses the shared delivery inbox.
The stop command maps only to `terminal.interrupt`; approval and raw-key,
start, close, and kill operations remain disabled. Core targets are
deny-by-default through exact inline or file-backed allowlists. See
`config.example.yaml` and `docs/operations.md` for current settings and rollout
procedures.

Repository layout:

- `src/herdrelay/` — runtime clients, Discord/Telegram transports, delivery
  coordination, security policy, and SQLite state;
- `integrations/` — Pi extension source;
- `scripts/` — adapter installers, hook writer, runner, and launchd helpers;
- `tests/` — unit and regression tests;
- `docs/operations.md` — deeper operational behavior;
- `docs/core-telegram-design.md` — proposed Herdr Core and multi-transport
  architecture; and
- `docs/core-telegram-plan.md` — staged implementation and rollout plan.
