export function mergeTranscriptDelta(current: string, update: string): string {
  const existing = current.trim();
  const incoming = update.trim();
  if (!existing) return incoming;
  if (!incoming || existing.endsWith(incoming)) return existing;
  if (incoming.startsWith(existing)) return incoming;

  const overlapLimit = Math.min(existing.length, incoming.length);
  for (let overlap = overlapLimit; overlap > 1; overlap--) {
    if (existing.endsWith(incoming.slice(0, overlap))) {
      return `${existing}${incoming.slice(overlap)}`;
    }
  }

  const joinsWithoutSpace = /^[,.;:!?%)\]}’']/u.test(incoming) || /[(\[{]$/u.test(existing);
  return `${existing}${joinsWithoutSpace ? "" : " "}${incoming}`;
}

export type TranscriptUpdate = {
  turnId: string;
  text: string;
  sequence: number;
  final: boolean;
};

export class TranscriptDraft {
  private readonly turns = new Map<string, TranscriptUpdate>();
  private readonly order: string[] = [];
  private readonly consumedTurns = new Set<string>();

  update(update: TranscriptUpdate): boolean {
    if (this.consumedTurns.has(update.turnId)) return false;
    const previous = this.turns.get(update.turnId);
    if (previous && previous.sequence >= update.sequence) return false;
    const text = update.text.trim();
    if (!text) return false;
    if (!previous) this.order.push(update.turnId);
    this.turns.set(update.turnId, { ...update, text });
    return true;
  }

  get text(): string {
    return this.order.map((id) => this.turns.get(id)?.text ?? "").filter(Boolean).join(" ").trim();
  }

  get finalizedText(): string {
    return this.order
      .map((id) => this.turns.get(id))
      .filter((turn): turn is TranscriptUpdate => turn?.final === true)
      .map((turn) => turn.text)
      .join(" ")
      .trim();
  }

  get interim(): string {
    const last = this.turns.get(this.order.at(-1) ?? "");
    return last && !last.final ? last.text : "";
  }

  undoFinal(): void {
    for (let index = this.order.length - 1; index >= 0; index--) {
      const id = this.order[index];
      if (id && this.turns.get(id)?.final) {
        this.order.splice(index, 1);
        this.turns.delete(id);
        return;
      }
    }
  }

  consume(): string {
    const text = this.finalizedText;
    this.clear();
    return text;
  }

  consumeAll(): string {
    const text = this.text;
    for (const id of this.order) this.consumedTurns.add(id);
    this.turns.clear();
    this.order.length = 0;
    return text;
  }

  clear(): void {
    this.turns.clear();
    this.order.length = 0;
    this.consumedTurns.clear();
  }
}
