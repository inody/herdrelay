# HerdRelay

HerdRelay connects authorized Discord and Telegram users to local coding agents.
One process selects one runtime backend—Herdr Core, Orca, or the legacy Herdr
TUI—and can expose it through Discord, Telegram, or both transports at the same
time.

The normal product surface is deliberately small:

- discover eligible local agents and give each exact target a Discord thread or
  Telegram forum topic;
- submit ordinary prompts after authorization, rate limiting, audit, and a fresh
  identity check;
- relay lifecycle status, structured questions, and completed responses; and
- route Telegram General and the Discord parent channel through the same
  backend-aware General provider; and
- transcribe bounded Telegram voice messages locally before routing or direct
  delivery.

The selected runtime remains the authority. HerdRelay's direct mutation path
does not provide shell access, arbitrary terminal input, generic approval,
remote Agent launch, or Agent/terminal close through chat. The `router` General
provider only selects an existing Agent. The optional `orca-bot` provider instead
delegates General to a separately configured Orca Bot, whose `SOUL.md` and tool
permissions define its capabilities and confirmation policy.

## Support matrix

| Runtime | Discord | Telegram |
|---|---:|---:|
| Herdr Core | Supported; live send/output acceptance | Supported; live send/output acceptance |
| Orca terminal agents | Supported; live send/output acceptance | Supported; live Pi and Claude send/output acceptance |
| Legacy Herdr TUI | Compatibility path | Best-effort; automated tests only |

Guarded interrupt is Core-only. Core and Orca structured questions can be
shown, but answering them from chat is disabled because neither backend has an
accepted safe public answer operation. Legacy TUI question answers remain
available through Discord when explicitly enabled.

Development requirements:

| Component | Requirement |
|---|---|
| Python | 3.12+ |
| Package manager | `uv` |
| Discord | `discord.py` 2.x |
| Pi adapter tests | Node.js 22.6+ |

The current live contract has been exercised with Herdr Core 0.1.21 and local
Orca public-CLI terminal agents. Runtime versions outside the tested contract
should be validated before production use.

## Architecture

```text
Claude/Codex hooks ─┐
Pi TUI extension ───┴─> private JSON inbox ─> shared delivery tracker
                                                ├─> Discord thread
                                                └─> Telegram forum topic

Discord thread ─┐
Telegram topic ─┴─> authorization + audit ─> runtime backend
                                             ├─> Herdr Core socket
                                             ├─> legacy Herdr CLI
                                             └─> Orca public CLI

Runtime discovery/events ─> status routing ─> enabled chat transports
HerdRelay state/audit    <──────────────────> SQLite
```

Runtime identity and transport destination are independent. Core routes pin the
stable Core ID together with the terminal address; Orca routes pin the runtime
identity while preserving topic continuity by stable host-qualified pane
address. Delivery dedupe is per destination, so successful Discord delivery
never suppresses Telegram, or vice versa.

## Quick start: Telegram with an existing runtime

### 1. Install HerdRelay

```bash
git clone https://github.com/inody/herdrelay.git
cd herdrelay
uv sync
cp .env.example .env
cp config.example.yaml config.yaml
```

### 2. Create the Telegram destination

Use a dedicated Bot and a private forum-enabled supergroup.

1. Create a Bot with BotFather and put its token in `.env`:

   ```dotenv
   TELEGRAM_BOT_TOKEN=your-bot-token
   ```

2. Create a private supergroup and enable **Topics**.
3. Add the Bot. Grant **Manage Topics**; unrelated administration permissions
   should remain disabled.
4. Keep Bot Privacy Mode enabled. An administrator Bot still receives group
   messages.
5. Obtain the exact forum chat ID and your Telegram user ID. Both allowlists must
   be nonempty.

Never commit or paste Bot tokens into configuration, screenshots, logs, or
issues.

### 3. Select a runtime and configure Telegram

For local Orca terminal agents, a minimal send/output configuration is:

