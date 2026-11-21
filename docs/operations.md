# HerdRelay operations reference

This document contains operational detail that is useful when running or
debugging HerdRelay but is not required for the first installation. Start with
the [README](../README.md).

## Runtime files and state

The standard checkout uses these local, gitignored files:

- `.env` — enabled transport tokens and optional runtime overrides;
- `config.yaml` — runtime configuration;
- `herdrelay.sqlite3` — SQLite state when copied from the example;
- `~/.cache/herdrelay/agent-output/` — default adapter inbox; and
- `logs/` — launchd stdout, stderr, and rotated backups.

If `storage.database_path` is omitted, HerdRelay prefers `herdrelay.sqlite3`.
It reuses an existing `herdr-discord-bridge.sqlite3` only when the preferred
file is absent. Keep the chosen database when upgrading because it contains:

- `bindings`: Discord location to Herdr pane mappings;
- `agent_threads`: pane to automatic-thread mappings;
- `audit_log`: input and control action records;
- `event_dedupe`: delivered hook and watcher event keys; and
- `pending_questions`: unanswered structured Claude questions;
- `telegram_topics`: normalized target-to-topic routes; and
- `transport_audit`: claimed Discord/Telegram mutations and outcomes.

HerdRelay does not currently expose a database migration or pruning command.
Back up the database with the bot stopped, or use SQLite's online-backup tools.

## Herdr session selection

CLI operations are ordinary `herdr` subprocesses and inherit HerdRelay's
environment. The status watcher independently resolves its Unix socket in this
order:

1. `HERDR_SOCKET_PATH` loaded into the app configuration;
2. process `HERDR_SOCKET_PATH`;
3. `~/.config/herdr/sessions/$HERDR_SESSION/herdr.sock`; or
4. `~/.config/herdr/herdr.sock`.

Use `herdr status` in the same environment as the bot. HerdRelay does not add
`--machine`, `--remote`, or `--session` flags to individual CLI calls and does
not follow the machine selected in another Herdr client.

## Orca backend exposure

The Orca backend uses only the documented `orca` CLI. New
configurations use `target_exposure.mode: all-eligible`: every listed terminal
with a recognized agent identity on configured `host_ids` is available, and
Telegram topics follow agent open/close lifecycle automatically. Temporary
connection or writability changes do not close the topic, and an active
`working`, `blocked`, or `waiting` agent bridges a temporary topology gap.
Discovery joins terminal topology with agent session metadata, so a newly
started agent receives a `starting` topic before its first Orca session ID is
reported. `selected` mode can optionally restrict exposure by exact pane keys
and/or worktree IDs; `none` disables target exposure without disabling the
transport.

A live route includes the current Orca runtime ID and the host-qualified pane
key. Topic continuity follows the stable pane address across runtime-ID
rotation. Before normal prompt submission, HerdRelay refreshes the agent list
and maps the agent's exact `paneKey` to a single connected, writable terminal
whose `tabId:leafId`, host, worktree, and agent identity agree. Topic discovery
does not require those mutation-time capabilities. If one pre-mutation snapshot
briefly omits a terminal, HerdRelay refreshes the exact discovery snapshot once;
this happens before any input is sent. It then calls `orca terminal send --enter
--wait-submit`; it never selects a terminal by title, repository name, cwd, or
display label, and it never retries after `terminal send` begins.

Orca reports input acceptance and, where the agent provider supports it,
submission observation. A transport timeout after input may have been accepted
is recorded as `outcome_unknown` and is not retried automatically. Direct
public-CLI live acceptance confirmed that an exposed Pi agent received the
prompt and entered `working`.

Telegram live E2E acceptance also succeeded: one authorized topic message was
claimed once, recorded as a `succeeded` `send_text` audit against the exact Orca
target key, observed as the agent's completed response, and delivered once to
the same topic. Discord live acceptance remains pending.

