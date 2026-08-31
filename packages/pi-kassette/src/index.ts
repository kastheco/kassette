import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { KeyId } from "@earendil-works/pi-tui";
import { deliverVoiceTranscript, interruptForBargeIn, transcriptDelivery } from "./bridge.js";
import { KassetteClient, type ClientOptions } from "./client.js";
import { TranscriptDraft } from "./draft.js";
import { mergeEditorDraft, resetVoiceBuffers } from "./lifecycle.js";
import { SpeechChunker } from "./speech.js";
import { initialVoiceState, reduceVoiceState, type ProviderMode, type VoiceAction, type VoiceState } from "./state.js";
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
  let suppressCurrentResponse = false;
  let outputPending = false;
  let providerMode: ProviderMode = "cascaded";
  let pendingDelegationId: string | undefined;
  let pendingDelegationText: string | undefined;
  let surface: VoiceSurface | undefined;
  let editorBeforeVoice = "";
  const draft = new TranscriptDraft();
  const speech = new SpeechChunker();

  const dispatch = (action: VoiceAction): void => {
    state = reduceVoiceState(state, action);
    surface?.invalidate();
  };

  const submitNativeDelegation = (): void => {
    if (!pendingDelegationId || !pendingDelegationText || !ctx) return;
    const text = pendingDelegationText;
    pendingDelegationText = undefined;
    responseCaption = "";
    speech.clear();
    const delivery = transcriptDelivery(ctx.isIdle(), bargedIn);
    dispatch({ type: "thinking" });
    deliverVoiceTranscript(
      text,
      delivery,
      (message, options) => pi.sendUserMessage(message, options),
      () => undefined,
    );
    bargedIn = false;
  };

  const submit = (): void => {
    if (providerMode === "native") {
      submitNativeDelegation();
      return;
    }
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
        outputPending = false;
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
    } else if (message.type === "provider.active") {
      const capabilities = data.capabilities;
      if (capabilities && typeof capabilities === "object" && !Array.isArray(capabilities)) {
        const mode = (capabilities as Record<string, unknown>).mode;
        if (mode === "native" || mode === "cascaded") {
          providerMode = mode;
          dispatch({ type: "provider-mode", mode });
        }
      }
    } else if (message.type === "delegation.requested") {
      const delegationId = data.delegation_id;
      const text = data.text;
      if (
        providerMode === "native"
        && typeof delegationId === "string"
        && typeof text === "string"
        && text.trim()
        && ctx
      ) {
        pendingDelegationId = delegationId;
        pendingDelegationText = text.trim();
        responseCaption = "";
        speech.clear();
        dispatch({ type: "transcript", text: pendingDelegationText, final: true });
        if (state.autoSend) submitNativeDelegation();
      }
    } else if (message.type === "input.audio_started") {
      if (outputPending || state.status === "speaking") client?.send("output.cancel");
      if (ctx && !ctx.isIdle()) {
        suppressCurrentResponse = true;
        ctx.abort();
      }
      outputPending = false;
      speech.clear();
      responseCaption = "";
    } else if (message.type === "audio.level") {
      const direction = data.direction;
      const level = data.level;
      if ((direction === "input" || direction === "output") && typeof level === "number") {
        dispatch({ type: "level", direction, level });
      }
    } else if (message.type === "transcript.delta" || message.type === "transcript.final") {
      const text = data.text;
      if (providerMode === "native" && typeof text === "string") {
        const final = message.type === "transcript.final";
        if (data.role === "user") dispatch({ type: "transcript", text, final });
        else if (data.role === "assistant") responseCaption = text;
        return;
      }
      const turnId = data.turn_id;
      const sequence = data.sequence;
      if (typeof turnId === "string" && typeof text === "string" && typeof sequence === "number") {
        const final = message.type === "transcript.final";
        draft.update({ turnId, text, sequence, final });
        dispatch({ type: "transcript", text: draft.text, final });
        if (final && state.autoSend) submit();
      }
    } else if (message.type === "speech.started") {
      outputPending = false;
      dispatch({ type: "speaking", text: responseCaption });
    } else if (message.type === "speech.stopped") {
      outputPending = false;
      dispatch({ type: "listening" });
    } else if (message.type === "session.state_changed") {
      if (data.state === "listening") dispatch({ type: "listening" });
      else if (data.state === "speaking") dispatch({ type: "speaking", text: responseCaption });
      else if (data.state === "interrupting") dispatch({ type: "interrupted" });
      else if (data.state === "failed") dispatch({ type: "failed", error: "Kassette failed" });
    } else if (message.type === "session.interrupted") {
      outputPending = false;
      pendingDelegationId = undefined;
      pendingDelegationText = undefined;
      suppressCurrentResponse = true;
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
    suppressCurrentResponse = false;
    outputPending = false;
    providerMode = "cascaded";
    pendingDelegationId = undefined;
    pendingDelegationText = undefined;
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
          outputPending = false;
          desiredInputPaused = false;
          client?.send("input.resume");
          dispatch({ type: "input-resumed" });
          return;
        }
        desiredInputPaused = !desiredInputPaused;
        client?.send(desiredInputPaused ? "input.pause" : "input.resume");
      },
      toggleAutoSend: () => {
        dispatch({ type: "toggle-auto-send" });
        if (providerMode === "native" && state.autoSend) submitNativeDelegation();
      },
      submit,
      undo: () => {
        if (providerMode === "native") return;
        draft.undoFinal();
        dispatch({ type: "transcript", text: draft.text, final: true });
      },
      toggleOutputMute: () => {
        dispatch({ type: "toggle-output-mute" });
        client?.send("output.mute", { muted: state.outputMuted });
      },
      interrupt: () => {
        client?.send("output.cancel");
        suppressCurrentResponse = true;
        outputPending = false;
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
  pi.on("agent_start", () => {
    if (!active) return;
    suppressCurrentResponse = false;
    outputPending = false;
    responseCaption = "";
    speech.clear();
    dispatch({ type: "thinking" });
  });
  pi.on("message_start", (event) => {
    if (!active || event.message.role !== "assistant") return;
    responseCaption = "";
    speech.clear();
  });
  pi.on("message_update", (event) => {
    if (!active || suppressCurrentResponse || event.assistantMessageEvent.type !== "text_delta") return;
    const delta = event.assistantMessageEvent.delta;
    responseCaption += delta;
    if (providerMode === "cascaded") speech.push(delta);
  });
  pi.on("agent_settled", () => {
    if (!active) return;
    if (providerMode === "native") {
      if (pendingDelegationId && responseCaption.trim()) {
        client?.send("delegation.complete", {
          delegation_id: pendingDelegationId,
          text: responseCaption.trim().slice(0, 32_000),
        });
        pendingDelegationId = undefined;
        pendingDelegationText = undefined;
      }
    } else if (suppressCurrentResponse) {
      speech.clear();
    } else {
      const [reply] = speech.finish();
      if (reply) outputPending = client?.send("tts.speak", { text: reply }) === true;
    }
    responseCaption = "";
    suppressCurrentResponse = false;
  });
}

export default createPiKassette;
