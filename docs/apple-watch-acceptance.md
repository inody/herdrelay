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

## Optional General-router acceptance

When `general_router.enabled` is configured with an approved model:

1. Dictate one natural-language routing request into General.
2. Confirm the model receives only opaque candidate IDs and display metadata.
3. Confirm exactly one high-confidence candidate and a verbatim instruction are
   selected, or that ambiguity fails closed without a runtime mutation.
4. Confirm HerdRelay revalidates the exact live target and records one
   `route_text` audit followed by one `send_text` mutation audit.
5. Confirm the selected Agent receives only the extracted instruction, not the
   routing words.

## Optional General voice-message acceptance

When `voice_input.enabled` is configured with an approved local transcriber:

1. Record one short Telegram voice message in General that names exactly one
   temporary Agent and gives one harmless instruction.
2. Confirm HerdRelay displays the transcript and does not retain the temporary
   audio file.
3. Confirm the audit sequence is `transcribe_voice`, `route_text`, then
   `send_text`, and that the Agent receives only the extracted instruction.
4. Repeat with an intentionally ambiguous destination and confirm there is no
   `send_text` audit or runtime mutation.
5. Send a follow-up voice message without a destination and confirm it uses the
   recent General destination when `context_ttl_seconds` is positive.
6. Name a different Agent explicitly and confirm the model switches to it and
   replaces the recent destination.
7. Send `/forget`, then send a destination-free follow-up and confirm it fails
   closed without a runtime mutation.
8. Send an over-duration test file and confirm it is rejected before download
   and transcription.

## Cleanup

- Close the temporary Telegram topic.
- If Core uses `selected` target exposure, remove the temporary address from
  its selector set and restart HerdRelay.
- Kill and remove only the temporary Core terminal.
- Verify the production service is running and the temporary route is marked
  closed in SQLite.
