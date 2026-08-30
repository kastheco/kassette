export const PROTOCOL_VERSION = 1;
export const REQUIRED_CAPABILITIES = [
  "audio.input",
  "audio.levels",
  "audio.output",
  "input.pause",
  "output.cancel",
  "output.mute",
  "transcript.stream",
  "tts.queue",
] as const;

export type Envelope = { label: "kassette"; type: string; data: Record<string, unknown> };

export function parseServerMessage(value: unknown): Envelope {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid kassette message");
  const message = value as Record<string, unknown>;
  if (message.label !== "kassette" || typeof message.type !== "string" || message.type.length > 64) {
    throw new Error("invalid kassette envelope");
  }
  if (!message.data || typeof message.data !== "object" || Array.isArray(message.data)) {
    throw new Error("invalid kassette message data");
  }
  const data = message.data as Record<string, unknown>;
  if (message.type === "terminal.hello") {
    if (data.protocol_version !== PROTOCOL_VERSION || !Array.isArray(data.capabilities)) {
      throw new Error("incompatible terminal protocol");
    }
    const capabilities = new Set(data.capabilities.filter((item): item is string => typeof item === "string"));
    if (!REQUIRED_CAPABILITIES.every((capability) => capabilities.has(capability))) {
      throw new Error("required terminal capabilities are missing");
    }
  }
  return { label: "kassette", type: message.type, data };
}

export function command(type: string, data: Record<string, unknown> = {}): Envelope {
  return { label: "kassette", type, data };
}
