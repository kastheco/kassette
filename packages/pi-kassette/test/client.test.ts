import { describe, expect, it } from "vitest";
import { KassetteClient, type ClientDependencies } from "../src/client.js";

class FakeSocket {
  readyState = 0;
  sent: string[] = [];
  private readonly listeners = new Map<string, Array<(...args: never[]) => void>>();

  on(event: "open" | "close" | "error" | "message", listener: (...args: never[]) => void): void {
    const listeners = this.listeners.get(event) ?? [];
    listeners.push(listener);
    this.listeners.set(event, listeners);
  }

  emit(event: "open" | "close" | "error" | "message", value?: unknown): void {
    if (event === "open") this.readyState = 1;
    if (event === "close") this.readyState = 3;
    for (const listener of this.listeners.get(event) ?? []) listener(value as never);
  }

  send(data: string): void {
    if (this.readyState !== 1) throw new Error("closed");
    this.sent.push(data);
  }

  close(): void {
    this.emit("close");
  }
}

function sessionResponse(): { ok: boolean; status: number; json(): Promise<unknown> } {
  return {
    ok: true,
    status: 201,
    async json() {
      return { session_id: "s1", token: "token", websocket_path: "/socket" };
    },
  };
}

function dependencies(sockets: FakeSocket[]): ClientDependencies {
  let now = 0;
  return {
    request: async () => sessionResponse(),
    createSocket: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    sleep: async (milliseconds) => { now += milliseconds; },
    now: () => now,
  };
}

function client(
  sockets: FakeSocket[],
  connections: string[],
  messages: string[] = [],
): KassetteClient {
  return new KassetteClient(
    {
      baseUrl: "http://127.0.0.1:7860",
      launchCommand: "true",
      reconnectMs: 1_000,
      onMessage: (message) => messages.push(message.type),
      onConnection: (state) => connections.push(state),
    },
    dependencies(sockets),
  );
}

async function tick(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("kassette reconnect fencing", () => {
  it("does not start a close-driven reconnect when a handshake already failed", async () => {
    const sockets: FakeSocket[] = [];
    const connections: string[] = [];
    const connecting = client(sockets, connections).connect();
    await tick();

    sockets[0]?.emit("error", new Error("handshake failed"));
    sockets[0]?.emit("close");
    await tick();
    expect(sockets).toHaveLength(2);

    sockets[1]?.emit("open");
    await connecting;
    await tick();
    expect(sockets).toHaveLength(2);
    expect(connections).toEqual([]);
  });

  it("runs one reconnect chain for repeated close notifications and fences old messages", async () => {
    const sockets: FakeSocket[] = [];
    const connections: string[] = [];
    const messages: string[] = [];
    const kassette = client(sockets, connections, messages);
    const connecting = kassette.connect();
    await tick();
    sockets[0]?.emit("open");
    await connecting;

    sockets[0]?.emit("close");
    sockets[0]?.emit("close");
    await tick();
    expect(connections).toEqual(["reconnecting"]);
    expect(sockets).toHaveLength(2);

    sockets[1]?.emit("open");
    sockets[0]?.emit("message", {
      toString: () => JSON.stringify({ label: "kassette", type: "speech.started", data: {} }),
    });
    sockets[1]?.emit("message", {
      toString: () => JSON.stringify({
        label: "kassette",
        type: "terminal.hello",
        data: {
          session_id: "s1",
          protocol_version: 1,
          capabilities: ["audio.input", "audio.levels", "audio.output", "input.pause", "output.cancel", "output.mute", "transcript.stream", "tts.queue"],
        },
      }),
    });
    await tick();
    expect(messages).toEqual(["terminal.hello"]);
    expect(connections).toEqual(["reconnecting", "connected"]);
    await kassette.close();
  });

  it("returns false instead of throwing when commands arrive during reconnect", async () => {
    const sockets: FakeSocket[] = [];
    const connections: string[] = [];
    const kassette = client(sockets, connections);

    expect(kassette.send("input.pause")).toBe(false);
    const connecting = kassette.connect();
    await tick();
    sockets[0]?.emit("open");
    await connecting;
    expect(kassette.send("input.pause")).toBe(true);
    sockets[0]?.emit("close");
    expect(kassette.send("input.resume")).toBe(false);
    await tick();
    sockets[1]?.emit("open");
    await tick();
    await kassette.close();
  });
});
