# Apple Watch acceptance checklist

Use this checklist with the supported Herdr Core + Telegram production path.
Run it only with a dedicated temporary Core target; never send acceptance input
to an existing work terminal.

## Record

- Apple Watch model and watchOS version:
- iPhone model and iOS version:
- Telegram iOS version:
- HerdRelay commit:
- Test Core ID and temporary terminal address:
- Telegram topic ID:

## Preconditions

- The Watch is paired and unlocked.
- Telegram notifications are enabled on the iPhone and mirrored to the Watch.
- HerdRelay is running with Telegram enabled.
- The temporary target is the only target added for this acceptance test.
- `enable_send` is true.
- `enable_interrupt` remains false unless interrupt is being tested separately.

## Notification and reply

1. Create one temporary Core test agent and its Telegram topic.
2. Send a two-line readiness message to the topic.
3. Confirm that the Watch receives one notification.
4. Confirm that the agent/topic identity and both lines are readable.
5. Reply from the Watch notification with the supplied unique marker.
6. Confirm that HerdRelay submits the marker to the exact temporary Core target
   once and records one successful transport audit entry.
7. Send a two-line synthetic adapter response for the same target.
8. Confirm that the Watch receives the response once with preserved line breaks.
9. Confirm that the Telegram destination-specific dedupe key exists and the
   inbox event is removed only after all configured routes complete.

## Navigation observations

Record whether the Watch can:

- open the originating Telegram conversation from the notification;
- distinguish topics when several agent notifications exist;
- return to another agent topic without using the iPhone; and
- send a second short reply without duplicate submission.

Topic browsing is an observation, not a release requirement unless the installed
Telegram/watchOS combination actually exposes it. Notification delivery and a
single short reply are the minimum acceptance criteria.

## Cleanup

- Close the temporary Telegram topic.
- Remove the temporary address from the Core allowlist.
- Restart HerdRelay after the allowlist change.
- Kill and remove only the temporary Core terminal.
- Verify the production service is running and the temporary route is marked
  closed in SQLite.