```yaml
runtime:
  provider: orca
  orca:
    cli_path: orca
    environment: local
    host_ids: [local]
    include_terminal_agents: true
    include_chats: false
    target_exposure:
      mode: all-eligible

storage:
  database_path: herdrelay.sqlite3

relay:
  enable_streaming: true
  max_message_chars: 2000
  dangerous_text_blocklist: ["rm -rf", "sudo", "git reset --hard"]
  streaming:
    mode: hooks
    hook_inbox_path: ~/.cache/herdrelay/agent-output
    hook_refresh_seconds: 1

transports:
  discord:
    enabled: false
  telegram:
    enabled: true
    mode: forum
    forum_chat_id: -1001234567890
    allowed_chat_ids: [-1001234567890]
    allowed_user_ids: [123456789]
    statuses: [blocked, done]
    enable_send: true
    enable_interrupt: false
    target_absence_grace_seconds: 60
    closed_topic_retention_minutes: 10
```

HerdRelay uses only Orca's public CLI. Before each prompt it resolves the exact
runtime, host, pane, worktree, Agent identity, and writable terminal. A transient
pre-send discovery gap may be refreshed once, but input is never retried after
`terminal send` begins. Orca chats remain excluded because no accepted public
prompt path is available; local terminal agents are supported.

For Herdr Core, select `provider: herdr-core` and configure an already-running
socket:

```yaml
runtime:
  provider: herdr-core
  herdr_core:
    socket_path: ~/.local/state/herdr-core/default/default.sock
    require_app_visible: true
    include_hidden_workers: false
    include_connected_machines: false
    target_exposure:
      mode: all-eligible
```

HerdRelay connects to the configured existing socket and never starts Core.
`all-eligible` exposes eligible local interactive agents while preserving
backend-specific visibility and exact-identity checks. Installations that need a
smaller set can use `selected` mode. The `herdrelay-targets` CLI manages the
optional Core address file only in `selected` mode.

### 4. Install the adapters required by your Agents

Install the Claude hook for Core/TUI output and for Orca-managed Claude
responses and structured questions:

```bash
uv run python scripts/manage_claude_hook.py install
```

Install the Pi adapter when using Pi with Herdr Core or the legacy TUI:

```bash
uv run python scripts/manage_pi_adapter.py install
```

Orca-managed non-Claude terminal Agents, including Pi, use public-CLI completion
polling and do not require the Pi adapter for Orca delivery.

For Codex hooks, first enable hooks:

```toml
[features]
hooks = true
```

Then install its Stop hook:

```bash
uv run python scripts/manage_codex_hook.py install
```

Run `/reload` in existing Pi sessions after installing the Pi adapter. Restart
existing Claude or Codex sessions after installing their hooks.

Adapters write private versioned events to the local inbox. Core events require
`HC_TERMINAL_ID` and a verifiable stable Core ID; HerdRelay does not guess Core
identity from a working directory. Orca-managed Claude hooks use the exact
`ORCA_PANE_KEY` and Orca's public `agent list` CLI to produce version 3
structured-question lifecycle events and completed Claude responses. Other
Orca agents use the in-process public-CLI output source. Assigning one completion
authority per agent avoids duplicate delivery while allowing Claude responses to
arrive before aggregate background work finishes.

### 5. Run HerdRelay

```bash
scripts/run_bot.sh
```

Or install the macOS user LaunchAgent:

```bash
scripts/install_launchd.sh
scripts/status_launchd.sh
```

In Telegram:

- `/agents` lists currently available agents;
- `/help` shows enabled controls;
- `/forget` clears the recent Telegram General destination with the router, or
  starts a new Telegram-specific Orca Bot chat with the Orca Bot provider;
- each eligible runtime target (terminal/pane), not each workspace or Agent
  name, receives one forum topic;
- ordinary topic text submits a prompt only when `enable_send: true` and a
  successful submission adds a 👀 reaction to that message;
