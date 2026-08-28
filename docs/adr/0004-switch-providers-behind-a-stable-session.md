# ADR 0004: Switch providers behind a stable voice session

## Status

Accepted

## Context

Kassette has two working voice topologies. The cascaded route is a Pipecat chain of VAD, Gemini STT, transcript grooming, Fish TTS, and lifecycle processors. Quicksilver is one native speech-to-speech processor. Selecting either topology only at process startup makes provider comparison expensive and makes a provider failure tear down the WebRTC session, audio lease, and client controls with it.

Provider sessions are disposable. The Kassette session and its durable conversation identity must remain authoritative while adapters are replaced. Delayed callbacks from a closed provider must not commit transcripts or mutate the replacement's state.

## Decision

Add a deep `VoiceProviderRuntime` module between the stable WebRTC input and output processors:

```text
transport input → VoiceProviderRuntime → transport output
                         │
                         └── active provider adapter
```

`VoiceProviderRegistry` resolves a stable provider ID to a capability descriptor and adapter factory. Both built-in routes are real adapters at this seam. `PipelineProviderAdapter` lets each adapter own a nested Pipecat pipeline while the outer transport and worker remain unchanged.

A session has two independent generations:

- `generation` fences replacement of the complete Kassette session;
- `provider_generation` fences callbacks from disposable provider adapters inside that session.

Switching is serialized. The normal path accepts a switch only while listening. A forced switch first interrupts current assistant speech and explicitly reports whether speech was interrupted or a provisional user transcript was discarded. The runtime then:

1. increments `provider_generation` and enters `switching`;
2. closes the old adapter;
3. builds and starts the replacement;
4. waits for the replacement to report `listening` within a bounded timeout;
5. publishes `provider.active` only after readiness.

If replacement setup, startup, or readiness fails, the runtime rebuilds the previous adapter under another provider generation. If rollback also fails, the session enters `failed` and releases the audio lease. Cancellation performs the same rollback before propagating.

Quicksilver can now run with provider lifecycle management disabled. In that mode it emits normalized events while `VoiceProviderRuntime` owns session state and the single audio lease.

## Client contract

Clients send raw SmallWebRTC application messages:

```json
{"label":"kassette","type":"provider.list","data":{}}
```

```json
{
  "label": "kassette",
  "type": "provider.switch",
  "data": {
    "provider_id": "quicksilver",
    "expected_provider_generation": 1,
    "force": false
  }
}
```

Kassette emits bounded, credential-free events:

- `provider.available`
- `provider.switch.requested`
- `provider.switching`
- `provider.active`
- `provider.switch.refused`
- `provider.switch.failed`
- `provider.fallback.active`

`provider.available` includes the active and desired provider IDs, current provider generation, and safe capability summaries. Switch requests may include the observed provider generation so stale UI actions fail without changing the adapter.

## Consequences

- Cascade and Quicksilver can replace each other without restarting Kassette or renegotiating WebRTC.
- The audio lease stays with the stable session and is never transferred to a provider adapter.
- Closing an adapter discards provider-internal context. A future durable conversation adapter must rebuild context from the authoritative Kas runtime rather than migrate hidden provider memory.
- Switching pauses input frame routing behind one lock. Provider startup latency therefore applies backpressure instead of leaking audio to both adapters.
- A provider must emit `listening` before it is considered active. Providers that cannot do so require an explicit adapter policy rather than a silent exception.
- ClickClack still needs a small selector that consumes this contract. It should not hardcode provider credentials or provider-specific capabilities.
