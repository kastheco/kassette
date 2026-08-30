export type TranscriptDelivery = "normal" | "steer";

export function interruptForBargeIn(abortPi: () => void, clearPendingSpeech: () => void): true {
  clearPendingSpeech();
  abortPi();
  return true;
}

export function transcriptDelivery(idle: boolean, bargedIn: boolean): TranscriptDelivery {
  if (bargedIn || !idle) return "steer";
  return "normal";
}
