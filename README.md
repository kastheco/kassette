# kassette

[![CI](https://github.com/kastheco/kassette/actions/workflows/ci.yml/badge.svg)](https://github.com/kastheco/kassette/actions/workflows/ci.yml)

kassette is a local realtime voice service built on Pipecat. It owns transient voice sessions and the audio path. Products and agent runtimes keep their own durable conversations.

The default pipeline is cascaded: Gemini 3.5 Transcribe Live produces provisional and finalized owner transcripts, ClickClack commits finalized turns to its normal message API for OpenClaw, and Fish Audio streams agent responses back through the existing SmallWebRTC connection. Cascade mode can instead use OpenAI's low-latency `gpt-live-transcribe` model for streaming transcripts. Quicksilver GPT-Live is a second runtime-selectable adapter behind the same session and transport.

## Project status

The original local voice-gateway scope is in daily use through the ClickClack Electron app. ClickClack owns the durable OpenClaw conversation while Kassette handles microphone input, live transcription, streamed speech, interruption, and playback.

The repository also includes `pi-kassette`, a Linux terminal voice client for Pi. Pi keeps its normal conversation and reasoning. Kassette owns the transient voice session and local devices.

## Development

```bash
uv sync
cp .env.example .env
# Add GOOGLE_API_KEY and FISH_API_KEY to .env.
# Or select OpenAI transcription and add OPENAI_API_KEY instead.
uv run kassette serve --client-origin http://127.0.0.1:5173
uv run kassette call
```

The local `.env` file is gitignored. `GOOGLE_API_KEY` authenticates `gemini-3.5-transcribe-live`; `FISH_API_KEY` authenticates Fish Audio `s2.1-pro`. `FISH_VOICE_ID` is optional. To use GPT transcription in cascade mode, set `KASSETTE_TRANSCRIPTION_PROVIDER=openai` and `OPENAI_API_KEY`. The default OpenAI model is `gpt-live-transcribe`, configurable through `KASSETTE_OPENAI_TRANSCRIPTION_MODEL`. Set `KASSETTE_VOICE_BACKEND=quicksilver` to use the Codex-authenticated native voice adapter instead of the cascade.

The service binds a loopback address only; `serve` and `call` both reject non-loopback hosts. Browser origins are separate: `--client-origin` allowlists exact additional HTTP(S) origins, which may be non-loopback, so a trusted remote client can reach a loopback-bound listener over an existing private network path. Origins with credentials, a path, a query, or a fragment are rejected.

Message-level playback uses `POST /api/tts` on the same local service. The endpoint accepts `{ "text": "..." }`, returns mono 24 kHz WAV audio, and keeps a small process-local content cache. Product clients should keep their own refresh-scoped audio cache so replay does not call the provider again.

### Pi voice surface

Install the extension from this checkout:

```bash
pi install ./packages/pi-kassette
```

Start Pi normally, then use `/kassette` or `Ctrl+Shift+V`. The voice surface starts with the mic paused. `Space` toggles the mic, `M` mutes playback, and `Escape` returns to Pi's editor. In cascade mode, `Shift+Space` toggles auto-send, `Enter` sends the current transcript, and `Backspace` removes the last finished utterance. In Quicksilver mode, native turns delegate to Pi automatically, then Quicksilver speaks Pi's answer.

The extension connects to `http://127.0.0.1:7860` and starts `kassette serve` when needed. Override those with `KASSETTE_URL` and `KASSETTE_COMMAND`. `KASSETTE_SHORTCUT` changes the activation binding. `KASSETTE_RECONNECT_MS`, `KASSETTE_AUTO_SEND=1`, and `KASSETTE_OUTPUT_MUTED=1` control the remaining client defaults. The service uses the system audio devices unless `KASSETTE_INPUT_DEVICE_INDEX` or `KASSETTE_OUTPUT_DEVICE_INDEX` is set.

Terminal sessions are loopback-only and use a one-use random capability. Their control channel carries transcript, state, and real input/output level events. Normal logs leave transcript and response text out.

Run the extension checks with:

```bash
npm ci --prefix packages/pi-kassette
npm test --prefix packages/pi-kassette
npm run typecheck --prefix packages/pi-kassette
```

### Transcript grooming

kassette defaults to preserving provider transcripts exactly. To apply fast deterministic corrections after STT and before transcript events, copy [`docs/transcript-grooming.example.json`](docs/transcript-grooming.example.json) outside the repository and set:

```bash
KASSETTE_TRANSCRIPT_GROOMING_PROFILE=/absolute/path/to/transcript-grooming.json
```

The version 1 profile supports boundaried word overrides, whitespace normalization, optional lowercase output, and restoration of the pronoun `I`. Rules apply to interim and final transcripts, fail open to provider text, and remain upstream of TTS so grooming cannot delay audio playback. Personal vocabulary belongs in the external profile, not the repository. See [ADR 0003](docs/adr/0003-groom-transcripts-at-a-provider-neutral-seam.md).

### Runtime provider switching

The WebRTC data channel accepts `provider.list` and generation-fenced `provider.switch` application messages. A listening session can replace `cascade` with `quicksilver` and switch back without renegotiating WebRTC or releasing its audio lease. Forced switches interrupt current speech and explicitly report whether a provisional owner transcript was discarded. See [ADR 0004](docs/adr/0004-switch-providers-behind-a-stable-session.md) for the message contract and rollback behavior.

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

GitHub Actions runs the same checks on every push and pull request using Python 3.12.

## Current scope

Implemented:

- Python 3.12 and Pipecat 1.8.0
- ClickClack Electron and OpenClaw integration through ClickClack's normal message path
- identified transient voice sessions with one local audio lease
- loopback-bound SmallWebRTC listener with an explicit browser-origin allowlist
- loopback terminal sessions with service-owned local audio and the Pi voice surface
- selectable Gemini 3.5 Transcribe Live or OpenAI GPT Live Transcribe STT
- provider-neutral provisional and final transcript events
- optional deterministic transcript grooming through external profiles
- Fish Audio streaming TTS for agent responses
- local on-demand WAV generation for message playback through `POST /api/tts`
- GPT-Live behind the isolated Quicksilver adapter, including delegated Pi terminal turns
- generation-fenced runtime switching between cascade and Quicksilver without WebRTC renegotiation
- provider discovery, readiness timeouts, rollback, sanitized diagnostics, interruption, and clean shutdown
- automated reconnect, lifecycle, provider-switching, transcript, TTS, and failure-path coverage

Intentionally outside kassette's scope:

- durable conversation or product state
- remote ingress, TLS, or TURN
- mobile and background-audio clients
- a separate system-wide desktop overlay or target router
- Orca and orkastrator bridges
- cross-device session handoff
- custom-voice enrollment and administration

The original architecture document remains useful for design context, but its phased roadmap is historical: [Unified Realtime Voice Gateway](https://app.notion.com/p/3c9b3a0a9c19811494c7cabc976a27ee?pvs=204).
