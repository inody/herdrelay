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
   uv run herdr-discord-bridge --config config.yaml
   ```

8. Verify `/herdr status`.
9. Bind a Discord thread or channel:

   ```text
   /herdr bind target:1-1
   ```

10. Test `/herdr tail`.

Keep `enable_send` and `enable_approve` disabled until read-only commands work.

## Notes

The first implementation uses Herdr CLI wrappers. The client interface is kept
small so the raw Herdr socket watcher can be added later.