Orca interrupt remains disabled. In live characterization,
`orca terminal send --interrupt` returned `accepted: true` and wrote one byte,
but the Pi agent remained `working`; input acceptance is therefore not treated
as proof of a native interrupt. Chat does not expose raw terminal input,
generic approval/denial, agent launch, or terminal close.

## Telegram topic lifecycle

Telegram creates one topic per eligible runtime target. Two simultaneous panes
with the same workspace and agent name are distinct targets and intentionally
receive separate topics. A target that returns at the same stable address
reopens its previous topic; an Orca runtime-ID rotation alone does not close or
replace that topic.

A polling backend does not close a topic until the target has remained absent
for `target_absence_grace_seconds`; this prevents transient incomplete Orca
snapshots from producing close/reopen loops. When sustained absence is
confirmed, the topic is closed. If `closed_topic_retention_minutes` is an
integer, a closed topic that remains absent is deleted after that many minutes
and its SQLite route is removed. `null` retains closed topics indefinitely. The
legacy `closed_topic_retention_days` setting remains accepted for existing
installations, but the two settings cannot be combined. The Bot needs **Manage
Topics**. Deletion API failures are isolated per topic and retried later instead of
stopping status reconciliation. Topic creation is serialized per stable target;
API or persistence failures enter a five-minute cooldown. If Telegram creates a
topic but SQLite persistence fails, HerdRelay deletes the new topic immediately
and reuses an existing stable route when one appeared concurrently. Do not
delete a still-tracked topic manually: a missing Telegram topic with a retained
SQLite route cannot be reopened.

## Telegram General providers

When `transports.telegram.general_router.enabled` is true, `provider` selects
`router` or `orca-bot`. General itself is the trigger; no wake word is required.
Slash commands are handled locally before General processing, and bound Agent
topics bypass either provider entirely.

With `provider: router`, authorized ordinary text in General is sent to the
configured intent model.

The model receives opaque candidate IDs plus topic ID, workspace/title, agent,
status, and display alias. It does not receive the runtime address or cwd. Its
JSON decision must select a supplied candidate and extract the forwarded prompt
as an exact substring of the original message. HerdRelay then refreshes the
runtime, verifies the exact target identity, applies the normal blocklist and
rate limits, audits both routing and mutation, and submits through the existing
backend path. Any model error, timeout, ambiguity, unknown candidate, or changed
target fails closed and is not retried automatically.

Supported providers are `openai-compatible`, `pi-cli`, and `command`. `pi-cli`
reuses Pi authentication but launches each classification with no session or
tools and disables automatic extension discovery, MCP, skills, prompt
templates, themes, and context files. Required trusted provider extensions can
be named explicitly in `model.extensions`. For LaunchAgent environments, set
`model.node_path` when the Pi launcher cannot find Node.js. Pi subprocesses use
a minimal environment; add only required credential variable names through
`model.pass_env`. The command provider runs a configured argv directly without
a shell. Keep the
router disabled until a provider and model have been selected explicitly. For
Telegram installations that report General with a numeric thread ID, set
`general_topic_id`; otherwise General is recognized by the absence of
`message_thread_id`.

When `context_ttl_seconds` is positive, a successful General route stores the
selected topic ID with a timestamp under a chat-and-user-specific SQLite state
key. On the next request, the model receives that current candidate as
`recent_context` together with its age. The model returns
`routing_basis=explicit_destination` when the utterance names a target, or
`routing_basis=recent_context` for a conversational continuation. Explicit
switches take priority. HerdRelay rejects a context-based decision unless it
selects the exact stored candidate, which must still be live. Context expires at
the configured TTL, disappears when its topic is unavailable, survives a
service restart while fresh, and can be cleared with `/forget`. Ambiguous or
failed requests do not replace it.