- `//reload` sends the agent command `/reload` without treating it as a Bot
  command; and
- `/interrupt` requires `enable_interrupt: true`, backend capability, and a
  one-time confirmation button.

A target returning at the same stable address reopens its existing topic, and an
Orca runtime-ID rotation does not create a new topic. Transient discovery gaps
shorter than `target_absence_grace_seconds` do not close a topic. A genuinely
new pane gets a separate topic even when it has the same workspace and agent
name. Closed topics are deleted after `closed_topic_retention_minutes`; set it
to `null` to keep them indefinitely. The legacy day-based setting remains
accepted, but it cannot be combined with the minute-based setting.

Prompt and interrupt attempts are claimed and audited before the runtime call.
Timeouts after mutation may be recorded as `outcome_unknown` and are never
retried automatically.

### General providers

`relay.general` is transport-neutral. It handles ordinary text in Telegram
General and in the configured Discord `thread_parent_channel_id`. Bound Telegram
topics and Discord child threads always use the direct Agent path.

`provider: auto` is recommended: Orca selects `orca-bot`, while Herdr Core and
the legacy TUI select `router`. Explicit `router`, `orca-bot`, and disabled
configurations remain available. Provider failures never trigger automatic
fallback.

With the router provider, ordinary General text is classified as a request to
one existing Agent. The model receives only opaque candidate IDs and display
metadata, must return structured JSON, and cannot call runtime tools. HerdRelay
verifies the selected live `TargetKey`, requires the forwarded prompt to be an
exact substring of the original utterance, then uses the audited prompt path.
Requests to launch, close, interrupt, or otherwise manage Agents fail closed.

```yaml
relay:
  general:
    enabled: true
    provider: auto
    activation: all-text
    execution_policy: auto-unique
    model:
      provider: openai-compatible
      model: router-small
      base_url: http://127.0.0.1:11434/v1
      timeout_seconds: 5
```

Use `api_key_env` for remote-provider credentials; never put a key in YAML. The
`pi-cli` provider reuses an existing Pi login while forcing an ephemeral,
no-session, no-tools invocation with automatic extension discovery, MCP, skills,
prompt templates, themes, and context files disabled. Trusted provider
extensions required for authentication can be loaded explicitly with the
`extensions` list. `node_path` can supply a stable Node.js installation to a
macOS LaunchAgent. The Pi subprocess receives a minimal environment by default;
only names listed in `pass_env` are copied in addition to HOME/locale/XDG state.
A `command` provider is also available for
local integrations and receives one JSON request on stdin, returning one JSON
decision on stdout. It is invoked as an argv array without a shell. Model
timeout, invalid JSON, ambiguity, invented candidate IDs, or non-verbatim prompt
text all fail closed without retry.

Set `context_ttl_seconds` to let General retain the most recent successfully
routed destination per chat and user. The model decides whether a new utterance
explicitly switches destination or continues that recent conversation. An
explicit destination takes priority. HerdRelay verifies that a
`recent_context` decision selects the exact stored live candidate; `/forget`
clears it. The default value `0` keeps the router stateless.

With the Orca Bot provider, General text is sent to one configured Orca Bot.
This mode requires `runtime.provider: orca` and fails at startup for other
runtimes.
HerdRelay creates and supervises a dedicated non-Agent Orca terminal because the
public orchestration CLI requires a live sender-terminal identity. The terminal
runs a local file-queue worker, is not published as an Agent conversation, and
does not take UI focus. Requests, pending reply correlation, heartbeat, and
transport-specific Bot chat IDs are stored under a private mode-`0700` queue
directory.
A mutation timeout is reported as `outcome_unknown` and is never resent
automatically. There is no automatic fallback to the model router.

