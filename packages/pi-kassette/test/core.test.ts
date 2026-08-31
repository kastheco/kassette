import { describe, expect, it } from "vitest";
import { deliverVoiceTranscript, interruptForBargeIn, transcriptDelivery } from "../src/bridge.js";
import { OwnedService } from "../src/client.js";
import { mergeTranscriptDelta, TranscriptDraft } from "../src/draft.js";
import { mergeEditorDraft, resetVoiceBuffers } from "../src/lifecycle.js";
import { parseServerMessage, REQUIRED_CAPABILITIES } from "../src/protocol.js";
import { SpeechChunker, prepareTextForSpeech } from "../src/speech.js";
import { initialVoiceState, reduceVoiceState, renderVoiceSurface } from "../src/state.js";
import { dispatchVoiceSurfaceInput, voiceSurfaceInput, type VoiceSurfaceActions } from "../src/ui.js";

describe("owned kassette process", () => {
  it("starts once and stops only the process it launched", () => {
    const signals: string[] = [];
    let launches = 0;
    const service = new OwnedService("kassette serve", () => {
      launches += 1;
      return { killed: false, kill: (signal?: string | number) => { signals.push(String(signal)); return true; } };
    });

    service.start();
    service.start();
    service.stop();

    expect(launches).toBe(1);
    expect(signals).toEqual(["SIGTERM"]);
  });
});

describe("terminal protocol", () => {
  it("accepts a compatible hello and rejects missing level telemetry", () => {
    expect(parseServerMessage({
      label: "kassette",
      type: "terminal.hello",
      data: { session_id: "s1", protocol_version: 1, capabilities: [...REQUIRED_CAPABILITIES] },
    }).type).toBe("terminal.hello");

    expect(() => parseServerMessage({
      label: "kassette",
      type: "terminal.hello",
      data: { session_id: "s1", protocol_version: 1, capabilities: ["audio.input"] },
    })).toThrow(/capabilities/);
  });
});

describe("transcript draft", () => {
  it("merges native word deltas while accepting cumulative revisions", () => {
    expect(mergeTranscriptDelta("", "wait")).toBe("wait");
    expect(mergeTranscriptDelta("wait", "for")).toBe("wait for");
    expect(mergeTranscriptDelta("wait for", "wait for enter")).toBe("wait for enter");
    expect(mergeTranscriptDelta("wait for enter", ".")).toBe("wait for enter.");
  });

  it("orders turns, ignores stale updates, and undoes one finalized utterance", () => {
    const draft = new TranscriptDraft();
    draft.update({ turnId: "a", text: "hello", sequence: 2, final: true });
    draft.update({ turnId: "a", text: "stale", sequence: 1, final: false });
    draft.update({ turnId: "b", text: "there", sequence: 3, final: true });

    expect(draft.text).toBe("hello there");
    draft.undoFinal();
    expect(draft.text).toBe("hello");
    draft.update({ turnId: "c", text: "unfinished", sequence: 4, final: false });
    expect(draft.finalizedText).toBe("hello");
    expect(draft.consume()).toBe("hello");
    expect(draft.text).toBe("");
  });

  it("can submit or preserve the visible interim transcript", () => {
    const draft = new TranscriptDraft();
    draft.update({ turnId: "a", text: "still transcribing", sequence: 1, final: false });

    expect(draft.finalizedText).toBe("");
    expect(draft.consumeAll()).toBe("still transcribing");
    expect(draft.text).toBe("");
    expect(draft.update({ turnId: "a", text: "still transcribing again", sequence: 2, final: true })).toBe(false);
    expect(draft.text).toBe("");
  });
});

describe("Pi turn delivery", () => {
  it("aborts Pi, clears pending speech, and steers the finalized barge-in transcript", () => {
    const actions: string[] = [];
    const bargedIn = interruptForBargeIn(
      () => actions.push("abort"),
      () => actions.push("clear-speech"),
    );

    expect(actions).toEqual(["clear-speech", "abort"]);
    expect(transcriptDelivery(false, bargedIn)).toBe("steer");
    expect(transcriptDelivery(true, bargedIn)).toBe("steer");
    expect(transcriptDelivery(false, false)).toBe("steer");
    expect(transcriptDelivery(true, false)).toBe("normal");
  });

  it("submits manual transcripts normally and busy transcripts as steering messages", () => {
    const calls: unknown[] = [];
    const send = (text: string, options?: { deliverAs: "steer" }) => calls.push(["send", text, options]);

    deliverVoiceTranscript("manual message", "normal", send, () => calls.push(["clear"]));
    deliverVoiceTranscript("interrupt message", "steer", send, () => calls.push(["clear"]));

    expect(calls).toEqual([
      ["send", "manual message", undefined],
      ["clear"],
      ["send", "interrupt message", { deliverAs: "steer" }],
    ]);
  });
});

