// HerdRelay Pi output adapter
// HERDRELAY_ADAPTER_ID=pi-output-v1

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

async function emitResponse(response: PendingResponse): Promise<void> {
  const event = {
    version: 1,
    event_id: response.eventId,
    agent: "pi",
    pane_id: paneId,
    session_id: response.sessionId,
    text: response.text,
    created_at: Date.now() / 1000,
  };
  await mkdir(inbox, { recursive: true, mode: 0o700 });
  const destination = join(inbox, `${response.eventId}.json`);
  const temporary = join(inbox, `.${response.eventId}.${process.pid}.tmp`);
  await writeFile(temporary, JSON.stringify(event), { encoding: "utf8", mode: 0o600 });
  await rename(temporary, destination);
}

export default function (pi: ExtensionAPI) {
  if (!enabled) return;

  let pending: PendingResponse | undefined;
  let deliveredEventId: string | undefined;

  pi.on("before_agent_start", (event) => ({
    systemPrompt: `${event.systemPrompt}\n\n## HerdRelay remote-question policy\nThis Pi session runs through HerdRelay and Discord. When a decision, clarification, or permission is needed, do not perform the dependent action. Instead, finish the current turn with a concise ordinary assistant message containing the question, relevant context, and numbered options when useful. Explicitly ask the user to reply in the Discord thread, then wait for that next message.`,
  }));

  pi.on("message_end", (event, ctx) => {
    if (ctx.hasUI !== true) return;
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
    if (ctx.hasUI !== true || !pending || pending.eventId === deliveredEventId) return;
    const response = pending;
    pending = undefined;
    try {
      await emitResponse(response);
      deliveredEventId = response.eventId;
    } catch {
      // Output relay must never affect the Pi session.
      pending = response;
    }
  });
}
