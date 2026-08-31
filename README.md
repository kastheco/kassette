<table>
  <tr>
    <td width="260" align="center">
      <img src="docs/assets/kassette.png" alt="kassette tape over ocean waves and mount fuji" width="220">
    </td>
    <td>
      <h1>kassette</h1>
      <p><strong>local realtime voice for pi, clickclack, and openclaw.</strong></p>
      <p><a href="https://github.com/kastheco/kassette/actions/workflows/ci.yml"><img src="https://github.com/kastheco/kassette/actions/workflows/ci.yml/badge.svg" alt="ci"></a></p>
    </td>
  </tr>
</table>

kassette is a local realtime voice service built on pipecat. it owns the live audio path and short-lived voice sessions while products and agent runtimes keep their own conversations.

the default path is a cascade. gemini 3.5 transcribe live produces provisional and final transcripts, then fish audio speaks the response returned by the active client. the cascade can use openai's low-latency `gpt-live-transcribe` model instead. quicksilver gpt-live is the second runtime-selectable adapter. clickclack delegates each spoken request through its active openclaw conversation, while pi delegates through its current terminal session. both return the agent's answer to quicksilver for native speech.

## project status

the original local voice-gateway scope is in daily use through the clickclack electron app. clickclack owns the durable openclaw conversation. kassette handles microphone input, live transcription, streamed speech, interruption, and playback.

the repository also includes `pi-kassette`, a linux terminal voice client for pi. pi keeps its normal conversation and reasoning. kassette owns the short-lived voice session and local devices.

## development

```bash
uv sync
cp .env.example .env
# add GOOGLE_API_KEY and FISH_API_KEY to .env.
# or select openai transcription and add OPENAI_API_KEY instead.
uv run kassette serve --client-origin http://127.0.0.1:5173
uv run kassette call
```

the local `.env` file is gitignored. `GOOGLE_API_KEY` authenticates `gemini-3.5-transcribe-live`; `FISH_API_KEY` authenticates fish audio `s2.1-pro`. `FISH_VOICE_ID` is optional. to use gpt transcription in cascade mode, set `KASSETTE_TRANSCRIPTION_PROVIDER=openai` and `OPENAI_API_KEY`. the default openai model is `gpt-live-transcribe`, configurable through `KASSETTE_OPENAI_TRANSCRIPTION_MODEL`. set `KASSETTE_VOICE_BACKEND=quicksilver` to use the codex-authenticated native voice adapter instead of the cascade.

the service only binds to a loopback address. both `serve` and `call` reject non-loopback hosts. browser origins are separate: `--client-origin` allowlists exact additional http(s) origins, including non-loopback origins, so a trusted remote client can reach the loopback listener through an existing private network path. origins with credentials, a path, a query, or a fragment are rejected.

message playback uses `POST /api/tts` on the same local service. the endpoint accepts `{ "text": "..." }`, returns mono 24 khz wav audio, and keeps a small process-local content cache. product clients should keep their own refresh-scoped audio cache so replay doesn't call the provider again.

### pi voice surface

install the extension from this checkout:

```bash
pi install ./packages/pi-kassette
```

start pi normally, then use `/kassette` or `Ctrl+Shift+V`. the voice surface starts with the mic paused. `Space` toggles the mic, `A` toggles auto-send, `Enter` sends the pending request, `M` mutes playback, and `Escape` returns to pi's editor. cascade mode also uses `Backspace` to remove the last finished utterance. in quicksilver mode, auto-send delegates native turns immediately; manual send holds the recognized request until `Enter`. quicksilver speaks pi's answer after the delegated turn completes.

the extension connects to `http://127.0.0.1:7860` and starts `kassette serve` when needed. override those with `KASSETTE_URL` and `KASSETTE_COMMAND`. `KASSETTE_SHORTCUT` changes the activation binding. `KASSETTE_RECONNECT_MS`, `KASSETTE_AUTO_SEND=1`, and `KASSETTE_OUTPUT_MUTED=1` control the remaining client defaults. the service uses the system audio devices unless `KASSETTE_INPUT_DEVICE_INDEX` or `KASSETTE_OUTPUT_DEVICE_INDEX` is set.

terminal sessions are loopback-only and use a one-use random capability. their control channel carries transcript, state, and real input/output level events. normal logs leave transcript and response text out.

run the extension checks with:

```bash
npm ci --prefix packages/pi-kassette
npm test --prefix packages/pi-kassette
npm run typecheck --prefix packages/pi-kassette
```

### transcript grooming

kassette preserves provider transcripts by default. to apply fast deterministic corrections after stt and before transcript events, copy [`docs/transcript-grooming.example.json`](docs/transcript-grooming.example.json) outside the repository and set:

```bash
KASSETTE_TRANSCRIPT_GROOMING_PROFILE=/absolute/path/to/transcript-grooming.json
```

the version 1 profile supports boundaried word overrides, whitespace normalization, optional lowercase output, and restoration of the pronoun `I`. rules apply to interim and final transcripts, fail open to provider text, and stay upstream of tts so grooming can't delay audio playback. personal vocabulary belongs in the external profile, not the repository. see [adr 0003](docs/adr/0003-groom-transcripts-at-a-provider-neutral-seam.md).

### runtime provider switching

the webrtc data channel accepts `provider.list` and generation-fenced `provider.switch` application messages. a listening session can replace `cascade` with `quicksilver` and switch back without renegotiating webrtc or releasing its audio lease. forced switches interrupt current speech and report whether a provisional owner transcript was discarded. see [adr 0004](docs/adr/0004-switch-providers-behind-a-stable-session.md) for the message contract and rollback behavior.

generate the same sample through eleven flash v2.5, eleven v3 conversational, fish s2.1 pro free, and fish s2.1 pro with:

```bash
uv run python scripts/compare_tts.py
# or provide your own sample:
uv run python scripts/compare_tts.py --text-file sample.txt
```

the comparison requires `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `FISH_API_KEY`, and optionally `FISH_VOICE_ID` in `.env`. it writes wav files, timing and cost metadata, and a listening page under `artifacts/tts-comparison/`.

run the checks with:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

github actions runs the same checks on every push and pull request using python 3.12.

## current scope

implemented:

- python 3.12 and pipecat 1.8.0
- clickclack electron and openclaw integration through clickclack's normal message path
- identified transient voice sessions with one local audio lease
- loopback-bound smallwebrtc listener with an explicit browser-origin allowlist
- loopback terminal sessions with service-owned local audio and the pi voice surface
- selectable gemini 3.5 transcribe live or openai gpt live transcribe stt
- provider-neutral provisional and final transcript events
- optional deterministic transcript grooming through external profiles
- fish audio streaming tts for agent responses
- local on-demand wav generation for message playback through `POST /api/tts`
- gpt-live behind the isolated quicksilver adapter, including delegated clickclack/openclaw and pi terminal turns
- generation-fenced runtime switching between cascade and quicksilver without webrtc renegotiation
- provider discovery, readiness timeouts, rollback, sanitized diagnostics, interruption, and clean shutdown
- automated reconnect, lifecycle, provider switching, transcript, tts, and failure-path coverage

intentionally outside kassette's scope:

- durable conversation or product state
- remote ingress, tls, or turn
- mobile and background-audio clients
- a separate system-wide desktop overlay or target router
- orca and orkastrator bridges
- cross-device session handoff
- custom-voice enrollment and administration

the original architecture document still has useful design context, but its phased roadmap is historical: [original voice gateway proposal](https://app.notion.com/p/3c7b3a0a9c1980b1a4c8c859b5322778).
