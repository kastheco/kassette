# kassette

kassette is a local realtime voice service built on Pipecat. It owns transient voice sessions and the audio path. Products and agent runtimes keep their own durable conversations.

The first delivery puts the full local loop in one process: a localhost service, an in-process Quicksilver adapter, and a SmallWebRTC browser client.

The service starts, the client loads, and direct provider signaling succeeds. See [`docs/live-validation.md`](docs/live-validation.md) for the remaining browser voice check.

## Development

```bash
uv sync
uv run kassette serve
uv run kassette call
```

Run the checks with:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

## Current boundary

Included now:

- Python 3.12 and Pipecat 1.8.0
- identified native voice sessions
- one local audio lease
- localhost-only client transport
- GPT-Live behind an isolated Quicksilver adapter
- interruption and clean session shutdown

Not included yet:

- durable state or product-specific clients
- cascaded STT and TTS providers
- remote ingress, TLS, or TURN
- provider hot swap
- daemon installation and upgrades
- custom voices

Architecture: [Unified Realtime Voice Gateway](https://app.notion.com/p/3c9b3a0a9c19811494c7cabc976a27ee?pvs=204)
