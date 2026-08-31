import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { EditorTheme, KeybindingsManager, TUI } from "@earendil-works/pi-tui";
import { afterEach, describe, expect, it } from "vitest";
import { createPiKassette, type PiKassetteDependencies, type VoiceClient } from "../src/index.js";
import type { ClientOptions } from "../src/client.js";
import type { Envelope } from "../src/protocol.js";
import type { VoiceSurface } from "../src/ui.js";

type Handler = (...args: any[]) => any;
type EditorFactory = (tui: TUI, theme: EditorTheme, keybindings: KeybindingsManager) => VoiceSurface;

class FakeVoiceClient implements VoiceClient {
  readonly sent: Array<[string, Record<string, unknown>]> = [];

  constructor(readonly options: ClientOptions) {}

  async connect(): Promise<void> {}

  send(type: string, data: Record<string, unknown> = {}): boolean {
    this.sent.push([type, data]);
    return true;
  }

  async close(): Promise<void> {}

  emit(type: string, data: Record<string, unknown>): void {
    this.options.onMessage({ label: "kassette", type, data } as Envelope);
  }
}

function harness(idle = true): {
  command: () => Promise<void>;
  emit: (type: string, data: Record<string, unknown>) => void;
  emitPi: (type: string, event?: unknown) => Promise<void>;
  press: (data: string) => void;
  sentMessages: Array<[string, { deliverAs?: "steer" | "followUp" } | undefined]>;
  getClientCommands: () => Array<[string, Record<string, unknown>]>;
  getAbortCount: () => number;
  setIdle: (value: boolean) => void;
  getEditorText: () => string;
  render: () => string[];
  shutdown: () => Promise<void>;
} {
  const handlers = new Map<string, Handler[]>();
  let commandHandler: Handler | undefined;
  let editorFactory: EditorFactory | undefined;
  let surface: VoiceSurface | undefined;
  let client: FakeVoiceClient | undefined;
  let editorText = "";
  let currentlyIdle = idle;
  let abortCount = 0;
  const sentMessages: Array<[string, { deliverAs?: "steer" | "followUp" } | undefined]> = [];

  const pi = {
    on(type: string, handler: Handler) {
      handlers.set(type, [...(handlers.get(type) ?? []), handler]);
    },
    registerCommand(_name: string, command: { handler: Handler }) {
      commandHandler = command.handler;
    },
    registerShortcut() {},
    sendUserMessage(text: string, options?: { deliverAs?: "steer" | "followUp" }) {
      sentMessages.push([text, options]);
    },
  } as unknown as ExtensionAPI;

  const colorTheme = {
    fg: (_color: string, text: string) => text,
    bold: (text: string) => text,
  };
  const context = {
    hasUI: true,
    mode: "tui",
    isIdle: () => currentlyIdle,
    abort: () => { abortCount += 1; currentlyIdle = true; },
    ui: {
      theme: colorTheme,
      notify: () => undefined,
      getEditorText: () => editorText,
      setEditorText: (text: string) => { editorText = text; },
      setEditorComponent: (factory?: EditorFactory) => {
        editorFactory = factory;
      },
    },
  } as unknown as ExtensionContext;
  const dependencies: PiKassetteDependencies = {
    createClient(options) {
      client = new FakeVoiceClient(options);
      return client;
    },
  };

  createPiKassette(pi, dependencies);
  for (const handler of handlers.get("session_start") ?? []) handler({}, context);

  const ensureSurface = (): VoiceSurface => {
    if (surface) return surface;
    if (!editorFactory) throw new Error("voice editor factory was not installed");
    const tui = { requestRender: () => undefined } as unknown as TUI;
    const editorTheme = {} as EditorTheme;
    const keybindings = { matches: () => false } as unknown as KeybindingsManager;
    surface = editorFactory(tui, editorTheme, keybindings);
    return surface;
  };

  return {
    command: async () => {
      if (!commandHandler) throw new Error("kassette command was not registered");
      await commandHandler("", context);
    },
    emit: (type, data) => {
      if (!client) throw new Error("voice client was not created");
      client.emit(type, data);
    },
    emitPi: async (type, event = {}) => {
      for (const handler of handlers.get(type) ?? []) await handler(event, context);
    },
    press: (data) => ensureSurface().handleInput(data),
    sentMessages,
    getClientCommands: () => client?.sent ?? [],
    getAbortCount: () => abortCount,
    setIdle: (value) => { currentlyIdle = value; },
    getEditorText: () => editorText,
    render: () => ensureSurface().render(80),
    shutdown: async () => {
      for (const handler of handlers.get("session_shutdown") ?? []) await handler({}, context);
    },
  };
}