With `provider: orca-bot`, authorized General text is sent verbatim to the exact
configured Orca Bot ID. This provider requires the Orca runtime and does not
fall back to the model router. HerdRelay supervises a dedicated non-Agent Orca
terminal in the Bot's worktree because `orca orchestration send` requires a live
sender-terminal identity. The terminal runs the configured gateway worker,
does not receive a Telegram topic, and is identified by `gateway_title` plus a
private heartbeat. A stale worker is replaced only after a second stale window;
a worker lock prevents two consumers from processing the queue.

The gateway stores mode-0600 atomic JSON requests, processing state, outbound
message correlations, events, heartbeat, and the Telegram-only Bot chat ID under
a mode-0700 queue directory. It uses only `orca orchestration send` and
`orca orchestration check`. Replies are accepted only when their `thread_id`
matches an outstanding outbound message ID, then are consumed from the mailbox
and delivered to the original General destination. A send timeout, restart with
an in-flight processing record, or public-CLI outcome-unknown error produces an
`outcome_unknown` event and is never resent automatically. `/forget` removes the
saved Bot chat ID so future requests create a new chat; pending replies from the
old chat remain eligible for delivery.

Set side-effect and confirmation rules in the Bot's `SOUL.md`. The current
production policy permits read-only inspection and explicitly requested Agent
launches, but requires confirmation before Mail, Todoist, external-app,
destructive command, Agent close, or terminal deletion mutations.

## Telegram voice input

`transports.telegram.voice_input.enabled` accepts authorized Telegram voice
messages, downloads the Bot API file into a mode-0700 cache directory,
transcribes it, and deletes the temporary audio. General passes the transcript
through the selected `router` or `orca-bot` provider. With
`scope: general-and-bound`, a voice message
inside a bound topic bypasses destination classification exactly like bound text
and submits the full transcript through that topic's existing route. The normal
live identity check, blocklist, mutation rate limits, dedupe, and audit still
apply. Bound-topic voice does not change the recent General destination.

The default limits are 120 seconds and 10 MB. Telegram's advertised size is
checked before download and the actual streamed byte count is checked again.
A failed download, timeout, empty transcript, invalid provider output, ambiguous
route, or changed runtime target causes no mutation and is not retried
automatically. Router processing records `transcribe_voice`, `route_text`, and
`send_text` as distinct audit actions; Orca Bot processing records
`transcribe_voice`, `bot_send`, and `bot_reply`.

The `mlx-whisper` provider invokes a separate Python interpreter and emits a
small JSON transcript contract. Set `python_path` to an environment containing
`mlx-whisper`; install the optional HerdRelay environment with
`uv sync --extra voice-mlx`, or reuse a separately managed trusted environment.
`ffmpeg` must be on the provider subprocess PATH. The provider inherits only
HOME, locale, cache-related variables, and names explicitly listed in
`pass_env`; Telegram and Discord credentials are not copied by default.
`language: smart` performs automatic detection and retries clips of at most two
minutes in Japanese when Whisper detects neither Japanese nor English.

The provider-neutral `command` implementation executes an argv array without a
shell. Use `{input}` where the temporary audio path should appear; if omitted,
the path is appended. The command must write exactly one JSON object to stdout:

```json
{"text":"HJB FDMのパイに続けて","language":"ja","duration_seconds":3.2}
```

Do not enable voice input until the configured Python/command, `ffmpeg`, and
model cache have been tested locally. Model downloads may be large and should be
performed during explicit setup rather than an unattended production request.

## Thread and binding lifecycle

`enable_auto_threads` starts a reconciliation loop using `herdr pane list`.

- A pane receives a thread only when its pane record includes a detected agent.
- The thread is a public thread under `thread_parent_channel_id`.
- The name is `alias/agent`, where the alias prefers the Herdr workspace label,
  then the cwd basename, then the pane ID.
- A blocked pane is prefixed with `🔴 `. Other lifecycle changes do not change
  the status prefix, which avoids excessive Discord rename requests.
- Thread names are truncated to Discord's 100-character limit.
- If agent detection disappears but the pane remains, HerdRelay preserves the
  existing name, binding, and archive state.
