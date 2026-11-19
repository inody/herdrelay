# HerdRelay

HerdRelay connects local coding agents to authorized chat transports. It routes
prompts, lifecycle notifications, and completed responses between Herdr Core or
the legacy Herdr TUI and Discord or Telegram. An experimental Orca backend can
relay eligible agent status and newly completed responses and submit normal
prompts through Orca's public CLI.

Herdr remains the runtime authority. HerdRelay does not start Core, expose its
raw protocol, provide arbitrary shell access, or replace Herdr's own session
management.

## Support matrix

| Runtime | Discord | Telegram |
|---|---:|---:|
| Herdr Core | Supported and live-E2E tested | Supported and live-E2E tested |
| Legacy Herdr TUI | Compatibility path | Best-effort; automated tests only |
| Orca | Experimental send/output; automated tests | Experimental; live-E2E send/output tested |

Herdr Core with Telegram and/or Discord is the supported production path.
Guarded interrupt is Core-only. Raw terminal keys, generic approval/denial,
remote agent start, and terminal close are not exposed through chat. Legacy TUI
structured-question answers remain available through Discord when explicitly
enabled.

Verified development environment:

| Component | Version or requirement |
|---|---|
| Python | 3.12+ |
| Herdr Core | 0.1.21 |
| Herdr app | 0.1.16; optional presentation layer |
| Legacy Herdr TUI | 0.9.5 compatibility |
| Pi | 1.5.x adapter coverage |
| Discord | `discord.py` 2.x |
| Package manager | `uv` |
| Pi adapter tests | Node.js 22.6+ |

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

Herdr lifecycle events ─> status routing ─> enabled chat transports
HerdRelay state/audit  <──────────────────> SQLite
```

Runtime identity and transport destination are independent. Core routes pin the
stable Core ID together with the terminal address. Delivery dedupe is
per-destination, so successful Discord delivery never suppresses Telegram, or
vice versa.

## Quick start: Herdr Core with Telegram

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
3. Add the Bot. Grant only **Manage Topics**; unrelated administration
   permissions should remain disabled.
4. Keep Bot Privacy Mode enabled. An administrator Bot still receives group
   messages.
5. Obtain the exact forum chat ID and your Telegram user ID. Both allowlists must
   be nonempty.

Never commit or paste Bot tokens into configuration, screenshots, logs, or
issues.

### 3. Configure Core and Telegram

A minimal supported configuration is:

```yaml
runtime:
  provider: herdr-core
  herdr_core:
    socket_path: ~/.local/state/herdr-core/default/default.sock
    target_exposure:
      mode: all-eligible
    require_app_visible: true
    include_hidden_workers: false
    include_connected_machines: false

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
    enable_send: false
    enable_interrupt: false
    target_absence_grace_seconds: 60
    closed_topic_retention_days: 1
```

`all-eligible` exposes every eligible local interactive agent while retaining
visibility, hidden-worker, connected-machine, capability, and exact-identity
checks. HerdRelay connects directly to the configured existing Unix socket and
never uses a CLI command that could start Core.

Installations that intentionally need a smaller set can use explicit
`selected` mode with exact addresses and an optional private file. The
`herdrelay-targets` CLI manages that file only in `selected` mode.

### 4. Install agent output adapters

```bash
uv run python scripts/manage_claude_hook.py install
uv run python scripts/manage_pi_adapter.py install
```

For Codex, first enable hooks:

```toml
[features]
hooks = true
```

Then install its Stop hook:

```bash
uv run python scripts/manage_codex_hook.py install
```

Run `/reload` in existing Pi sessions. Restart existing Claude or Codex
sessions after installing their hooks.

Adapters write private versioned events to the local inbox. Core events require
`HC_TERMINAL_ID` and a verifiable stable Core ID; HerdRelay does not guess Core
identity from a working directory. Orca-managed Claude hooks use the exact
`ORCA_PANE_KEY` and Orca's public `agent list` CLI to produce version 3
structured-question lifecycle events. Completed Orca responses come only from
the in-process public-CLI output source to avoid duplicate delivery.

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
- each eligible runtime target (terminal/pane), not each workspace or agent
  name, receives one forum topic;
- ordinary topic text submits a prompt only when `enable_send: true` and a
  successful submission adds a 👀 reaction to that message;
- `//reload` sends the agent command `/reload` without treating it as a Bot
  command; and
