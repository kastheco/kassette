export type TranscriptDelivery = "normal" | "steer" | "hold";

export function interruptForBargeIn(abortPi: () => void, clearPendingSpeech: () => void): true {
  clearPendingSpeech();
  abortPi();
  return true;
}

export function transcriptDelivery(idle: boolean, bargedIn: boolean): TranscriptDelivery {
  if (bargedIn) return "steer";
  return idle ? "normal" : "hold";
}