- If an agent returns to the same pane, HerdRelay reuses and, if needed,
  unarchives the existing thread.
- If the pane disappears from a successful pane snapshot, its thread is
  archived.
- A failed or malformed pane snapshot aborts the synchronization before any
  archival decision.
- If Discord can no longer fetch a recorded thread, HerdRelay leaves the record
  in place rather than silently creating a replacement.

Pane movement across workspaces changes the public Herdr pane ID. HerdRelay does
not migrate mappings across that change, so the old thread is archived after
the old pane ID disappears and the new pane may receive a new thread.

Automatic thread creation is the current public binding mechanism. There is no
manual bind command in the Discord interface.

## Input path

For an ordinary bound-thread message, HerdRelay:

1. ignores bot authors, empty content, non-thread channels, and unbound threads;
2. checks for a pending structured question and accepts a valid numeric answer
   only when `enable_questions` and backend question answering are available;
3. otherwise verifies guild/channel, user, `enable_send`, message length, and
   blocklist;
4. submits one normal backend prompt; and
5. writes an audit entry and adds ✅, or writes an error entry and adds ⚠️.

The reaction reports submission success only. It does not wait for an agent
turn to finish.

With `allow_pane_send_fallback: true`, an `agent prompt` failure falls back to
`herdr pane run`. This can type into a shell or other foreground program, so the
default is false.

Arbitrary `!key` aliases and raw terminal escape-sequence injection are not
supported.

## Mention command behavior

All mention commands check the Discord location before reading backend state or
resolving a bound target.

- `interrupt` checks `enable_interrupt`, the write-user allowlist, and a guarded
  native backend capability. The legacy `stop` spelling is an interrupt alias;
  it never closes a pane or terminal.
- `list` and `help` do not require a write-user entry.
- Unknown commands, including the retired `start` command, are rejected and
  audited without a backend mutation.

For `interrupt`, HerdRelay writes an `attempt` audit row before the backend
mutation and an `ok` row after it succeeds. Failure to write the initial audit
row prevents the mutation. A later Discord reply failure does not replay or
roll back a successful interrupt.

## Status watcher

`enable_watcher` starts a direct subscription to Herdr's local event socket.
For every existing pane, it subscribes to each configured
`pane.agent_status_changed` status and also listens for pane creation, closure,
and movement.

Operational behavior:

- Existing shell panes are subscribed even before an agent is detected, so a
  later launch can produce status events.
- With no panes, the watcher waits for `reconnect_delay_seconds` and retries
  without opening an empty subscription.
- Topology events force an immediate subscription rebuild.
- A post-acknowledgement pane snapshot catches topology changes that occurred
  during subscription setup.
- `resubscribe_interval_seconds: 0` disables only periodic reconnects;
  topology-triggered refreshes still occur.
- A blocked notification is delayed by
  `blocked_notification_delay_seconds` so a structured Claude question can be
  delivered before the generic blocked status notification.

When `include_output` is true, the watcher reads the configured tail and uses a
hash of that output for deduplication. When false, it uses Herdr's
`state_change_seq` when available, otherwise the event payload. A notification
requires an existing pane/thread binding.

Blocked notifications intentionally mention all `allowed_user_ids`. Done and
working notifications do not.

## Optional Core target selection

Core defaults to `target_exposure.mode: all-eligible` after visible/root/local
eligibility checks. Installations that intentionally need a smaller set can use
`selected` mode with
`runtime.herdr_core.target_exposure.addresses_file` and manage it locally:

```bash
uv run herdrelay-targets --config config.yaml list
uv run herdrelay-targets --config config.yaml allow t12
uv run herdrelay-targets --config config.yaml remove t12
```

