import { spawn, type ChildProcess } from "node:child_process";
import WebSocket from "ws";
import { command, parseServerMessage, PROTOCOL_VERSION, REQUIRED_CAPABILITIES, type Envelope } from "./protocol.js";

class KassetteServiceError extends Error {}

type ServiceProcess = Pick<ChildProcess, "kill" | "killed">;

type SocketLike = {
  readyState: number;
  on(event: "open", listener: () => void): void;
  on(event: "close", listener: () => void): void;
  on(event: "error", listener: (error: Error) => void): void;
  on(event: "message", listener: (data: { toString(): string }) => void): void;
  send(data: string): void;
  close(): void;
};

type SessionResponse = {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
};

export type ClientDependencies = {
  request: (url: string, init: RequestInit) => Promise<SessionResponse>;
  createSocket: (url: URL) => SocketLike;
  sleep: (milliseconds: number) => Promise<void>;
  now: () => number;
};

const defaultDependencies: ClientDependencies = {
  request: (url, init) => fetch(url, init),
  createSocket: (url) => new WebSocket(url, { maxPayload: 64_000 }),
  sleep: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  now: () => Date.now(),
};

export class OwnedService {
  private process?: ServiceProcess;

  constructor(
    private readonly command: string,
    private readonly launch: (command: string) => ServiceProcess = (command) =>
      spawn(command, { shell: true, stdio: "ignore" }),
  ) {}

  start(): void {
    if (!this.process) this.process = this.launch(this.command);
  }

  stop(): void {
    if (this.process && !this.process.killed) this.process.kill("SIGTERM");
    this.process = undefined;
  }
}

export type ClientOptions = {
  baseUrl: string;
  launchCommand: string;
  reconnectMs: number;
  onMessage: (message: Envelope) => void;
  onConnection: (state: "connected" | "reconnecting" | "failed", error?: string) => void;
};

export class KassetteClient {
  private socket?: SocketLike;
  private readonly ownedService: OwnedService;
  private closed = false;
  private generation = 0;
  private reconnectTask?: Promise<void>;

  constructor(
    private readonly options: ClientOptions,
    private readonly dependencies: ClientDependencies = defaultDependencies,
  ) {
    this.ownedService = new OwnedService(options.launchCommand);
  }

  async connect(): Promise<void> {
    this.closed = false;
    const generation = ++this.generation;
    try {
      await this.open(generation);
    } catch (error) {
      if (error instanceof KassetteServiceError) throw error;
      this.ownedService.start();
      await this.retryUntilReady(generation);
    }
  }

  send(type: string, data: Record<string, unknown> = {}): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    try {
      this.socket.send(JSON.stringify(command(type, data)));
      return true;
    } catch {
      return false;
    }
  }

  async close(): Promise<void> {
    this.closed = true;
    this.generation += 1;
    const socket = this.socket;
    this.socket = undefined;
    socket?.close();
    this.ownedService.stop();
    await this.reconnectTask;
  }

  private async open(generation: number): Promise<void> {
    if (!this.isCurrent(generation)) throw new Error("stale kassette connection attempt");
    const response = await this.dependencies.request(
      `${this.options.baseUrl}/api/terminal/sessions`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ protocol_version: PROTOCOL_VERSION, capabilities: REQUIRED_CAPABILITIES }),
      },
    );
    if (!response.ok) throw new KassetteServiceError(`kassette session failed (${response.status})`);
    const created = await response.json() as Partial<{ session_id: string; token: string; websocket_path: string }>;
    if (!created.session_id || !created.token || !created.websocket_path) {
      throw new KassetteServiceError("kassette returned an invalid terminal session");
    }
    if (!this.isCurrent(generation)) throw new Error("stale kassette connection attempt");
    const websocketUrl = new URL(created.websocket_path, this.options.baseUrl);
    websocketUrl.protocol = websocketUrl.protocol === "https:" ? "wss:" : "ws:";
    websocketUrl.searchParams.set("token", created.token);

    await new Promise<void>((resolve, reject) => {
      const socket = this.dependencies.createSocket(websocketUrl);
      let opened = false;
      let settled = false;
      this.socket = socket;
      const rejectOnce = (error: Error): void => {
        if (settled) return;
        settled = true;
        if (this.socket === socket) this.socket = undefined;
        reject(error);
      };
      socket.on("open", () => {
        if (!this.isCurrent(generation)) {
          socket.close();
          rejectOnce(new Error("stale kassette connection attempt"));
          return;
        }
        opened = true;
        settled = true;
        resolve();
      });
      socket.on("error", (error) => {
        if (!opened) rejectOnce(error);
      });
      socket.on("message", (raw) => {
        if (!this.isCurrent(generation) || this.socket !== socket) return;
        try {
          const message = parseServerMessage(JSON.parse(raw.toString()));
          this.options.onMessage(message);
          if (message.type === "terminal.hello") this.options.onConnection("connected");
        } catch (error) {
          this.generation += 1;
          if (this.socket === socket) this.socket = undefined;
          socket.close();
          this.options.onConnection("failed", error instanceof Error ? error.message : String(error));
        }
      });
      socket.on("close", () => {
        if (this.socket === socket) this.socket = undefined;
        if (opened && this.isCurrent(generation)) this.scheduleReconnect();
        else if (!opened) rejectOnce(new Error("kassette control channel closed during handshake"));
      });
    });
  }

  private scheduleReconnect(): void {
    if (this.closed || this.reconnectTask) return;
    const generation = ++this.generation;
    this.options.onConnection("reconnecting");
    this.reconnectTask = this.retryUntilReady(generation)
      .catch((error: unknown) => {
        if (this.isCurrent(generation)) {
          this.options.onConnection("failed", error instanceof Error ? error.message : String(error));
        }
      })
      .finally(() => {
        this.reconnectTask = undefined;
      });
  }

  private async retryUntilReady(generation: number): Promise<void> {
    const deadline = this.dependencies.now() + this.options.reconnectMs;
    let delay = 100;
    while (this.isCurrent(generation) && this.dependencies.now() < deadline) {
      try {
        await this.open(generation);
        return;
      } catch (error) {
        if (error instanceof KassetteServiceError) throw error;
        await this.dependencies.sleep(delay);
        delay = Math.min(delay * 2, 1_000);
      }
    }
    if (this.isCurrent(generation)) throw new Error("kassette did not become ready");
  }

  private isCurrent(generation: number): boolean {
    return !this.closed && generation === this.generation;
  }
}
