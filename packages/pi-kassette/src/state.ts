export type VoiceStatus = "connecting" | "mic paused" | "listening" | "transcribing" | "thinking" | "speaking" | "interrupted" | "reconnecting" | "failed";
export type ProviderMode = "cascaded" | "native";
export type VoiceState = {
  status: VoiceStatus;
  providerMode: ProviderMode;
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
  | { type: "provider-mode"; mode: ProviderMode }
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

export type VoiceSurfaceStyle = {
  accent(text: string): string;
  text(text: string): string;
  muted(text: string): string;
  success(text: string): string;
  warning(text: string): string;
  error(text: string): string;
  bold(text: string): string;
};

const plainStyle: VoiceSurfaceStyle = {
  accent: (text) => text,
  text: (text) => text,
  muted: (text) => text,
  success: (text) => text,
  warning: (text) => text,
  error: (text) => text,
  bold: (text) => text,
};

export function initialVoiceState(): VoiceState {
  return { status: "connecting", providerMode: "cascaded", inputLevel: 0, outputLevel: 0, transcript: "", response: "", autoSend: false, outputMuted: false, lastInputLevelAt: 0 };
}

export function reduceVoiceState(state: VoiceState, action: VoiceAction): VoiceState {
  switch (action.type) {
    case "connected": return state;
    case "provider-mode": return { ...state, providerMode: action.mode };
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

function tail(text: string, width: number): string {
  const normalized = text.replace(/\s+/gu, " ").trim();
  if (normalized.length <= width) return normalized;
  return `…${normalized.slice(-(width - 1))}`;
}

function statusColor(status: VoiceStatus, style: VoiceSurfaceStyle): (text: string) => string {
  if (status === "failed") return style.error;
  if (status === "listening" || status === "transcribing" || status === "speaking") return style.success;
  if (status === "mic paused" || status === "interrupted" || status === "reconnecting") return style.warning;
  return style.accent;
}

function spectrum(level: number, width: number, phase: number): string {
  const cells = Math.max(12, Math.min(48, width));
  const bars = "▁▂▃▄▅▆▇█";
  const normalized = Math.max(0, Math.min(1, (level - 0.001) / 0.02));
  const boosted = Math.sqrt(normalized);
  if (boosted < 0.025) return "·".repeat(cells);
  const center = (cells - 1) / 2;
  return Array.from({ length: cells }, (_, index) => {
    const distance = Math.abs(index - center) / Math.max(1, center);
    const envelope = 0.35 + 0.65 * (1 - distance ** 1.6);
    const ripple = 0.78 + 0.22 * Math.sin(index * 1.37 + phase);
    const height = Math.max(1, Math.round(boosted * envelope * ripple * (bars.length - 1)));
    return bars[height] ?? "▁";
  }).join("");
}

function frameLine(raw: string, styled: string, innerWidth: number, style: VoiceSurfaceStyle): string {
  const clippedRaw = raw.slice(0, innerWidth);
  const clippedStyled = raw.length > innerWidth ? style.text(clippedRaw) : styled;
  return `${style.accent("│")} ${clippedStyled}${" ".repeat(Math.max(0, innerWidth - clippedRaw.length))} ${style.accent("│")}`;
}

export function renderVoiceSurface(
  state: VoiceState,
  width: number,
  now = Date.now(),
  style: VoiceSurfaceStyle = plainStyle,
): string[] {
  const panelWidth = Math.max(28, width);
  const innerWidth = panelWidth - 4;
  const activeOutput = state.status === "speaking";
  const level = activeOutput ? state.outputLevel : state.inputLevel;
  const inputActive = state.status === "listening" || state.status === "transcribing";
  const inputStale = inputActive && now - state.lastInputLevelAt > 1_500;
  const color = statusColor(state.status, style);
  const status = state.status.toUpperCase();
  const dot = inputStale ? "○" : state.status === "mic paused" ? "Ⅱ" : "●";
  const titleRaw = ` KASSETTE  ${dot} ${status} `;
  const title = `${style.bold(style.accent(" KASSETTE "))}${color(` ${dot} ${status} `)}`;
  const topFill = Math.max(0, panelWidth - titleRaw.length - 2);
  const lines = [`${style.accent("╭")}${title}${style.accent("─".repeat(topFill))}${style.accent("╮")}`];

  const visualRaw = inputStale ? "audio signal lost" : spectrum(level, Math.min(48, innerWidth), now / 120);
  const visualPad = Math.max(0, Math.floor((innerWidth - visualRaw.length) / 2));
  const visualContent = `${" ".repeat(visualPad)}${visualRaw}`;
  lines.push(frameLine(visualContent, inputStale ? style.error(visualContent) : style.accent(visualContent), innerWidth, style));

  const modeRaw = state.providerMode === "native"
    ? `${activeOutput ? "output" : "input"}  native voice  ·  ${state.outputMuted ? "muted" : "sound on"}`
    : `${activeOutput ? "output" : "input"}  ${state.autoSend ? "auto-send" : "manual send"}  ·  ${state.outputMuted ? "muted" : "sound on"}`;
  lines.push(frameLine(modeRaw, style.muted(modeRaw), innerWidth, style));

  const transcriptRaw = state.transcript ? `you  › ${tail(state.transcript, innerWidth - 7)}` : state.status === "mic paused" ? "you  › mic paused" : "you  › say something…";
  const transcriptStyled = state.transcript
    ? `${style.accent("you  › ")}${style.text(tail(state.transcript, innerWidth - 7))}`
    : style.muted(transcriptRaw);
  lines.push(frameLine(transcriptRaw, transcriptStyled, innerWidth, style));

  if (state.error) {
    const errorRaw = `error › ${tail(state.error, innerWidth - 7)}`;
    lines.push(frameLine(errorRaw, style.error(errorRaw), innerWidth, style));
  }

  const hintsRaw = state.status === "speaking"
    ? "space stop · m mute · esc text"
    : state.providerMode === "native"
      ? "space mic · m mute · esc text"
      : "space mic · ⇧space auto · ↵ send · m · esc";
  const hints = hintsRaw.slice(0, innerWidth);
  lines.push(`${style.accent("╰─")}${style.muted(` ${hints} `)}${style.accent("─".repeat(Math.max(0, panelWidth - hints.length - 4)))}${style.accent("╯")}`);
  return lines;
}
