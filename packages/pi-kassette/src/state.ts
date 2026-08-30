export type VoiceStatus = "connecting" | "mic paused" | "listening" | "transcribing" | "thinking" | "speaking" | "interrupted" | "reconnecting" | "failed";
export type VoiceState = {
  status: VoiceStatus;
  inputLevel: number;
  outputLevel: number;
  transcript: string;
  response: string;
  autoSend: boolean;
  outputMuted: boolean;
  lastInputLevelAt: number;
  error?: string;
};
export type VoiceAction =
  | { type: "connected" }
  | { type: "input-paused" }
  | { type: "input-resumed" }
  | { type: "listening" }
  | { type: "level"; direction: "input" | "output"; level: number; at?: number }
  | { type: "transcript"; text: string; final: boolean }
  | { type: "thinking" }
  | { type: "speaking"; text: string }
  | { type: "interrupted" }
  | { type: "reconnecting" }
  | { type: "failed"; error: string }
  | { type: "toggle-auto-send" }
  | { type: "toggle-output-mute" };

export function initialVoiceState(): VoiceState {
  return { status: "connecting", inputLevel: 0, outputLevel: 0, transcript: "", response: "", autoSend: false, outputMuted: false, lastInputLevelAt: 0 };
}

export function reduceVoiceState(state: VoiceState, action: VoiceAction): VoiceState {
  switch (action.type) {
    case "connected": return state;
    case "input-paused": return { ...state, status: "mic paused", inputLevel: 0 };
    case "input-resumed": return { ...state, status: "listening" };
    case "listening": return state.status === "mic paused" ? state : { ...state, status: "listening", outputLevel: 0 };
    case "level": return {
      ...state,
      [action.direction === "input" ? "inputLevel" : "outputLevel"]: Math.max(0, Math.min(1, action.level)),
      ...(action.direction === "input" ? { lastInputLevelAt: action.at ?? Date.now() } : {}),
    };
    case "transcript": return {
      ...state,
      status: state.status === "mic paused" ? "mic paused" : action.final ? "listening" : "transcribing",
      transcript: action.text,
    };
    case "thinking": return { ...state, status: "thinking", response: "" };
    case "speaking": return { ...state, status: "speaking", response: action.text };
    case "interrupted": return { ...state, status: "interrupted", outputLevel: 0 };
    case "reconnecting": return { ...state, status: "reconnecting" };
    case "failed": return { ...state, status: "failed", error: action.error };
    case "toggle-auto-send": return { ...state, autoSend: !state.autoSend };
    case "toggle-output-mute": return { ...state, outputMuted: !state.outputMuted };
  }
}

function waveform(level: number, width: number): string {
  const cells = Math.max(4, Math.min(24, width - 4));
  const bars = "▁▂▃▄▅▆▇█";
  const height = Math.round(level * (bars.length - 1));
  const center = (cells - 1) / 2;
  return Array.from({ length: cells }, (_, index) => {
    const distance = Math.abs(index - center) / Math.max(1, center);
    return bars[Math.max(0, Math.round(height * (1 - distance * 0.65)))] ?? "▁";
  }).join("");
}

function tail(text: string, width: number): string {
  if (text.length <= width) return text;
  return `…${text.slice(-(width - 1))}`;
}

export function renderVoiceSurface(state: VoiceState, width: number, now = Date.now()): string[] {
  const usable = Math.max(12, width);
  const level = state.status === "speaking" ? state.outputLevel : state.inputLevel;
  const flags = `${state.autoSend ? "AUTO-SEND" : "MANUAL"} · ${state.outputMuted ? "OUTPUT MUTED" : "OUTPUT ON"}`;
  const lines = [` ${state.status.toUpperCase()} · ${flags}`];
  const inputActive = state.status === "listening" || state.status === "transcribing";
  const inputStale = inputActive && now - state.lastInputLevelAt > 1_500;
  if (inputStale) lines.push(" AUDIO LEVELS STALE");
  else if (usable >= 28) lines.push(` ${waveform(level, usable - 2)}`);
  if (state.transcript) lines.push(` You: ${tail(state.transcript, usable - 6)}`);
  if (state.response) lines.push(` Pi: ${tail(state.response, usable - 5)}`);
  if (state.error) lines.push(` ${tail(state.error, usable - 2)}`);
  lines.push(" Space mic · ⇧Space auto · Enter send · M mute · Esc text");
  return lines;
}
