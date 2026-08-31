# ADR 0006: Delegate Quicksilver turns to Pi

## Status

Superseded by [ADR 0007](0007-reconcile-quicksilver-turns-on-the-sideband-stream.md).

This record preserves the original terminal-only decision. ADR 0007 broadens client delegation to SmallWebRTC sessions and replaces provider-ID-first turn binding with finalized-transcript reconciliation.

## Context

The Pi terminal client originally required the cascaded provider because that route separates transcription, Pi reasoning, and speech synthesis. Quicksilver already provides lower-latency native audio and turn handling, but its direct mode answers from the provider's own context. Running that direct mode inside Pi would bypass Pi's tools, model, and durable conversation history.

Quicksilver supports client delegation. A native turn can request outside context, wait for the client response, and then render that response as speech. This gives the terminal client a provider-neutral audio path without moving reasoning authority out of Pi.

## Decision

Allow terminal sessions to start either built-in provider selected by `KASSETTE_VOICE_BACKEND`.

When a terminal session selects Quicksilver:

1. Kassette instructs Quicksilver to delegate every user request to the client.
2. Kassette emits a bounded `delegation.requested` event with the provider delegation ID and request text.
3. `pi-kassette` sends that request through Pi's normal or steering delivery mode.
4. The extension collects Pi's completed text response without queuing cascade TTS.
5. The extension sends `delegation.complete` with the original delegation ID and Pi response.
6. Kassette accepts the response only while that delegation ID is pending, appends it to the native turn, and lets Quicksilver render the speech.

Terminal input pause remains an outer audio gate owned by the terminal runtime. Quicksilver acknowledges that control so the same `Space` behavior works for both providers. Output mute and interruption remain outside the provider adapter.

Browser and SmallWebRTC Quicksilver sessions keep direct mode by default. Client delegation is enabled only by the terminal runtime.

## Consequences

- Pi remains authoritative for reasoning, tools, and durable history in both voice modes.
- Quicksilver supplies native transcription, turn handling, and speech without Fish TTS.
- Delegation responses are bounded, paired to one pending provider ID, and rejected after completion or interruption.
- Native transcript events are displayed in the terminal but are not also submitted as Pi messages.
- Provider mode is visible in the terminal surface. Cascade-only auto-send controls are hidden in native mode.
- A Quicksilver turn cannot finish if Pi fails before returning text. Recovery and explicit provider switching remain follow-up work.
