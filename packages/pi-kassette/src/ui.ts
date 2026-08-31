import { CustomEditor, type KeybindingsManager, type Theme } from "@earendil-works/pi-coding-agent";
import { matchesKey, type EditorTheme, type TUI } from "@earendil-works/pi-tui";
import { renderVoiceSurface, type VoiceState, type VoiceSurfaceStyle } from "./state.js";

export type VoiceSurfaceInput =
  | "exit"
  | "toggle-auto-send"
  | "toggle-input"
  | "submit"
  | "undo"
  | "toggle-output-mute"
  | "interrupt"
  | "extension";

export function voiceSurfaceInput(data: string, matchesInterrupt: boolean): VoiceSurfaceInput {
  if (matchesKey(data, "escape")) return "exit";
  if (data === " ") return "toggle-input";
  if (data === "a" || data === "A") return "toggle-auto-send";
  if (matchesKey(data, "space")) return "toggle-input";
  if (matchesKey(data, "return")) return "submit";
  if (matchesKey(data, "backspace")) return "undo";
  if (data === "m" || data === "M") return "toggle-output-mute";
  if (matchesInterrupt) return "interrupt";
  return "extension";
}

export type VoiceSurfaceActions = {
  getState: () => VoiceState;
  toggleInput: () => void;
  toggleAutoSend: () => void;
  submit: () => void;
  undo: () => void;
  toggleOutputMute: () => void;
  interrupt: () => void;
  exit: () => void;
};

export function dispatchVoiceSurfaceInput(
  input: VoiceSurfaceInput,
  actions: VoiceSurfaceActions,
  forwardExtensionShortcut: () => boolean,
): boolean {
  if (input === "exit") actions.exit();
  else if (input === "toggle-auto-send") actions.toggleAutoSend();
  else if (input === "toggle-input") actions.toggleInput();
  else if (input === "submit") actions.submit();
  else if (input === "undo") actions.undo();
  else if (input === "toggle-output-mute") actions.toggleOutputMute();
  else if (input === "interrupt") actions.interrupt();
  else return forwardExtensionShortcut();
  return true;
}

export class VoiceSurface extends CustomEditor {
  private readonly staleTimer: ReturnType<typeof setInterval>;

  constructor(
    tui: TUI,
    theme: EditorTheme,
    private readonly voiceKeybindings: KeybindingsManager,
    private readonly actions: VoiceSurfaceActions,
    private readonly colorTheme: Theme,
  ) {
    super(tui, theme, voiceKeybindings, { paddingX: 0 });
    this.staleTimer = setInterval(() => this.tui.requestRender(), 80);
  }

  override handleInput(data: string): void {
    const input = voiceSurfaceInput(
      data,
      this.voiceKeybindings.matches(data, "app.interrupt"),
    );
    const handled = dispatchVoiceSurfaceInput(
      input,
      this.actions,
      () => this.onExtensionShortcut?.(data) ?? false,
    );
    if (!handled) return;
    this.tui.requestRender();
  }

  dispose(): void {
    clearInterval(this.staleTimer);
  }

  invalidate(): void {
    this.tui.requestRender();
  }

  override render(width: number): string[] {
    const style: VoiceSurfaceStyle = {
      accent: (text) => this.colorTheme.fg("accent", text),
      text: (text) => this.colorTheme.fg("text", text),
      muted: (text) => this.colorTheme.fg("muted", text),
      success: (text) => this.colorTheme.fg("success", text),
      warning: (text) => this.colorTheme.fg("warning", text),
      error: (text) => this.colorTheme.fg("error", text),
      bold: (text) => this.colorTheme.bold(text),
    };
    return renderVoiceSurface(this.actions.getState(), width, Date.now(), style);
  }
}