The command connects directly to the configured existing Core socket; it does
not invoke a Core-starting CLI operation. `allow` verifies that the exact
address is currently an eligible live agent. Writes are atomic and mode `0600`.
Restart HerdRelay after changes. At least one selector must remain in
`selected` mode; switch the configuration to `all-eligible` or `none` instead
of relying on an ambiguous empty file.

## Hook-mode output relay

Hook mode watches `streaming.hook_inbox_path` for JSON files, oldest first. The
built-in adapters atomically rename a temporary file into the inbox so HerdRelay
does not read a partial event.

A legacy TUI response event has this format. TUI-to-Telegram routing has
automated contract coverage but is not part of live acceptance or the supported
production matrix:

```json
{
  "version": 1,
  "event_id": "stable-id-for-this-turn",
  "agent": "claude",
  "pane_id": "w1:p1",
  "text": "complete assistant response"
}
```

A Core-aware response uses version 2 and pins the Core identity as well as the
terminal address:

```json
{
  "version": 2,
  "event_id": "stable-id-for-this-turn",
  "runtime": "herdr-core",
  "core_id": "stable-core-id",
  "target": "t12",
  "agent": "pi",
  "text": "complete assistant response"
}
```

An Orca response uses version 3. It separates the stable Orca runtime and pane
identity from the conversation generation:

```json
{
  "version": 3,
  "event_id": "stable-response-id",
  "backend": "orca",
  "instance_id": "orca-runtime-id",
  "target": "local/tab:pane",
  "generation": "orca-session-id",
  "agent": "pi",
  "text": "complete assistant response"
}
```

The in-process Orca output source observes exposed non-Claude agents through
the public Orca CLI. Its first snapshot establishes a cursor without backfilling
existing completed responses. A later changed `done` response is written
atomically to the same private inbox and uses the normal per-destination delivery
tracking. Orca-managed Claude responses instead come from Claude's Stop hook;
Orca can keep its aggregate state at `working` for background tasks after Claude
has already produced the user-facing response.

The Pi adapter and Claude/Codex hook prefer `HC_TERMINAL_ID`. They read the
stable Core ID from `HERDRELAY_CORE_ID` when explicitly supplied, otherwise
from `core.json` beside `HC_TERMINAL_SOCKET`. If a Core terminal lacks a
verifiable Core identity, the adapter emits nothing instead of falling back to
an inherited legacy pane ID. A shared Codex app-server that does not inherit
Core terminal identity remains unable to emit a version 2 event; HerdRelay does
not guess a Core terminal from working-directory matches.

For an Orca-managed Claude terminal, the question hooks read the inherited
exact `ORCA_PANE_KEY` and resolve it to exactly one terminal agent with `orca
agent list --json`. The public CLI supplies the runtime ID, host ID, and current
Orca session generation for version 3 question lifecycle events. Missing,
ambiguous, or failed CLI resolution emits nothing and never falls back to a
legacy pane identity. Claude Stop events are the completion authority for
Orca-managed Claude agents; the polling output source skips those agents to
prevent duplicate delivery. The hook does not use Orca's private database or
daemon socket.

Supported `kind` values are:

- `response` (default): relay `text` and clear any pending question;
- `question`: relay text, persist structured question data, and attach controls;
- `question_resolved`: clear the pane's pending question without posting; and
- `human_input`: add allowed users to an existing Pi thread without posting.

Validation and retention rules:

- `version` must be `1`, `2`, or `3`.
- Version 1 requires only `pane_id` and rejects conflicting Core identity.
- Version 2 requires `runtime: herdr-core`, `core_id`, and `target`, and rejects
  a conflicting legacy `pane_id`.
- Version 3 currently requires `backend: orca`, `instance_id`, `target`, and an
  optional nonempty `generation`; it rejects legacy and Core identity fields.
- Required identifiers must be non-empty strings; event IDs, Core IDs, and
  target addresses are limited to 200 characters and agent names to 50.
- Files larger than `hook_max_event_bytes`, invalid JSON, and malformed events
  are deleted.