```yaml
runtime:
  provider: orca
relay:
  general:
    enabled: true
    provider: auto
    orca_bot:
      bot_id: 00000000-0000-0000-0000-000000000000
      queue_path: ~/.cache/herdrelay/orca-bot-gateway
      gateway_worktree: folder:00000000-0000-0000-0000-000000000000
      gateway_title: HerdRelay Gateway
      gateway_command: >-
        cd /path/to/herdrelay &&
        exec /path/to/uv run python -m herdrelay.orca_bot_gateway
        --config /path/to/herdrelay/config.yaml
```

The Bot receives the original General text verbatim. HerdRelay correlates replies
by the public CLI's outbound message ID and sends only matching replies back to
the originating transport. Orca names the Bot conversations `Home`,
`Telegram Chat`, and `Discord Chat`; their contexts cannot mix accidentally.
After a new Side chat is created, open the Bot's `Home Chat`, select `Chats` in
the right sidebar, and open that Side chat once to reveal it in the Bots list.
Configure side-effect confirmations and allowed behavior in the Bot's
`SOUL.md`; HerdRelay does not grant tools or silently reinterpret Bot failures.
`/forget` atomically moves future messages to a newly created Bot chat while
allowing already-pending replies to finish.

### Optional Telegram voice input

Authorized Telegram voice messages can be transcribed locally. Voice input
requires `enable_send: true` and an enabled General provider. General passes the
transcript through the selected `router` or `orca-bot` provider; with
`scope: general-and-bound`, a bound topic submits the full transcript directly
through its existing Agent route
without an LLM call. The default `scope: general` keeps bound topics text-only.
Audio is downloaded to a private temporary directory, bounded by duration and
file size, and deleted after each attempt. Telegram photo, video, document, and
other media messages are not forwarded to Agents.

```yaml
transports:
  telegram:
    voice_input:
      enabled: true
      scope: general-and-bound
      max_duration_seconds: 120
      max_file_bytes: 10000000
      transcription:
        provider: mlx-whisper
        model: mlx-community/whisper-large-v3-turbo
        language: smart
        python_path: /path/to/python-with-mlx-whisper
        timeout_seconds: 120
```

The `mlx-whisper` provider runs in a separate Python subprocess with a minimal
environment. Install its optional dependencies with `uv sync --extra voice-mlx`
and make `ffmpeg` available, or point `python_path` at an existing trusted MLX
Whisper environment. A provider-neutral `command` option is also available;
its argv may contain `{input}` and must emit one JSON object containing at least
a non-empty `text` field. Voice input and its model download are disabled by
default. Transcription and the selected General submission path produce
separate audit entries, and any download, transcription, routing, or gateway
error fails closed without automatic retry.

## Optional Discord transport

Set `transports.discord.enabled: true`, provide `DISCORD_TOKEN` in `.env`, and
configure the Discord section:

```yaml
transports:
  discord:
    enabled: true
    allowed_guild_ids: [123456789012345678]
    allowed_channel_ids: [234567890123456789]
    allowed_user_ids: [345678901234567890]
    thread_parent_channel_id: 234567890123456789
    enable_send: true
    enable_questions: false
    enable_watcher: true
    enable_interrupt: true
    enable_auto_threads: true
    auto_threads:
      refresh_seconds: 30
    watcher:
      statuses: [blocked, done]
```

