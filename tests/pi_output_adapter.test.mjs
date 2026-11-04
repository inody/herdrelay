// Run with Node >=22.6: node --experimental-strip-types --test tests/pi_output_adapter.test.mjs
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, rm, stat, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

let serial = 0;

async function setup(t, { herdr = "1", pane = "test:p1" } = {}) {
  const root = await mkdtemp(join(tmpdir(), "herdrelay-pi-test-"));
  const keys = ["HERDR_ENV", "HERDR_PANE_ID", "HERDRELAY_HOOK_INBOX"];
  const original = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  process.env.HERDR_ENV = herdr;
  process.env.HERDR_PANE_ID = pane;
  process.env.HERDRELAY_HOOK_INBOX = join(root, "inbox");
  t.after(async () => {
    for (const key of keys) {
      if (original[key] === undefined) delete process.env[key];
      else process.env[key] = original[key];
    }
    await rm(root, { recursive: true, force: true });
  });
  const url = new URL("../integrations/pi-herdrelay-output.ts", import.meta.url);
  url.searchParams.set("test", String(++serial));
  const { default: adapter } = await import(url.href);
  const handlers = new Map();
  adapter({ on: (name, handler) => handlers.set(name, handler) });
  const inbox = join(root, "inbox");
  return {
    root, inbox, handlers,
    async events() {
      const files = await readdir(inbox).catch((error) => {
        if (error.code === "ENOENT") return [];
        throw error;
      });
      return Promise.all(files.filter((file) => file.endsWith(".json"))
        .map(async (file) => JSON.parse(await readFile(join(inbox, file), "utf8"))));
    },
    async emit(name, event, ctx) {
      return await handlers.get(name)?.(event, ctx);
    },
  };
}

function context(mode = "tui", session = "session-1") {
  return {
    mode,
    hasUI: mode === "tui" || mode === "rpc" || mode === undefined,
    sessionManager: { getSessionId: () => session },
  };
}
function response(text = "返信です\n二行目", timestamp = 123) {
  return { message: { role: "assistant", timestamp, content: [{ type: "text", text }] } };
}

test("TUI relays only after settlement, once, with correct identity and private files", async (t) => {
  const adapter = await setup(t);
  const ctx = context();
  await adapter.emit("message_end", response(), ctx);
  assert.deepEqual(await adapter.events(), []);
  await adapter.emit("agent_settled", { aborted: false }, ctx);
  const [event] = await adapter.events();
  assert.equal(event.text, "返信です\n二行目");
  assert.equal(event.agent, "pi");
  assert.equal(event.pane_id, "test:p1");
  assert.equal(event.session_id, "session-1");
  assert.equal(event.version, 1);
  const file = join(adapter.inbox, `${event.event_id}.json`);
  assert.equal((await stat(file)).mode & 0o777, 0o600);
  assert.equal((await stat(adapter.inbox)).mode & 0o777, 0o700);
  assert.deepEqual(await readdir(adapter.inbox), [`${event.event_id}.json`]);
  await unlink(file);
  await adapter.emit("message_end", response(), ctx);
  await adapter.emit("agent_settled", { aborted: false }, ctx);
  assert.deepEqual(await adapter.events(), []);
});

for (const mode of ["rpc", "json", "print", "unknown", undefined]) {
  test(`does not relay ${String(mode)} mode even with inherited pane environment`, async (t) => {
    const adapter = await setup(t);
    const ctx = context();
    ctx.mode = mode;
    // Prove that hasUI=true is insufficient, including in RPC mode.
    ctx.hasUI = true;
    await adapter.emit("message_end", response(), ctx);
    await adapter.emit("agent_settled", { aborted: false }, ctx);
    assert.deepEqual(await adapter.events(), []);
  });
}

test("settlement handler independently checks TUI mode", async (t) => {
  const adapter = await setup(t);
  await adapter.emit("message_end", response(), context());
  await adapter.emit("agent_settled", {}, context("rpc"));
  assert.deepEqual(await adapter.events(), []);
  await adapter.emit("agent_settled", {}, context());
  assert.equal((await adapter.events()).length, 1);
});

test("interactive TUI input emits a visibility event without storing prompt text", async (t) => {
  const adapter = await setup(t);
  const prompt = "private prompt text";
  await adapter.emit("input", { text: prompt, source: "interactive" }, context());
  const [event] = await adapter.events();
  assert.equal(event.kind, "human_input");
  assert.equal(event.agent, "pi");
  assert.equal(event.pane_id, "test:p1");
  assert.equal(event.session_id, "session-1");
  assert.equal(event.text, undefined);
  assert.doesNotMatch(JSON.stringify(event), /private prompt text/);
});

for (const [mode, source] of [
  ["rpc", "interactive"], ["json", "interactive"], ["print", "interactive"],
  ["tui", "rpc"], ["tui", "extension"],
]) {
  test(`does not mark input from mode=${mode}, source=${source} as human TUI activity`, async (t) => {
    const adapter = await setup(t);
    await adapter.emit("input", { text: "ignored", source }, context(mode));
    assert.deepEqual(await adapter.events(), []);
  });
}

test("assistant continuations and subagent activity alone do not emit visibility events", async (t) => {
  const adapter = await setup(t);
  await adapter.emit("message_end", response("autonomous output"), context());
  await adapter.emit("agent_settled", {}, context());
  const events = await adapter.events();
  assert.equal(events.length, 1);
  assert.equal(events[0].kind, undefined);
  assert.equal(events[0].text, "autonomous output");
});

test("adapter never installs a prompt override or tells the agent to use Discord", async (t) => {
  const adapter = await setup(t);
  assert.deepEqual([...adapter.handlers.keys()].sort(), ["agent_settled", "input", "message_end"]);
  const event = { systemPrompt: "Original instructions", systemPromptOptions: { sections: [] } };
  assert.equal(await adapter.emit("before_agent_start", event, context()), undefined);
  assert.equal(event.systemPrompt, "Original instructions");
  assert.deepEqual(event.systemPromptOptions.sections, []);
});

for (const options of [{ herdr: "0" }, { pane: "" }]) {
  test(`disabled outside a Herdr pane: ${JSON.stringify(options)}`, async (t) => {
    const adapter = await setup(t, options);
    assert.equal(adapter.handlers.size, 0);
    assert.deepEqual(await adapter.events(), []);
  });
}

test("only the latest assistant text is relayed; other roles and thinking are ignored", async (t) => {
  const adapter = await setup(t);
  const ctx = context();
  await adapter.emit("message_end", response("途中の説明"), ctx);
  await adapter.emit("message_end", {
    message: { role: "assistant", timestamp: 124, content: [
      { type: "thinking", thinking: "not public" },
      { type: "text", text: "最終回答" },
      { type: "text", text: "続き" },
    ] },
  }, ctx);
  await adapter.emit("message_end", { message: { role: "toolResult", content: [{ type: "text", text: "tool" }] } }, ctx);
  await adapter.emit("agent_settled", {}, ctx);
  assert.deepEqual((await adapter.events()).map((event) => event.text), ["最終回答\n続き"]);
});

test("inbox failure does not break Pi and retains the response for retry", async (t) => {
  const adapter = await setup(t);
  await writeFile(adapter.inbox, "deliberately block mkdir");
  await adapter.emit("message_end", response(), context());
  await assert.doesNotReject(adapter.emit("agent_settled", {}, context()));
  await unlink(adapter.inbox);
  await adapter.emit("agent_settled", {}, context());
  assert.equal((await adapter.events()).length, 1);
});
