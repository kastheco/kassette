# Live validation

## Automated checks

Observed on the committed KAS-731 lane head on 2026-08-27:

```bash
uv sync --locked                   # resolved 104; checked 102 packages
uv run pytest                      # 51 passed
uv run ruff check .              # passed
uv run ruff format --check .     # 23 files already formatted
uv run pyright                   # 0 errors, 0 warnings, 0 informations
```

Check the local runner without opening a provider session:

```bash
uv run kassette serve
curl -L http://127.0.0.1:7860/client
```

The automated suite starts the executable Pipecat runner on an ephemeral loopback port,
loads `/client/`, verifies `/api/offer` is exposed, stops it, and restarts it on the same
port. A second executable test connects
two real aiortc peers to the runner in sequence using a content-free synthetic provider.
It verifies that reconnect creates a fresh session ID, closes and reaps the prior session,
transfers the single audio lease, propagates peer disconnect, closes the provider once,
and leaves no registered session or lease behind.

Focused behavior tests additionally cover first-input timing, established provider peer/media
disconnect propagation, provider-open and provider-event failure cleanup,
failed interruption recovery, idempotent terminal cleanup, generation-fenced stale callbacks
and lease release, bounded provider parsing, sanitized fixtures, and content-free structured
lifecycle timing records. The fixtures prove credentials, auth and SDP material, raw audio,
transcript contents, hostile metadata, response bodies, and provider-controlled strings are
not emitted by diagnostics or errors and are rejected or truncated at explicit bounds.

## Manual voice check

1. Authenticate the `openai-codex` provider in Pi.
2. Run `uv run kassette serve`.
3. Run `uv run kassette call` in another terminal.
4. Allow microphone access in the browser.
5. Speak one short request and confirm that audio streams back.
6. Interrupt the assistant while it is speaking.
7. Disconnect and reconnect. Confirm that a new identified session starts.
8. Stop kassette. Confirm that the session closes and releases its audio lease.

## Provider handshake

The first direct handshake returned HTTP 403 with `Voice session access denied` because kassette requested the unsupported `cedar` voice. GPT-Live through Codex currently accepts `arbor`, `breeze`, `cove`, `ember`, `juniper`, `maple`, `sol`, `spruce`, or `vale`.

kassette now defaults to `sol`, matching the Pi `/live` package. Direct signaling and the sideband handshake pass.

A live synthetic-audio check inherited from the merged base passed on 2026-08-27: a
SmallWebRTC client sent generated speech through kassette and GPT-Live returned 477 audio
frames, including 11 non-silent frames. The peer stayed connected throughout the exchange.

The KAS-731 lane added repeatable localhost runner and reconnect coverage without loading
credentials or contacting GPT-Live. Final physical-browser microphone permission, audible
playback, barge-in, and browser-button reconnect remain supervisor evidence; they are not
claimed by the automated fixture.
