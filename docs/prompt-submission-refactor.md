# Prompt submission refactor

Status: in progress

This document tracks the incremental refactor that makes direct Agent prompt
submission transport-neutral without merging transport UI, General routing,
voice transcription, structured questions, or Orca Bot conversations into one
component.

## Goals

- Keep Telegram and Discord adapters independent of backend-specific exception
  classes.
- Apply exact target identity checks, mutation claim/dedupe, audit, and unknown
  outcome handling through one direct-Agent submission use case.
- Preserve current production behavior until an explicit configuration change
  enables a shared rate-limit policy.
- Keep Orca Bot General separate because it has queue, reply-correlation, and
  Side chat lifecycle semantics that direct Agent prompts do not have.

## Architectural boundary

```text
transport authorization and route lookup
                 │
                 ▼
       PromptSubmissionService
                 │
                 ▼
       backend-neutral RuntimeClient
```

Voice input ends before this boundary by producing text. General routing ends
before this boundary by producing an exact `TargetKey` and verbatim text.
Transport adapters remain responsible for reactions, replies, and structured
question controls.

## Required invariants

1. A persisted `TargetKey` is resolved again immediately before mutation.
2. The resolved live target must reproduce the same normalized identity.
3. A claimed mutation is never submitted twice.
4. A timeout or lost response after submission is `outcome_unknown` and is not
   retried automatically.
5. Backend-specific errors are normalized before they reach transport code.
6. Existing Telegram and Discord user-facing feedback remains unchanged during
   migration unless a separate behavior change is approved.
7. Orca private storage and daemon protocols remain out of scope.

## Incremental stages

- [x] Baseline: shared voice processing and fail-closed General decision
  validation (`0b4e274`).
- [x] Stage 1: add backend-neutral mutation receipt and error contracts without
  changing backend behavior.
- [x] Stage 2: normalize Core, Orca, and legacy TUI prompt results at the backend
  boundary.
- [x] Stage 3: add `PromptSubmissionService` and parity tests while no transport
  uses it.
- [x] Stage 4: migrate Telegram direct-Agent submission.
- [x] Stage 5: migrate Discord General router submission.
- [x] Stage 6: migrate Discord bound-thread submission while leaving structured
  question handling in the Discord adapter.
- [x] Stage 7: remove obsolete transport-specific execution code and verify the
  complete test suite.
- [ ] Stage 8: repeat live Telegram/Discord General and bound acceptance, then
  restart and verify Orca production.

Shared rate-limit configuration is intentionally not part of the mechanical
refactor. The service may accept a policy supplied by each transport so current
behavior remains stable. A later change can move limits to `relay` after the
cross-transport policy is approved.

## Verification log

- Stage 1: `483 passed, 1 skipped`; domain contract only.
- Stage 2: `489 passed, 1 skipped`; Core, Orca, and legacy TUI prompt clients
  return `MutationReceipt` and expose normalized mutation errors while retaining
  transitional backend exception ancestry for unchanged transport behavior.
- Stage 3: `497 passed, 1 skipped`; the unused application service claims each
  event atomically, revalidates exact identity, submits once, finalizes audit,
  and preserves `outcome_unknown` without retrying.
- Stage 4: `497 passed, 1 skipped`; Telegram bound, routed General, and
  text-normalized voice prompts use the shared service. Interrupt confirmation
  remains transport-specific and unchanged.
- Stage 5: `500 passed, 1 skipped`; Discord router-based General submission uses
  the shared service, including claimed blocklist rejection and unknown-outcome
  handling. Orca Bot General remains a separate queue-based use case.
- Stage 6: `501 passed, 1 skipped`; Discord bound text and native voice prompts
  use the shared service and event dedupe. Legacy structured-question selection
  remains in the Discord adapter and retains its one-time claim behavior.
- Stage 7: `502 passed, 1 skipped`; prompt exceptions no longer inherit backend
  CLI/RPC errors, the compatibility backend uses the required receipt contract
  directly, and an import-boundary test protects the application service from
  transport SDK and concrete backend dependencies.
