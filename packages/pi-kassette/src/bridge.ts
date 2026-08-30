export type TranscriptDelivery = "normal" | "steer";
export type SendVoiceTranscript = (text: string, options?: { deliverAs: "steer" }) => void;

export function interruptForBargeIn(abortPi: () => void, clearPendingSpeech: () => void): true {
  clearPendingSpeech();
  abortPi();
  return true;
}

export function transcriptDelivery(idle: boolean, bargedIn: boolean): TranscriptDelivery {
  if (bargedIn || !idle) return "steer";
  return "normal";
}

export function deliverVoiceTranscript(
  text: string,
  delivery: TranscriptDelivery,
  send: SendVoiceTranscript,
  clearPendingSpeech: () => void,
): void {
  if (delivery === "normal") {
    send(text);
    return;
  }
  clearPendingSpeech();
  send(text, { deliverAs: "steer" });
}
