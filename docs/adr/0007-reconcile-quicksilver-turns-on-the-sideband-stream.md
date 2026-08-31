# ADR 0007: Reconcile Quicksilver turns on the sideband stream

## Status

Accepted

## Context

Quicksilver client delegation now serves two clients that keep reasoning outside Kassette: klickklack sends browser requests through the active OpenClaw conversation, and `pi-kassette` sends terminal requests through the active Pi session. Kassette still owns the transient audio session, native transcript, interruption state, and provider lifecycle.

The provider does not guarantee that a delegation event arrives before the final user transcript. A delegation can be delayed until after transcript finalization, and some turns can finish without a usable provider delegation event. Binding client work only when `delegation.created` arrives can therefore lose a request, bind it to the next turn, or allow the provider's direct answer to reach the client.

Quicksilver also exposes output audio on the ordered WebSocket sideband while its WebRTC connection carries a mirrored remote audio track. Forwarding the RTP track and processing control events from the sideband gives audio and delegation state different ordering authorities.

## Decision

Enable Quicksilver client delegation for both terminal sessions and switchable SmallWebRTC sessions.

Treat the finalized user transcript as the request boundary:

1. Accumulate user transcript deltas under one Kassette turn ID.
2. Defer provider delegation events while that user turn is active.
3. At transcript finalization, bind a matching deferred provider delegation when one is available.
4. Otherwise emit one bounded `delegation.requested` event with a temporary `kassette:` delegation ID.
5. If the real provider delegation arrives later, reconcile it with the unresolved synthetic record by normalized request text. A bounded fallback may match the single unresolved record when the provider omitted or changed the request text.
6. Keep completed synthetic records only as short-lived reconciliation tombstones. Exclude spoken or expired records from new fallback matches.
7. Accept `delegation.complete` only for the pending client ID. Send the response to a matched real provider delegation when one exists. For an unresolved synthetic delegation, treat client completion as the end of the reconciliation grace period and send the answer as speakable session context immediately; retain the synthetic record as a short-lived tombstone for any later provider delegation.
8. Suppress provider output until the delegated client response starts. If unauthorized direct output has already begun when a synthetic completion arrives, interrupt that provider turn before sending the delegated answer. Client completion alone does not authorize audio from the discarded direct answer.
9. Clear active, deferred, and unresolved turn state on interruption or teardown.

Use the ordered sideband stream as the source of Quicksilver control events and output audio. Convert sideband audio frames into the Pipecat audio path. Continue draining the mirrored WebRTC remote track so transport backpressure cannot stall the provider, but do not forward that duplicate track to clients.

Clients treat delegation IDs as opaque and return them unchanged. Synthetic IDs are an internal recovery mechanism, not a second client protocol.

## Consequences

- klickklack/OpenClaw and Pi remain authoritative for reasoning, tools, and durable history in native mode.
- A finalized request reaches its client even when the provider delegation event is late or missing.
- Late real delegation events can complete the original turn without creating a duplicate client request.
- Completed turns cannot absorb unrelated future provider delegations after the reconciliation window closes.
- A synthetic completion cannot remain silent forever while waiting for a provider event that may never arrive.
- Direct provider audio stays blocked until the delegated response begins, and an in-flight unauthorized turn is cancelled first.
- Audio, transcript, delegation, and interruption events share one ordered source before entering Pipecat.
- The mirrored WebRTC audio track still consumes network and decode work, but it is drained rather than forwarded.
- Automated tests cover the ordering and reconciliation contracts. Physical microphone input, audible playback, and barge-in still require manual validation.
