type Clearable = { clear(): void };

export function resetVoiceBuffers(draft: Clearable, speech: Clearable): "" {
  draft.clear();
  speech.clear();
  return "";
}

export function mergeEditorDraft(editorText: string, transcriptDraft: string): string {
  const transcript = transcriptDraft.trim();
  if (!editorText) return transcript;
  if (!transcript) return editorText;
  const separator = editorText.endsWith("\n") ? "" : "\n";
  return `${editorText}${separator}${transcript}`;
}