Create the Bot in the
[Discord Developer Portal](https://discord.com/developers/applications), enable
**Message Content Intent**, and grant only the permissions HerdRelay needs:

- View Channels
- Send Messages
- Read Message History
- Create Public Threads
- Send Messages in Threads
- Manage Threads
- Add Reactions
- Change Nickname, optional

Put the parent text-channel ID in both `thread_parent_channel_id` and
`allowed_channel_ids`. Automatic threads are public threads within that parent
channel. HerdRelay adds configured allowed users as thread members and names
managed topics and threads `[hc] alias/agent · address`,
`[herdr] alias/agent · address`, or `[orca] alias/agent · address`, so
historical routes from another backend are not mistaken for the current route.
Discord permissions remain the visibility boundary.

A normal message in the parent channel uses General. A normal message in a bound
child thread submits directly to that Agent. Missing targets wait for
`target_absence_grace_seconds`, then their threads are archived. Archived managed
threads are deleted after `closed_thread_retention_minutes`; set it to `null` to
retain them. Mentioning the Bot exposes `list`, `help`, `forget`, and the guarded
`interrupt` command when the selected backend
supports it and configuration enables it. The legacy `stop` spelling remains a
Core interrupt alias only; it never closes a target. Chat does not expose raw
keys, agent start, terminal close/kill/remove, or generic approval/denial. All
HerdRelay-generated chat UI messages are English; user prompts and Agent replies
are forwarded without translation.

## Legacy Herdr TUI compatibility

Select the legacy runtime with:

```yaml
runtime:
  provider: herdr-tui
  herdr_tui:
    cli_path: herdr
    default_source: recent-unwrapped
    command_timeout_seconds: 20
    target_exposure:
      mode: selected
      addresses: [w1:p1]
```

Use `selected` during backend tests to prevent unrelated panes from creating
topics and threads. Legacy Discord behavior remains available. TUI-to-Telegram routing has contract
tests but no live acceptance guarantee and is outside the supported production
matrix.

The former top-level `herdr` section and `runtime.core` name remain accepted for
configuration compatibility. New installations should use the grouped schema in
`config.example.yaml`.

## Configuration model

Settings are grouped by responsibility:

- `runtime`: provider selection, backend eligibility, host scope, and explicit
  target exposure policy;
- `storage`: stable SQLite path;
- `relay`: shared output, streaming, and text safety policy;
- `transports.discord`: Discord access and controls; and
- `transports.telegram`: Telegram access and controls.

Important defaults and invariants:

- Discord defaults to enabled for compatibility; disable it explicitly for a
  Telegram-only process.
- Telegram `enable_send` and `enable_interrupt` default to false.
- New Core and Orca configurations default to `all-eligible`; `selected` and
  `none` are explicit restrictions.
- Telegram requires exact nonempty chat and user allowlists.
- `storage.database_path` defaults to `herdrelay.sqlite3`. An existing legacy
  `herdr-discord-bridge.sqlite3` is reused only when the preferred file is
  absent.
- Hook inbox directories and files are created as `0700` and `0600`.

Environment variables:

| Variable | Meaning |
|---|---|
| `DISCORD_TOKEN` | Required only when Discord is enabled |
| `TELEGRAM_BOT_TOKEN` | Required only when Telegram is enabled |
| `HERDR_SOCKET_PATH` | Optional legacy TUI socket override |
| `HERDR_SESSION` | Optional legacy named session |
| `HERDRELAY_HOOK_INBOX` | Adapter-side inbox override |
| `HERDRELAY_CORE_ID` | Explicit adapter-side stable Core identity |

See [`config.example.yaml`](config.example.yaml) for the complete schema and
[`docs/operations.md`](docs/operations.md) for event formats and operational
details.

## Security model

Treat every enabled chat destination as remote terminal access.

- Backends expose only eligible targets; mutations revalidate exact runtime and
  target identity immediately before input.
- Telegram mutations require exact actor and chat authorization, atomic audit
  claims, per-actor/chat/target rate limits, and destination-bound topic routes.
- Telegram interrupt confirmations are actor/chat/topic/target bound,
  single-use, and expiring.
- Mutation feedback failure never replays the mutation.
- Discord output disables mentions; Telegram uses exact persisted topics.
- `dangerous_text_blocklist` is a substring filter, not a sandbox or policy
  engine.
- Hook files contain assistant responses and question text. Protect the host
  account and backups.
- Telegram Bots cannot use Secret Chats; Telegram stores transmitted messages
  and metadata in its cloud service.
- Discord thread delivery is not transactional across multiple chunks. A retry
  after partial failure can repeat an earlier chunk.
- Connected-machine Core targets remain disabled unless explicitly supported by
  policy and configuration.

HerdRelay never exposes raw runtime protocols, arbitrary backend methods, shell
commands, force-send behavior, raw terminal keys, generic approval/denial, or
Agent/terminal start, close, kill, or remove operations over Telegram.

## Operations

Run all configured transports:

```bash
uv run herdrelay --config config.yaml
```

Run Telegram explicitly without creating a Discord client:

```bash
uv run herdrelay-telegram --config config.yaml
```

Manage an optional file-backed Core selection (`selected` mode only):

```bash
uv run herdrelay-targets --config config.yaml list
uv run herdrelay-targets --config config.yaml allow t12
uv run herdrelay-targets --config config.yaml remove t12
```

After changing code, `.env`, `config.yaml`, or target exposure settings:

```bash
launchctl kickstart -k "gui/$UID/dev.herdrelay"
scripts/status_launchd.sh
```

Logs:

```text
logs/launchd.out.log
logs/launchd.err.log
```

Adapter installers contain absolute checkout paths for Claude and Codex.
Reinstall them after moving the repository. The Pi installer copies the managed
extension and existing Pi sessions require `/reload`.

## Development and tests

```bash
uv sync --extra dev
uv run pytest -q
node --experimental-strip-types --test tests/pi_output_adapter.test.mjs
uv run python -m compileall -q src tests scripts
git diff --check
```

Run the isolated live Core contract only when explicitly requested:

```bash
HERDRELAY_RUN_HC_LIVE_TEST=1 uv run pytest -q tests/test_hc_live.py
```

The live fixture uses isolated HOME/XDG paths, `HC_SESSION`, and a private Core
socket. It never types into an existing terminal.

## Repository layout

- `src/herdrelay/` — runtime clients, transport adapters, delivery coordination,
  security policy, and SQLite state;
- `integrations/` — Pi extension source;
- `scripts/` — adapter installers, compatibility checks, service helpers, and
  log rotation;
- `tests/` — unit, regression, adapter, and isolated live-contract tests;
- `docs/operations.md` — event formats and operational details;
- `docs/core-telegram-design.md` — Core/Telegram architecture and security boundary;
- `docs/core-telegram-plan.md` — Core/Telegram implementation history and remaining
  acceptance work;
- `docs/runtime-transport-design.md` — generalized backend/transport architecture,
  including Orca; and
- `docs/runtime-transport-plan.md` — staged refactor and Orca implementation plan;
- `docs/feature-inventory.md` — common features, legacy TUI-only controls, and cleanup candidates.

## Current limitations

- Physical Apple Watch notification and short-reply behavior still require the
  device checklist in
  [`docs/apple-watch-acceptance.md`](docs/apple-watch-acceptance.md). Telegram
  text and voice behavior verified on iPhone does not by itself establish Watch
  notification acceptance.
- Telegram interrupt is Core-only.
- Orca prompt submission has automated coverage, direct public-CLI acceptance,
  and live Telegram and Discord send/output acceptance. Telegram General and
  the Discord parent channel also have live Orca Bot acceptance using separate
  Bot chats. Orca-managed Claude question identity and Telegram question
  display have live acceptance. Question text and options are display-only
  because no safe public structured-answer operation has been accepted. Claude
  completed responses use its Stop hook; other Orca terminal Agents use polling,
  which intentionally does not backfill responses that predate its first
  snapshot. Orca interrupt is disabled because `terminal send --interrupt`
  accepted input without stopping the tested Pi turn.
- Legacy TUI with Telegram is best-effort and not live-acceptance tested.
- Native Orca chat agents remain excluded until a documented safe prompt path
  is available; local Orca terminal agents are exposed automatically.
- Existing Pi sessions need `/reload` after the Pi adapter changes. Claude and
  Codex must have their hooks installed; restart sessions after first installing
  or changing hook registration.
