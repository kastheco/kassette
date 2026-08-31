const CODE_NOTICE = " I put the code example in the chat instead of reading it aloud. ";

export function prepareTextForSpeech(markdown: string): string {
  let text = markdown.normalize("NFKC");
  text = text.replace(/```[\s\S]*?(?:```|$)/gu, CODE_NOTICE);
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

  push(delta: string): string[] {
    this.pending += delta;
    return [];
  }

  finish(): string[] {
    const reply = prepareTextForSpeech(this.pending);
    this.pending = "";
    return reply ? [reply] : [];
  }

  clear(): void {
    this.pending = "";
  }
}
