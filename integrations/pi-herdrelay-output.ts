// HerdRelay Pi output adapter
// HERDRELAY_ADAPTER_ID=pi-output-v1
// HERDRELAY_ADAPTER_VERSION=3

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createHash } from "node:crypto";
import { mkdir, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

const paneId = process.env.HERDR_PANE_ID;
const enabled = process.env.HERDR_ENV === "1" && !!paneId;
const inbox = process.env.HERDRELAY_HOOK_INBOX
  ?? join(homedir(), ".cache", "herdrelay", "agent-output");

type PendingResponse = {
  eventId: string;
  text: string;
  sessionId: string;
};

function assistantText(message: any): string {
  if (message?.role !== "assistant" || !Array.isArray(message.content)) return "";
  return message.content
    .filter((block: any) => block?.type === "text" && typeof block.text === "string")
    .map((block: any) => block.text)
    .join("\n")
    .trim();
}

async function emitEvent(event: Record<string, unknown> & { event_id: string }): Promise<void> {
  await mkdir(inbox, { recursive: true, mode: 0o700 });
  const destination = join(inbox, `${event.event_id}.json`);
  const temporary = join(inbox, `.${event.event_id}.${process.pid}.tmp`);
  await writeFile(temporary, JSON.stringify(event), { encoding: "utf8", mode: 0o600 });
  await rename(temporary, destination);
}

function eventBase(eventId: string, sessionId: string) {
  return {
    version: 1,
    event_id: eventId,
    agent: "pi",
    pane_id: paneId,
    session_id: sessionId,
    created_at: Date.now() / 1000,
  };
}

export default function (pi: ExtensionAPI) {
  if (!enabled) return;

  let pending: PendingResponse | undefined;
  let deliveredEventId: string | undefined;

  // Mark only input submitted through the interactive TUI. Retries,
  // continuations, tools, subagents, RPC, and extension-generated prompts do
  // not make a Discord thread visible to the user.
  pi.on("input", async (event, ctx) => {
    if (ctx.mode !== "tui" || event.source !== "interactive") return;
    const sessionId = ctx.sessionManager.getSessionId();
    const timestamp = String(Date.now());
    const eventId = createHash("sha256")
      .update(["pi-human-input", sessionId, timestamp, event.text].join("\0"))
      .digest("hex");
    try {
      await emitEvent({ ...eventBase(eventId, sessionId), kind: "human_input" });
    } catch {
      // Visibility tracking must never affect input submission.
    }
  });

  // Relay output without modifying prompts or assuming where the user types.
  // RPC also has hasUI=true and may inherit the parent pane's environment.
  pi.on("message_end", (event, ctx) => {
    if (ctx.mode !== "tui") return;
    const text = assistantText(event.message);
    if (!text) return;
    const sessionId = ctx.sessionManager.getSessionId();
    const timestamp = String((event.message as any).timestamp ?? "");
    const eventId = createHash("sha256")
      .update(["pi", sessionId, timestamp, text].join("\0"))
      .digest("hex");
    pending = { eventId, text, sessionId };
  });

  pi.on("agent_settled", async (_event, ctx) => {
    if (ctx.mode !== "tui" || !pending || pending.eventId === deliveredEventId) return;
    const response = pending;
    pending = undefined;
    try {
      await emitEvent({
        ...eventBase(response.eventId, response.sessionId),
        text: response.text,
      });
      deliveredEventId = response.eventId;
    } catch {
      // Output relay must never affect the Pi session.
      pending = response;
    }
  });
}
