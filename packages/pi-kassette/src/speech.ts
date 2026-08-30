const CODE_NOTICE = " I put the code example in the chat instead of reading it aloud. ";

export function prepareTextForSpeech(markdown: string): string {
  let text = markdown.normalize("NFKC");
  text = text.replace(/```[\s\S]*?```/gu, CODE_NOTICE);
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/gu, (_match, alt: string) => alt.trim() ? ` ${alt.trim()}. ` : " I put an image in the chat. ");
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/gu, "$1");
  text = text.replace(/https?:\/\/\S+/giu, "the link in chat");
  text = text.replace(/^\s{0,3}#{1,6}\s+/gmu, "");
  text = text.replace(/^\s{0,3}>\s?/gmu, "");
  text = text.replace(/^\s*[-+*]\s+/gmu, "");
  text = text.replace(/(\*\*|__)(.*?)\1/gu, "$2");
  text = text.replace(/`([^`\n]+)`/gu, "$1");
  text = text.replace(/:\s*\n+/gu, ". ");
  text = text.replace(/\s*\n+\s*/gu, ". ");
  text = text.replace(/([.!?])\s*\.\s*/gu, "$1 ");
  return text.replace(/\s+/gu, " ").trim();
}

export class SpeechChunker {
  private pending = "";
  private scanBuffer = "";
  private insideFence = false;

  push(delta: string): string[] {
    this.scanBuffer += delta;
    this.consumeFences(false);
    return this.emitSentences();
  }

  finish(): string[] {
    this.consumeFences(true);
    if (this.insideFence) {
      this.pending += CODE_NOTICE;
      this.insideFence = false;
    }
    const chunks = this.emitSentences();
    const rest = prepareTextForSpeech(this.pending);
    this.pending = "";
    return rest ? [...chunks, rest] : chunks;
  }

  clear(): void {
    this.pending = "";
    this.scanBuffer = "";
    this.insideFence = false;
  }

  private consumeFences(flush: boolean): void {
    while (true) {
      const marker = this.scanBuffer.indexOf("```");
      if (marker >= 0) {
        const before = this.scanBuffer.slice(0, marker);
        if (!this.insideFence) this.pending += before;
        this.insideFence = !this.insideFence;
        if (!this.insideFence) this.pending += CODE_NOTICE;
        this.scanBuffer = this.scanBuffer.slice(marker + 3);
        continue;
      }
      const retained = flush
        ? 0
        : this.scanBuffer.endsWith("``")
          ? 2
          : this.scanBuffer.endsWith("`")
            ? 1
            : 0;
      const consumable = this.scanBuffer.slice(0, this.scanBuffer.length - retained);
      if (!this.insideFence) this.pending += consumable;
      this.scanBuffer = this.scanBuffer.slice(this.scanBuffer.length - retained);
      if (flush) {
        if (!this.insideFence) this.pending += this.scanBuffer;
        this.scanBuffer = "";
      }
      return;
    }
  }

  private emitSentences(): string[] {
    const chunks: string[] = [];
    const boundary = /[.!?](?:["')\]]?)(?=\s|$)/gu;
    let end = 0;
    for (const match of this.pending.matchAll(boundary)) end = (match.index ?? 0) + match[0].length;
    if (end > 0) {
      const complete = this.pending.slice(0, end);
      this.pending = this.pending.slice(end).trimStart();
      const sentences = complete.match(/[^.!?]+[.!?]["')\]]?/gu) ?? [complete];
      chunks.push(...sentences.map(prepareTextForSpeech).filter(Boolean));
    }
    return chunks;
  }
}
