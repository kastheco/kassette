# Live validation

## Automated checks

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Check the local runner without opening a provider session:

```bash
uv run kassette serve
curl -L http://127.0.0.1:7860/client
```

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

A synthetic end-to-end check passed on 2026-08-27: a SmallWebRTC client sent generated speech through the kassette service and GPT-Live returned 477 audio frames, including 11 non-silent frames. The peer stayed connected throughout the exchange. Physical microphone permissions, interruption, and browser reconnect still need the manual checks above.