describe("response speech", () => {
  it("buffers a complete reply into one speech request and leaves code visual", () => {
    const chunker = new SpeechChunker();
    expect(chunker.push("First sentence. Partial")).toEqual([]);
    expect(chunker.push(" answer!")).toEqual([]);
    expect(chunker.finish()).toEqual(["First sentence. Partial answer!"]);
    expect(prepareTextForSpeech("Look:\n```ts\nconst x = 1\n```\n[docs](https://x.test)")).toBe(
      "Look. I put the code example in the chat instead of reading it aloud. docs",
    );
  });

  it("never speaks streaming fenced code and clears interrupted replies", () => {
    const chunker = new SpeechChunker();
    expect(chunker.push("Before. ```ts\nconst x = why?\n")).toEqual([]);
    expect(chunker.push("done!\n``` After.")).toEqual([]);
    expect(chunker.finish()).toEqual([
      "Before. I put the code example in the chat instead of reading it aloud. After.",
    ]);
    chunker.push("Interrupted answer.");
    chunker.clear();
    expect(chunker.push("New answer.")).toEqual([]);
    expect(chunker.finish()).toEqual(["New answer."]);
  });
});


describe("voice surface keys", () => {
  it("keeps plain Space distinct from manual-send input", () => {
    expect(voiceSurfaceInput(" ", false)).toBe("toggle-input");
    expect(voiceSurfaceInput("a", false)).toBe("toggle-auto-send");
    expect(voiceSurfaceInput("\u001b[32;2u", false)).toBe("toggle-auto-send");
  });

  it("always exits on Escape even when Pi also maps Escape to interrupt", () => {
    expect(voiceSurfaceInput("\u001b", true)).toBe("exit");
  });

  it("keeps a distinct Pi cancel binding and forwards unknown keys to extension shortcuts", () => {
    expect(voiceSurfaceInput("\u0003", true)).toBe("interrupt");
    expect(voiceSurfaceInput("\u0018", false)).toBe("extension");

    const calls: string[] = [];
    const actions: VoiceSurfaceActions = {
      getState: initialVoiceState,
      toggleInput: () => calls.push("input"),
      toggleAutoSend: () => calls.push("auto"),
      submit: () => calls.push("submit"),
      undo: () => calls.push("undo"),
      toggleOutputMute: () => calls.push("mute"),
      interrupt: () => calls.push("interrupt"),
      exit: () => calls.push("exit"),
    };
    expect(dispatchVoiceSurfaceInput(voiceSurfaceInput("\r", false), actions, () => false)).toBe(true);
    expect(dispatchVoiceSurfaceInput("extension", actions, () => {
      calls.push("extension");
      return true;
    })).toBe(true);
    expect(calls).toEqual(["submit", "extension"]);
  });
});


describe("voice lifecycle", () => {
  it("preserves existing editor text while handing off a finalized draft", () => {
    expect(mergeEditorDraft("existing text", "spoken draft")).toBe("existing text\nspoken draft");
    expect(mergeEditorDraft("existing text", "")).toBe("existing text");
    expect(mergeEditorDraft("existing text  ", "spoken draft")).toBe("existing text  \nspoken draft");

    const draft = new TranscriptDraft();
    const speech = new SpeechChunker();
    draft.update({ turnId: "old", text: "already handed off", sequence: 1, final: true });
    speech.push("old response tail");
    expect(resetVoiceBuffers(draft, speech)).toBe("");
    expect(draft.finalizedText).toBe("");
    expect(speech.finish()).toEqual([]);
  });
});

describe("voice surface", () => {
  it("does not claim the mic is paused until the service acknowledges it", () => {
    const connected = reduceVoiceState(initialVoiceState(), { type: "connected" });
    expect(connected.status).toBe("connecting");
    expect(reduceVoiceState(connected, { type: "input-paused" }).status).toBe("mic paused");
  });

  it("shows manual-send controls for native delegation", () => {
    const native = reduceVoiceState(initialVoiceState(), { type: "provider-mode", mode: "native" });
    const surface = renderVoiceSurface(native, 60, 1_000).join("\n");

    expect(surface).toContain("native voice");
    expect(surface).toContain("manual send");
    expect(surface).toContain("a auto");
    expect(surface).toContain("↵ send");
  });

  it("shows semantic state and real levels, then preserves draft on exit", () => {
    let state = reduceVoiceState(initialVoiceState(), { type: "connected" });
    state = reduceVoiceState(state, { type: "input-resumed" });
    state = reduceVoiceState(state, { type: "level", direction: "input", level: 0.6, at: 1_000 });
    state = reduceVoiceState(state, { type: "transcript", text: "testing", final: false });

    const lines = renderVoiceSurface(state, 48, 1_100);
    expect(lines[0]).toContain("╭ KASSETTE");
    expect(lines.join("\n")).toContain("TRANSCRIBING");
    expect(lines.join("\n")).toContain("you  › testing");
    expect(lines.join("\n")).toMatch(/[█▆▃]/);
    expect(lines.at(-1)).toContain("space mic");
    expect(renderVoiceSurface(state, 48, 2_600).join("\n")).toContain("audio signal lost");

    const quietMic = reduceVoiceState(state, { type: "level", direction: "input", level: 0.006, at: 3_000 });
    expect(renderVoiceSurface(quietMic, 48, 3_010).join("\n")).toMatch(/[▂▃▄▅▆▇█]/);

    const speaking = reduceVoiceState(quietMic, { type: "speaking", text: "assistant reply stays in chat" });
    const speakingSurface = renderVoiceSurface(speaking, 48, 3_020).join("\n");
    expect(speakingSurface).not.toContain("assistant reply stays in chat");
    expect(speakingSurface).toContain("space stop");
  });
});
