import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { KeyId } from "@earendil-works/pi-tui";
import { deliverVoiceTranscript, interruptForBargeIn, transcriptDelivery } from "./bridge.js";
import { KassetteClient, type ClientOptions } from "./client.js";
import { TranscriptDraft } from "./draft.js";
import { mergeEditorDraft, resetVoiceBuffers } from "./lifecycle.js";
import { SpeechChunker } from "./speech.js";
import { initialVoiceState, reduceVoiceState, type VoiceAction, type VoiceState } from "./state.js";
import { VoiceSurface } from "./ui.js";
import type { Envelope } from "./protocol.js";

export type VoiceClient = Pick<KassetteClient, "connect" | "send" | "close">;
export type PiKassetteDependencies = {
  createClient(options: ClientOptions): VoiceClient;
};

const defaultDependencies: PiKassetteDependencies = {
  createClient: (options) => new KassetteClient(options),
};

export function createPiKassette(
  pi: ExtensionAPI,
  dependencies: PiKassetteDependencies = defaultDependencies,
): void {
  let ctx: ExtensionContext | undefined;
  let client: VoiceClient | undefined;
  let state = initialVoiceState();
  let active = false;
  let inputPaused = false;
  let desiredInputPaused = true;
  let bargedIn = false;
  let responseCaption = "";
  let surface: VoiceSurface | undefined;
  let editorBeforeVoice = "";
  const draft = new TranscriptDraft();
  const speech = new SpeechChunker();

  const dispatch = (action: VoiceAction): void => {
    state = reduceVoiceState(state, action);
    surface?.invalidate();
  };

  const submit = (): void => {
    if (!draft.text || !ctx) return;
    const delivery = transcriptDelivery(ctx.isIdle(), bargedIn);
    const text = draft.consumeAll();
    dispatch({ type: "transcript", text: "", final: true });
    dispatch({ type: "thinking" });
    deliverVoiceTranscript(
      text,
      delivery,
      (message, options) => pi.sendUserMessage(message, options),
      () => {
        speech.clear();
        responseCaption = "";
      },
    );
    bargedIn = false;
  };

  const handleMessage = (message: Envelope): void => {
    const data = message.data;
    if (message.type === "terminal.hello") {
      dispatch({ type: "connected" });
      client?.send(desiredInputPaused ? "input.pause" : "input.resume");
      if (state.outputMuted) client?.send("output.mute", { muted: true });
    } else if (message.type === "audio.level") {
      const direction = data.direction;
      const level = data.level;
      if ((direction === "input" || direction === "output") && typeof level === "number") {
        dispatch({ type: "level", direction, level });
      }
    } else if (message.type === "transcript.delta" || message.type === "transcript.final") {
      const turnId = data.turn_id;
      const text = data.text;
      const sequence = data.sequence;
      if (typeof turnId === "string" && typeof text === "string" && typeof sequence === "number") {
        const final = message.type === "transcript.final";
        draft.update({ turnId, text, sequence, final });
        dispatch({ type: "transcript", text: draft.text, final });
        if (final && state.autoSend) submit();
      }
    } else if (message.type === "speech.started") {
      dispatch({ type: "speaking", text: responseCaption });
    } else if (message.type === "speech.stopped") {
      dispatch({ type: "listening" });
    } else if (message.type === "session.state_changed") {
      if (data.state === "listening") dispatch({ type: "listening" });
      else if (data.state === "speaking") dispatch({ type: "speaking", text: responseCaption });
      else if (data.state === "interrupting") dispatch({ type: "interrupted" });
      else if (data.state === "failed") dispatch({ type: "failed", error: "Kassette failed" });
    } else if (message.type === "session.interrupted") {
      bargedIn = interruptForBargeIn(
        () => ctx?.abort(),
        () => speech.clear(),
      );
      responseCaption = "";
      dispatch({ type: "interrupted" });
    } else if (message.type === "input.state_changed") {
      inputPaused = data.paused === true;
      dispatch({ type: inputPaused ? "input-paused" : "input-resumed" });
    } else if (message.type === "session.error") {
      dispatch({ type: "failed", error: typeof data.message === "string" ? data.message : "Kassette failed" });
    }
  };

  const exit = async (): Promise<void> => {
    if (!active || !ctx) return;
    const text = draft.text;
    active = false;
    await client?.close();
    client = undefined;
    surface?.dispose();
    ctx.ui.setEditorComponent(undefined);
    surface = undefined;
    ctx.ui.setEditorText(mergeEditorDraft(editorBeforeVoice, text));
    responseCaption = resetVoiceBuffers(draft, speech);
    bargedIn = false;
    inputPaused = false;
    desiredInputPaused = true;
    editorBeforeVoice = "";
  };

  const enter = async (): Promise<void> => {
    if (!ctx?.hasUI || ctx.mode !== "tui") {
      ctx?.ui.notify("pi-kassette requires Pi's terminal UI", "warning");
      return;
    }
    if (active) return exit();
    active = true;
    const colorTheme = ctx.ui.theme;
    editorBeforeVoice = ctx.ui.getEditorText();
    state = { ...initialVoiceState(), autoSend: process.env.KASSETTE_AUTO_SEND !== "0", outputMuted: process.env.KASSETTE_OUTPUT_MUTED === "1" };
    const actions = {
      getState: (): VoiceState => state,
      toggleInput: () => {
        if (state.status === "speaking") {
          client?.send("output.cancel");
          desiredInputPaused = false;
          client?.send("input.resume");
          dispatch({ type: "input-resumed" });
          return;
        }
        desiredInputPaused = !desiredInputPaused;
        client?.send(desiredInputPaused ? "input.pause" : "input.resume");
      },
      toggleAutoSend: () => dispatch({ type: "toggle-auto-send" }),
      submit,
      undo: () => {
        draft.undoFinal();
        dispatch({ type: "transcript", text: draft.text, final: true });
      },
      toggleOutputMute: () => {
        dispatch({ type: "toggle-output-mute" });
        client?.send("output.mute", { muted: state.outputMuted });
      },
      interrupt: () => {
        client?.send("output.cancel");
        ctx?.abort();
        speech.clear();
        responseCaption = "";
        dispatch({ type: "interrupted" });
      },
      exit: () => void exit(),
    };
    ctx.ui.setEditorComponent((tui, theme, keybindings) => {
      surface = new VoiceSurface(tui, theme, keybindings, actions, colorTheme);
      return surface;
    });
    client = dependencies.createClient({
      baseUrl: process.env.KASSETTE_URL ?? "http://127.0.0.1:7860",
      launchCommand: process.env.KASSETTE_COMMAND ?? "kassette serve",
      reconnectMs: Number(process.env.KASSETTE_RECONNECT_MS ?? 8_000),
      onMessage: handleMessage,
      onConnection: (connection, error) => dispatch(connection === "connected" ? { type: "connected" } : connection === "reconnecting" ? { type: "reconnecting" } : { type: "failed", error: error ?? "Kassette connection failed" }),
    });
    try {
      await client.connect();
    } catch (error) {
      dispatch({ type: "failed", error: error instanceof Error ? error.message : String(error) });
    }
  };

  const activationShortcut = (process.env.KASSETTE_SHORTCUT ?? "ctrl+shift+v") as KeyId;
  pi.registerCommand("kassette", { description: "Toggle the Kassette voice surface", handler: enter });
  pi.registerShortcut(activationShortcut, { description: "Toggle the Kassette voice surface", handler: enter });

  pi.on("session_start", (_event, context) => { ctx = context; });
  pi.on("session_before_switch", async () => { await exit(); });
  pi.on("session_before_fork", async () => { await exit(); });
  pi.on("session_shutdown", async () => { await exit(); ctx = undefined; });
  pi.on("agent_start", () => { if (active) dispatch({ type: "thinking" }); });
  pi.on("message_update", (event) => {
    if (!active || event.assistantMessageEvent.type !== "text_delta") return;
    const delta = event.assistantMessageEvent.delta;
    responseCaption += delta;
    dispatch({ type: "speaking", text: responseCaption });
    for (const chunk of speech.push(delta)) client?.send("tts.speak", { text: chunk });
  });
  pi.on("message_end", () => {
    if (!active) return;
    for (const chunk of speech.finish()) client?.send("tts.speak", { text: chunk });
    responseCaption = "";
  });
}

export default createPiKassette;