- A valid event without a persisted Discord thread or Telegram topic waits until
  a route appears or `hook_max_event_age_seconds` elapses.
- Delivered and duplicate events are deleted.
- The pre-fanout legacy Discord dedupe key
  `agent-output:<agent>:<event_id>` remains readable for compatibility.
- Discord and Telegram response dedupe keys include their destination IDs.
  Delivery to one transport does not suppress another route, and a shared
  version 1, version 2, or version 3 file remains until all currently persisted
  routes have completed.
- Telegram stores normalized backend target keys, so legacy pane IDs,
  Core-ID-pinned terminal addresses, and Orca runtime/pane identities use the
  same routing machinery.
- Core Discord thread bindings store both the stable Core ID and terminal
  address. A changed Core identity is rejected before a thread mutation.
- Text is posted as ordinary Discord message content with original newlines and
  agent-authored Markdown. HerdRelay adds no code fences and disables mentions.
- Long output is split at line boundaries where possible. Delivery is not
  transactional across chunks; a retry after a later chunk fails may repeat
  earlier chunks.

### Claude question lifecycle

The Claude installer registers:

- `PreToolUse` for `AskUserQuestion` to create a question event;
- `PostToolUse` for `AskUserQuestion` to clear an answered question;
- `UserPromptSubmit` to clear a question when a new prompt is submitted; and
- `Stop` to relay the completed assistant response.

Only a tool call containing exactly one question gets a normalized structured
question event. Every transport may render the question text and options, but
answer controls appear only when the backend implements structured question
answering. For the legacy TUI on Discord, a single-choice question with up to
five options uses buttons. Larger or multi-select questions with up to 25
options use a select menu. Pending questions have no timeout and are restored
from SQLite after a bot restart.

The legacy backend implements `answer_question` by typing navigation keys into
the Claude TUI. A multi-select answer toggles options, moves through the extra
rows to Submit, and confirms the review screen. Keys are spaced by 0.5 seconds
because the dialog can drop rapid input. The sequence assumes the cursor and
selected options have not been changed manually. Core and Orca currently render
questions without answer controls because no safe public structured-answer
operation has been accepted.

Pressing Escape in the terminal does not itself emit a resolution hook. A later
new prompt, answered tool call, delivered final response, pane closure, or
Discord answer clears the stored question.

### Pi thread visibility

The Pi adapter emits `human_input` only when:

```text
ctx.mode === "tui" and event.source === "interactive"
```

That includes text typed into the Pi TUI and text that Discord caused the TUI
to receive. It excludes RPC input, extension-generated input, retries,
continuations, tools, and subagents. The event contains no prompt text.

HerdRelay adds each `allowed_user_ids` member to the existing thread. Discord,
not HerdRelay, controls sidebar ordering. Membership is not removed
automatically.

Pi response relay also requires TUI mode. `ctx.hasUI` is deliberately not used
because RPC mode may report a UI and inherit the parent pane environment.

### Codex pane resolution

A standalone agent normally inherits `HERDR_PANE_ID`. Codex's shared app-server
may not, so its hook runs `herdr pane list` with a one-second timeout and selects
a pane only when exactly one record has:

- `agent == "codex"`; and
- a cwd equal to the hook payload's resolved cwd.

No terminal content is read. Zero or multiple matches produce no event.

## Poll mode

`streaming.mode: poll` is an optional legacy mode. It periodically calls
`pane read` for detected agents and computes the difference between rolling
snapshots.

- `tail_lines` controls the ongoing snapshot size.
- `initial_tail_lines` limits restart backfill.
- `enable_visible_fallback` reads the visible screen when recent output did not
  change, allowing some pagers and TUI updates to appear.

Diffing deliberately requires a multi-line overlap before dropping old content.
A repeated single footer line is too weak and can cause duplication rather than
risking lost output.

Poll mode can duplicate redraws, expose screen-only content, and generate more
Herdr reads. Prefer hooks unless terminal mirroring is specifically needed.