const originalAutoSend = process.env.KASSETTE_AUTO_SEND;
afterEach(() => {
  if (originalAutoSend === undefined) delete process.env.KASSETTE_AUTO_SEND;
  else process.env.KASSETTE_AUTO_SEND = originalAutoSend;
});

describe.sequential("pi-kassette extension delivery", () => {
  it("submits the visible interim transcript on Enter and ignores its late final", async () => {
    process.env.KASSETTE_AUTO_SEND = "0";
    const app = harness(true);
    await app.command();

    app.emit("transcript.delta", { turn_id: "turn-1", text: "manual voice message", sequence: 1 });
    app.press("\r");
    app.emit("transcript.final", { turn_id: "turn-1", text: "manual voice message", sequence: 2 });

    expect(app.sentMessages).toEqual([["manual voice message", undefined]]);
    app.press("\u001b");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(app.getEditorText()).toBe("");
    await app.shutdown();
  });

  it("auto-sends finalized speech and steers when Pi is busy", async () => {
    delete process.env.KASSETTE_AUTO_SEND;
    const app = harness(false);
    await app.command();

    app.emit("transcript.final", { turn_id: "turn-2", text: "hands free message", sequence: 1 });

    expect(app.sentMessages).toEqual([["hands free message", { deliverAs: "steer" }]]);
    await app.shutdown();
  });

  it("uses Space to stop playback and immediately resume listening", async () => {
    process.env.KASSETTE_AUTO_SEND = "0";
    const app = harness(true);
    await app.command();

    app.emit("speech.started", {});
    app.press(" ");

    expect(app.getClientCommands().slice(-2)).toEqual([
      ["output.cancel", {}],
      ["input.resume", {}],
    ]);
    await app.shutdown();
  });

  it("sends one TTS request only after the complete assistant reply", async () => {
    const app = harness(false);
    await app.command();

    await app.emitPi("agent_start");
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: "Got it. " },
    });
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: "Here is the rest." },
    });

    expect(app.getClientCommands()).toEqual([]);
    await app.emitPi("agent_settled");
    expect(app.getClientCommands()).toEqual([
      ["tts.speak", { text: "Got it. Here is the rest." }],
    ]);
    await app.shutdown();
  });

  it("speaks only the final assistant message after tool turns settle", async () => {
    const app = harness(false);
    await app.command();

    await app.emitPi("agent_start");
    await app.emitPi("message_start", { message: { role: "assistant" } });
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: "I will inspect that." },
    });
    await app.emitPi("message_end", { message: { role: "assistant" } });
    expect(app.getClientCommands()).toEqual([]);

    await app.emitPi("message_start", { message: { role: "assistant" } });
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: "The final answer." },
    });
    await app.emitPi("message_end", { message: { role: "assistant" } });
    await app.emitPi("agent_settled");

    expect(app.getClientCommands()).toEqual([
      ["tts.speak", { text: "The final answer." }],
    ]);
    await app.shutdown();
  });

  it("aborts and suppresses a reply when the user resumes speaking", async () => {
    const app = harness(false);
    await app.command();

    await app.emitPi("agent_start");
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: "Fast acknowledgement." },
    });
    app.emit("input.audio_started", {});
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: " Late response text." },
    });
    await app.emitPi("agent_settled");

    expect(app.getAbortCount()).toBe(1);
    expect(app.getClientCommands()).toEqual([]);
    await app.shutdown();
  });

  it("cancels a completed reply that is queued when user speech resumes", async () => {
    const app = harness(false);
    await app.command();

    await app.emitPi("agent_start");
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: "Queued reply." },
    });
    await app.emitPi("agent_settled");
    app.setIdle(true);
    app.emit("input.audio_started", {});

    expect(app.getClientCommands()).toEqual([
      ["tts.speak", { text: "Queued reply." }],
      ["output.cancel", {}],
    ]);
    expect(app.getAbortCount()).toBe(0);
    await app.shutdown();
  });

  it("switches native Quicksilver turns to manual send and clears stale speaking", async () => {
    const app = harness(true);
    await app.command();
    app.emit("provider.active", {
      provider_id: "quicksilver",
      capabilities: { mode: "native" },
    });

    app.press("a");
    app.emit("transcript.delta", { role: "user", text: "wait" });
    app.emit("transcript.delta", { role: "user", text: "for" });
    app.emit("transcript.delta", { role: "user", text: "enter" });
    expect(app.render().join("\n")).toContain("wait for enter");
    app.emit("speech.started", {});
    app.emit("delegation.requested", {
      delegation_id: "delegation-manual",
      text: "wait for enter",
    });

    expect(app.sentMessages).toEqual([]);
    expect(app.render().join("\n")).toContain("LISTENING");
    expect(app.render().join("\n")).toContain("manual send");

    app.press("\r");
    expect(app.sentMessages).toEqual([["wait for enter", undefined]]);
    await app.shutdown();
  });

  it("delegates native Quicksilver turns through Pi and returns one answer", async () => {
    const app = harness(true);
    await app.command();
    app.emit("provider.active", {
      provider_id: "quicksilver",
      capabilities: { mode: "native" },
    });

    app.emit("transcript.final", { role: "user", text: "voice transcript" });
    expect(app.sentMessages).toEqual([]);

    app.emit("delegation.requested", {
      delegation_id: "delegation-1",
      text: "inspect the repository",
    });
    expect(app.sentMessages).toEqual([["inspect the repository", undefined]]);

    app.setIdle(false);
    await app.emitPi("agent_start");
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: "Pi found " },
    });
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: "the answer." },
    });
    await app.emitPi("agent_settled");

    expect(app.getClientCommands()).toContainEqual([
      "delegation.complete",
      { delegation_id: "delegation-1", text: "Pi found the answer." },
    ]);
    expect(app.getClientCommands().some(([type]) => type === "tts.speak")).toBe(false);
    await app.shutdown();
  });

  it("finishes native playback from the finalized message and clears the sent preview", async () => {
    const app = harness(true);
    await app.command();
    app.emit("provider.active", {
      provider_id: "quicksilver",
      capabilities: { mode: "native" },
    });
    app.emit("transcript.final", { role: "user", text: "what is your name" });
    app.emit("delegation.requested", {
      delegation_id: "delegation-final-message",
      text: "what is your name",
    });

    expect(app.sentMessages).toEqual([["what is your name", undefined]]);
    expect(app.render().join("\n")).toContain("THINKING");
    expect(app.render().join("\n")).not.toContain("you  › what is your name");

    app.setIdle(false);
    await app.emitPi("agent_start");
    await app.emitPi("message_start", { message: { role: "assistant" } });
    await app.emitPi("message_end", {
      message: {
        role: "assistant",
        content: [{ type: "text", text: "I’m Pi." }],
      },
    });
    app.setIdle(true);
    await app.emitPi("agent_settled");

    expect(app.getClientCommands()).toContainEqual([
      "delegation.complete",
      { delegation_id: "delegation-final-message", text: "I’m Pi." },
    ]);
    expect(app.render().join("\n")).toContain("LISTENING");
    await app.shutdown();
  });

  it("drops a delegated response after native playback is interrupted", async () => {
    const app = harness(true);
    await app.command();
    app.emit("provider.active", {
      provider_id: "quicksilver",
      capabilities: { mode: "native" },
    });
    app.emit("delegation.requested", {
      delegation_id: "delegation-1",
      text: "start work",
    });
    app.setIdle(false);
    await app.emitPi("agent_start");
    await app.emitPi("message_update", {
      assistantMessageEvent: { type: "text_delta", delta: "stale answer" },
    });

    app.emit("session.interrupted", {});
    await app.emitPi("agent_settled");

    expect(app.getClientCommands().some(([type]) => type === "delegation.complete")).toBe(false);
    await app.shutdown();
  });

  it("restores an unsent interim transcript when Escape returns to text mode", async () => {
    process.env.KASSETTE_AUTO_SEND = "0";
    const app = harness(true);
    await app.command();

    app.emit("transcript.delta", { turn_id: "turn-3", text: "keep this draft", sequence: 1 });
    app.press("\u001b");
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(app.getEditorText()).toBe("keep this draft");
    await app.shutdown();
  });
});
