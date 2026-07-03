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

## Setup

1. Create a Discord application and bot.
2. Invite the bot to a private server with application command permissions.
3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
4. Copy `config.example.yaml` to `config.yaml`.
5. Add allowed guild, channel, and user IDs to `config.yaml`.
6. Install dependencies:

   ```bash
   uv sync --extra dev
   ```

7. Run the bot locally:

   ```bash
   scripts/run_bot.sh
   ```

8. Verify `/herdr status`.
9. Bind a Discord thread or channel:

   ```text
   /herdr bind target:1-1
   ```

10. Test `/herdr tail`.

Keep `enable_send`, `enable_approve`, and `enable_watcher` disabled until
read-only commands work.

## Suggested rollout

1. Start read-only:

   ```yaml
   enable_send: false
   enable_approve: false
   enable_watcher: false
   ```

2. Verify:

   ```text
   /herdr ids
   /herdr status
   /herdr bind target:w7:p2 label:test-agent
   /herdr tail lines:20
   ```

3. Enable send only for allowlisted users:

   ```yaml
   enable_send: true
   allowed_user_ids:
     - 123456789012345678
   ```

   `submit_after_agent_send: true` sends Enter after `herdr agent send`. In the
   tested environment, `submit_after_agent_send_delay_seconds: 0.5` was needed
   so the agent UI had time to receive the text before Enter.

4. Enable approval:

   ```yaml
   enable_approve: true
   ```

   Approval requires:

   - an allowlisted Discord user
   - a resolved target
   - current Herdr status `blocked`
   - explicit button click after a tail preview

5. Enable watcher:

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

6. Enable dashboard:

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

## Operational notes

- Secrets and local runtime files are ignored by Git: `.env`, `config.yaml`, and
  SQLite databases.
- The bot runs in the foreground. Use `scripts/run_bot.sh` from a terminal pane
  or wrap it in your preferred process manager.
- On macOS, install it as a user `launchd` service:

  ```bash
  scripts/install_launchd.sh
  scripts/status_launchd.sh
  scripts/uninstall_launchd.sh
  ```

  Logs are written to `logs/launchd.out.log` and `logs/launchd.err.log`.
- Read/tail/send use Herdr CLI wrappers. Event notifications use the Herdr raw
  socket API.
- Notification dedupe is stored in SQLite in five-minute buckets using
  `pane_id + status + tail hash`.
- Dashboard message state is stored in SQLite under `bot_state`.