## Lower-noise hook-only operation

To relay final responses while minimizing periodic reads and reconnects:

```yaml
streaming:
  mode: hooks
watcher:
  statuses: ["blocked"]
  resubscribe_interval_seconds: 0
  include_output: false
```

This disables done cards, watcher tail reads, and periodic subscription
reconnects. Topology changes still refresh watcher subscriptions. Automatic
threads and hook output continue when their top-level feature flags remain
enabled.

Set `enable_watcher: false` to disable all status subscriptions. Set
`enable_streaming: false` to disable response relay.

## launchd service details

`scripts/install_launchd.sh` installs two user LaunchAgents:

- `dev.herdrelay`: keeps `scripts/run_bot.sh --log-level INFO` alive; and
- `dev.herdrelay.log-rotate`: runs hourly and at load.

The bot LaunchAgent uses this fixed `PATH`:

```text
$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
```

Install `uv` and `herdr` in one of those locations or edit the generated plist.
The installer removes the legacy `dev.herdr.discord-bridge` LaunchAgent if
present.

Log rotation copy-compresses the active file and truncates the same inode so the
running launchd-managed process can continue writing. Defaults are 10 MiB and
three gzip backups. The rotation script also accepts process environment
variables `HERDRELAY_LOG_MAX_BYTES` and `HERDRELAY_LOG_BACKUP_COUNT`, although
the generated rotation plist does not set them.

## Troubleshooting checklist

### The bot does not start

- Run `scripts/run_bot.sh` in a terminal first.
- Confirm `.env` and `config.yaml` exist in the repository root.
- Confirm `DISCORD_TOKEN` is non-empty.
- Run `uv run herdrelay --config config.yaml --log-level DEBUG` for more logs.
- Check that `config.yaml` is a YAML mapping and that ID allowlists are YAML
  lists.

### Threads are not created

- Confirm `enable_auto_threads: true`.
- Confirm `thread_parent_channel_id` is a text channel visible to the bot.
- Check Create Public Threads, Send Messages in Threads, and Manage Threads.
- Run `herdr pane list` and verify the pane record includes an agent name.
- Wait for `auto_threads.refresh_seconds`.
- Check for `Thread sync failed` or `Failed to sync thread` in the logs.

### Messages are rejected

- A ⚠️ reaction means the send path raised an error; inspect the audit database
  and logs.
- Confirm the guild and parent/thread channel are allowed.
- Confirm the author is in `allowed_user_ids` and `enable_send` is true.
- Check `max_message_chars` and every case-insensitive blocklist substring.
- Run `herdr agent list`; without a detected agent, `agent prompt` fails unless
  pane fallback is enabled.

### Hook output is missing

- Confirm `enable_streaming: true` and `streaming.mode: hooks`.
- Confirm the adapter is loaded in the running agent process, not just installed
  on disk.
- Compare `streaming.hook_inbox_path` with `HERDRELAY_HOOK_INBOX`.
- Inspect the inbox for retained JSON files. A retained valid event usually
  indicates a missing mapping or inaccessible Discord thread.
- Check `Agent hook inbox scan failed`, `Discarding invalid`, and
  `Failed to post stream chunk` messages.
- Preserve an event file before manual debugging if it contains sensitive data.

### Status cards are missing or duplicated

- Confirm `enable_watcher: true` and a non-empty `watcher.statuses` list.
- Confirm the pane already has a binding; status events do not create threads.
- Confirm the watcher socket is the same server used by CLI calls.
- With `include_output: true`, identical status/tail hashes are deduplicated.
- With `include_output: false`, dedupe depends on Herdr state metadata or the
  event payload.

### A control action may have happened despite an error

A Discord API failure can occur after a guarded backend interrupt. `interrupt`
(and its legacy `stop` alias) records `attempt` before the call and `ok`
immediately after it. Inspect `audit_log` and current backend state before
retrying.
