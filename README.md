# herdr-discord-bridge

A local Discord bot for monitoring Herdr panes and agents.

Herdr remains the source of truth for panes, agents, output, cwd, and status.
Discord is only a remote dashboard and control surface.

## Current scope

Implemented first:

- `/herdr status`
- `/herdr tail`
- `/herdr bind`
- `/herdr bindings`
- `/herdr unbind`

Write actions are guarded by config and audit logging:

- `/herdr send`
- `/herdr approve`

Reply-send is available with `enable_reply_send: true`:

- The bot records watcher notification message IDs and target-scoped command
  responses such as `/herdr tail`, `/herdr bind`, `/herdr send`, and
  `/herdr approve`.
- Replying to one of those bot messages sends the reply text to the
  corresponding Herdr target.
- This requires Discord's Message Content Intent for the bot.

Event notifications are available with `enable_watcher: true`:

- `blocked` notifications include recent tail and Approve/Cancel buttons.
- `done` notifications include recent tail.
- The watcher uses Herdr's raw socket API and subscribes to current panes for
  `pane.agent_status_changed` events.

Dashboard updates are available with `enable_dashboard: true`:

- The bot creates one dashboard message in `dashboard_channel_id`.
- It edits that message periodically with current Herdr agent status.
- `/herdr dashboard` refreshes the message manually.
- `/herdr dashboard recreate:true` creates a fresh dashboard message and stores
  that message ID.

## Requirements

- macOS or another local machine that can reach the Herdr CLI and socket.
- Python 3.12 or newer.
- `uv` for dependency installation.
- A Discord server where you can install a bot. A private test server is strongly
  recommended while setting this up.
- Herdr running locally with at least one pane or agent visible.

## Discord setup

1. Open the Discord Developer Portal and create a new application.
2. In the application's Bot page, create a bot and copy its token.
3. Keep the token private. Put it only in `.env`; do not paste it into
   `config.yaml`, issues, logs, or screenshots.
4. In the Bot page, enable Message Content Intent only if you plan to use
   reply-send. Slash commands, read-only status, watcher notifications, and
   approval buttons do not need it.
5. In OAuth2 URL Generator, select these scopes:

   ```text
   bot
   applications.commands
   ```

6. Select the bot permissions needed for the channels where the bridge will
   operate:

   ```text
   View Channels
   Send Messages
   Read Message History
   Use Application Commands
   ```

   Add `Create Public Threads`, `Create Private Threads`, or thread-management
   permissions only if your workflow needs the bot to create or manage threads.

7. Open the generated install URL and add the bot to your Discord server.
8. In Discord, enable Developer Mode in user settings. Then right-click your
   server, target channel, and your user profile to copy their IDs. Putting the
   guild ID in `config.yaml` before first launch lets the bot sync slash commands
   to that server immediately.

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

3. Put the Discord bot token in the `DISCORD_TOKEN` entry in `.env`:

   ```dotenv
   DISCORD_TOKEN=
   ```

4. Put the Discord IDs in `config.yaml`:

   ```yaml
   allowed_guild_ids:
     - 123456789012345678
   allowed_channel_ids:
     - 123456789012345678
   allowed_user_ids:
     - 123456789012345678
   ```

   `allowed_guild_ids` and `allowed_channel_ids` restrict where the bot responds.
   `allowed_user_ids` restricts write actions such as send, reply-send, and
   approval. Read-only commands are still limited by guild and channel.

5. Start with write features disabled in `config.yaml`:

   ```yaml
   enable_send: false
   enable_approve: false
   enable_watcher: false
   enable_dashboard: false
   enable_reply_send: false
   ```

6. Run the bot:

   ```bash
   scripts/run_bot.sh
   ```

7. In Discord, run:

   ```text
   /herdr ids
   ```

   Confirm the IDs match your `config.yaml`. If slash commands do not appear,
   check that `applications.commands` was selected in the install URL, the bot is
   installed in the server, and `allowed_guild_ids` contains that server ID.

8. Restart the bot after editing `.env` or `config.yaml`.

## First verification

1. Confirm the bot can see Herdr:

   ```text
   /herdr status
   ```

2. Bind the current Discord channel or thread to a Herdr target:

   ```text
   /herdr bind target:w7:p2 label:test-agent
   ```

   Use a target shown by `/herdr status`.

3. Read recent output through the binding:

   ```text
   /herdr tail lines:20
   ```

Keep `enable_send`, `enable_approve`, `enable_watcher`, `enable_dashboard`, and
`enable_reply_send` disabled until read-only commands work.

## Feature rollout

Enable features one at a time and restart the bot after each config change.

1. Enable send only for allowlisted users:

   ```yaml
   enable_send: true
   allowed_user_ids:
     - 123456789012345678
   ```

   `submit_after_agent_send: true` sends Enter after `herdr agent send`. In the
   tested environment, `submit_after_agent_send_delay_seconds: 0.5` was needed
   so the agent UI had time to receive the text before Enter.

2. Enable reply-send if you enabled Message Content Intent in the Discord
   Developer Portal:

   ```yaml
   enable_send: true
   enable_reply_send: true
   ```

   You can reply to a bot notification or target-scoped command response with
   short choices such as `1`, `2`, `yes`, or free text. The reply is sent through
   the same guarded send path and is audit-logged.

3. Enable approval:

   ```yaml
   enable_approve: true
   ```

   Approval requires:

   - an allowlisted Discord user
   - a resolved target
   - current Herdr status `blocked`
   - explicit button click after a tail preview

4. Enable watcher:

   ```yaml
   enable_watcher: true
   watcher:
     statuses: ["blocked", "done"]
     reconnect_delay_seconds: 5
     resubscribe_interval_seconds: 300
     blocked_tail_lines: 80
     done_tail_lines: 60
   ```

   Herdr currently requires `pane_id` and `agent_status` filters for
   `events.subscribe`, so the watcher builds subscriptions from the panes visible
   at connection time. It reconnects every `resubscribe_interval_seconds` to pick
   up panes created after startup.

5. Enable dashboard:

   ```yaml
   enable_dashboard: true
   dashboard_channel_id: 123456789012345678
   dashboard:
     refresh_seconds: 60
   ```

   The dashboard message is stored in SQLite and edited in place. If it is
   deleted or you want a fresh one, run:

   ```text
   /herdr dashboard recreate:true
   ```

## Running in the background

For regular use on macOS, install the included user `launchd` service:

```bash
scripts/install_launchd.sh
scripts/status_launchd.sh
```

After changing `.env`, `config.yaml`, or code, restart the service:

```bash
launchctl kickstart -k "gui/$UID/dev.herdr.discord-bridge"
```

To remove it:

```bash
scripts/uninstall_launchd.sh
```

## Operational notes

- Secrets and local runtime files are ignored by Git: `.env`, `config.yaml`, and
  SQLite databases.
- The bot runs in the foreground. Use `scripts/run_bot.sh` from a terminal pane
  or wrap it in your preferred process manager.
- The macOS `launchd` service writes logs to `logs/launchd.out.log` and
  `logs/launchd.err.log`.
- Read/tail/send use Herdr CLI wrappers. Event notifications use the Herdr raw
  socket API.
- Notification dedupe is stored in SQLite in five-minute buckets using
  `pane_id + status + tail hash`.
- Dashboard message state is stored in SQLite under `bot_state`.
