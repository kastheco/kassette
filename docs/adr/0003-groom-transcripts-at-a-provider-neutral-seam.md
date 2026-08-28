# ADR 0003: Groom transcripts at a provider-neutral seam

## Status

Accepted

## Context

Speech-to-text providers commonly misrecognize project names and return casing that does not match a user's dictation style. Kassette needs to support those corrections without embedding one user's vocabulary in the open-source service or coupling grooming to Gemini, Fish Audio, or a voice client.

HyperWhisper currently gives the local user useful deterministic behavior: boundaried word overrides and lowercase output with the pronoun `I` restored. Its spoken-symbol replacement is not suitable for Kassette's conversational mode: a recovered transcript changed the ordinary phrase “during that period” into “during that .”.

Live transcript latency must remain independent of TTS. Grooming therefore cannot move transcript events back behind the TTS stage.

## Decision

Add a deep transcript-grooming module with one asynchronous interface:

```python
async def groom(request: TranscriptGroomingRequest) -> GroomedTranscript
```

`TranscriptGroomingProcessor` owns the Pipecat frame integration. It runs after STT and before `CascadedTranscriptEvents`:

```text
transport input → VAD → STT → transcript grooming → transcript events → TTS → speech events → transport output
```

The default adapter is no-op. A deterministic rules adapter supports:

- boundaried word overrides;
- optional lowercase output;
- optional restoration of standalone and contracted `I`;
- whitespace normalization.

Both interim and final frames use deterministic rules so the visible transcript does not change style only at finalization. Future expensive or non-idempotent adapters may inspect `request.final` and groom final frames only.

Profiles are versioned JSON files selected through `KASSETTE_TRANSCRIPT_GROOMING_PROFILE`. Personal profiles live outside the repository. Kassette bounds profile size, override count, override length, and adapter runtime. Grooming failures, timeouts, or suspicious empty results fail open to the provider transcript.

Diagnostics report adapter name, final/interim state, changed status, latency, and error type without logging transcript text. The in-memory result retains only the replacement text needed by the pipeline; raw provider text remains on the frame until a successful replacement.

Spoken-symbol commands are deliberately excluded from the conversational rules adapter.

## Consequences

- Provider adapters and voice clients do not need to know user vocabulary.
- Open-source Kassette remains useful with no personal configuration.
- A custom adapter can satisfy the same interface without changing the pipeline.
- Deterministic profiles add negligible latency and are testable with golden pairs.
- LLM rewriting is deferred. If added, it should be a separate final-only adapter evaluated for semantic preservation, latency, privacy, and fail-open behavior.
