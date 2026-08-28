# kassette

kassette is a local realtime voice service built on Pipecat. It owns transient voice sessions and the audio path. Products and agent runtimes keep their own durable conversations.

The default pipeline is cascaded: Gemini 3.5 Transcribe Live produces provisional and finalized owner transcripts, ClickClack commits finalized turns to its normal message API for OpenClaw, and Fish Audio streams agent responses back through the existing SmallWebRTC connection. The legacy Quicksilver GPT-Live adapter remains available as an explicit fallback.

## Development

```bash
uv sync
cp .env.example .env
# Add GOOGLE_API_KEY and FISH_API_KEY to .env.
uv run kassette serve --client-origin http://127.0.0.1:5173
uv run kassette call
```

The local `.env` file is gitignored. `GOOGLE_API_KEY` authenticates `gemini-3.5-transcribe-live`; `FISH_API_KEY` authenticates Fish Audio `s2.1-pro`. `FISH_VOICE_ID` is optional.

Message-level playback uses `POST /api/tts` on the same local service. The endpoint accepts `{ "text": "..." }`, returns mono 24 kHz WAV audio, and keeps a small process-local content cache. Product clients should keep their own refresh-scoped audio cache so replay does not call the provider again.

Generate the same sample through Eleven Flash v2.5, Eleven v3 Conversational, Fish S2.1 Pro Free, and Fish S2.1 Pro with:

```bash
uv run python scripts/compare_tts.py
# Or provide your own sample:
uv run python scripts/compare_tts.py --text-file sample.txt
```

The comparison requires `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `FISH_API_KEY`, and optionally `FISH_VOICE_ID` in `.env`. It writes WAV files, timing and cost metadata, and a listening page under `artifacts/tts-comparison/`.

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
- Gemini 3.5 Transcribe Live through Pipecat's streaming STT service
- provider-neutral partial/final transcript events over the WebRTC data channel
- Fish Audio streaming TTS for agent responses
- local on-demand WAV generation for message playback through `POST /api/tts`
- GPT-Live behind an isolated, opt-in Quicksilver adapter
- interruption and clean session shutdown

Not included yet:

- durable state or product-specific clients
- server-owned durable conversation state
- remote ingress, TLS, or TURN
- provider hot swap
- daemon installation and upgrades
- custom voices

Architecture: [Unified Realtime Voice Gateway](https://app.notion.com/p/3c9b3a0a9c19811494c7cabc976a27ee?pvs=204)