- `/interrupt` requires `enable_interrupt: true` plus a one-time confirmation
  button.

A target returning at the same stable address reopens its existing topic, and an
Orca runtime-ID rotation does not create a new topic. Transient discovery gaps
shorter than `target_absence_grace_seconds` do not close a topic. A genuinely
new pane gets a separate topic even when it has the same workspace and agent
name. Closed topics are deleted after `closed_topic_retention_days`; set it to
`null` to keep them indefinitely.

Prompt and interrupt attempts are claimed and audited before the Core call.
Timeouts are recorded as `outcome_unknown` and are never retried automatically.

### Optional General-topic LLM router

`transports.telegram.general_router` can treat authorized ordinary text in
General as a one-shot routing request. Bound agent topics still submit directly.
The router model receives only opaque candidate IDs and display metadata, must
return structured JSON, and cannot call runtime tools. HerdRelay verifies the
selected live `TargetKey`, requires the forwarded prompt to be an exact substring
of the original utterance, then uses the normal audited prompt path.

The router is disabled by default and supports explicit providers:

```yaml
transports:
  telegram:
    enable_send: true
    general_router:
      enabled: true
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

### Optional Telegram voice input

Authorized Telegram voice messages in General can be transcribed locally and
then passed through the same General router. Bound topics remain text-only by
default. Audio is downloaded to a private temporary directory, bounded by
duration and file size, and deleted after each attempt.

```yaml
transports:
  telegram:
    voice_input:
      enabled: true
      scope: general
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
default. Transcription, routing, and prompt submission produce separate audit
entries, and any download, transcription, or routing error fails closed without
retry.

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
channel; Discord permissions remain the visibility boundary.

A normal message in a bound thread submits a prompt. Mentioning the Bot exposes
`list`, `help`, and the guarded `interrupt` command when the selected backend
supports it and configuration enables it. The legacy `stop` spelling remains a
Core interrupt alias only; it never closes a target. Chat does not expose raw
keys, agent start, terminal close/kill/remove, or generic approval/denial.

## Legacy Herdr TUI compatibility

Select the legacy runtime with:

```yaml
runtime:
  provider: herdr-tui
  herdr_tui:
    cli_path: herdr
    default_source: recent-unwrapped
    command_timeout_seconds: 20
```

Legacy Discord behavior remains available. TUI-to-Telegram routing has contract
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

HerdRelay never exposes the raw Core socket, arbitrary Core methods, shell
commands, force-send behavior, raw Core keys, approval/denial, or terminal
start/close/kill/remove over Telegram.

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
- `docs/runtime-transport-plan.md` — staged refactor and Orca implementation plan; and
- `docs/feature-inventory.md` — common features, legacy TUI-only controls, and cleanup candidates.

## Current limitations

- Physical Apple Watch notification and short-reply behavior still require
  device acceptance testing; use
  [`docs/apple-watch-acceptance.md`](docs/apple-watch-acceptance.md).
- Telegram interrupt is Core-only.
- Orca prompt submission has automated coverage, direct public-CLI acceptance,
  and live Telegram send/output acceptance; live Discord acceptance remains
  pending. Orca-managed Claude question identity and Telegram question display
  have live acceptance. Question text and options are display-only because no
  safe public structured-answer operation has been accepted. Orca interrupt is
  disabled because `terminal send --interrupt` accepted input without stopping
  the tested Pi turn. Discord watcher events and existing-response backfill
  remain disabled for Orca.
- Legacy TUI with Telegram is best-effort and not live-acceptance tested.
- Native Orca chat agents remain excluded until a documented safe prompt path
  is available; local Orca terminal agents are exposed automatically.
- Existing agent sessions must load the current adapter version before they can
  emit Core-aware output events.
