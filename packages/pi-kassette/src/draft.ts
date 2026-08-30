export type TranscriptUpdate = {
  turnId: string;
  text: string;
  sequence: number;
  final: boolean;
};

export class TranscriptDraft {
  private readonly turns = new Map<string, TranscriptUpdate>();
  private readonly order: string[] = [];

  update(update: TranscriptUpdate): boolean {
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
    this.clear();
    return text;
  }

  clear(): void {
    this.turns.clear();
    this.order.length = 0;
  }
}
