<table>
  <tr>
    <td width="260" align="center">
      <img src="docs/assets/kassette.png" alt="kassette tape over ocean waves and mount fuji" width="220">
    </td>
    <td>
      <h1>kassette</h1>
      <p><strong>local and hosted realtime voice for pi, clickclack, openclaw, and private product backends.</strong></p>
      <p><a href="https://github.com/kastheco/kassette/actions/workflows/ci.yml"><img src="https://github.com/kastheco/kassette/actions/workflows/ci.yml/badge.svg" alt="ci"></a></p>
    </td>
  </tr>
</table>

kassette is a realtime voice service built on pipecat. local mode owns one machine's audio devices for clickclack and pi. hosted mode runs authenticated, concurrent webrtc sessions behind a trusted product backend. products and agent runtimes keep their own durable conversations in both modes.

the default path is a cascade. gemini 3.5 transcribe live produces provisional and final transcripts, then fish audio speaks the response returned by the active client. the cascade can use openai's low-latency `gpt-live-transcribe` model instead. quicksilver gpt-live is the second runtime-selectable adapter. clickclack delegates each spoken request through its active openclaw conversation, while pi delegates through its current terminal session. both return the agent's answer to quicksilver for native speech.

## project status

the original local voice-gateway scope is in daily use through the clickclack electron app. clickclack owns the durable openclaw conversation. kassette handles microphone input, live transcription, streamed speech, interruption, and playback.

the repository also includes `pi-kassette`, a linux terminal voice client for pi. pi keeps its normal conversation and reasoning. kassette owns the short-lived voice session and local devices.

## local mode

```bash
uv sync
cp .env.example .env
# add GOOGLE_API_KEY and FISH_API_KEY to .env.
# or select openai transcription and add OPENAI_API_KEY instead.
uv run kassette serve --client-origin http://127.0.0.1:5173
uv run kassette call
```

the local `.env` file is gitignored. `GOOGLE_API_KEY` authenticates `gemini-3.5-transcribe-live`; `FISH_API_KEY` authenticates fish audio `s2.1-pro`. `FISH_VOICE_ID` is optional. to use gpt transcription in cascade mode, set `KASSETTE_TRANSCRIPTION_PROVIDER=openai` and `OPENAI_API_KEY`. the default openai model is `gpt-live-transcribe`, configurable through `KASSETTE_OPENAI_TRANSCRIPTION_MODEL`. set `KASSETTE_VOICE_BACKEND=quicksilver` to use the codex-authenticated native voice adapter, or `KASSETTE_VOICE_BACKEND=gemini-live` for delegated Gemini native audio. Gemini Live defaults to `gemini-3.8-live`; set `KASSETTE_GEMINI_LIVE_MODEL=gemini-3.8-live-extended-thinking` to use extended thinking. Both Gemini paths delegate substantive answers to the active Pi or OpenClaw client.

local mode binds to `127.0.0.1:7860` by default. both `serve` and `call` continue to reject non-loopback hosts unless `serve` receives the explicit `--hosted` option. browser origins are separate: `--client-origin` allowlists exact additional http(s) origins. origins with credentials, a path, a query, or a fragment are rejected.

message playback uses `POST /api/tts` on the same local service. the endpoint accepts `{ "text": "..." }`, returns mono 24 khz wav audio, and keeps a small process-local content cache. product clients should keep their own refresh-scoped audio cache so replay doesn't call the provider again.

## hosted mode

hosted mode is an explicit server contract for a private backend deployment. it binds to `0.0.0.0` and reads Railway's `PORT`:

```bash
export PORT=7860
export KASSETTE_SERVICE_SECRET="$(openssl rand -hex 32)"
export KASSETTE_ICE_SERVERS='[{"urls":"turns:turn.example.net:443?transport=tcp","username":"...","credential":"..."}]'
uv run kassette serve --hosted
```

`KASSETTE_SERVICE_SECRET` must contain at least 32 characters. send it as `Authorization: Bearer <secret>` on `POST /start`, `POST` and `PATCH /api/offer`, and session-scoped signaling under `/sessions/{session_id}/...`. hosted mode authenticates every route except `GET /healthz`, which returns a fixed bounded response for Railway health checks. missing or invalid hosted auth returns a bounded `401` response.

Tower is the trust seam for the planned deployment. Tower authenticates users on its HTTPS origin, proxies signaling over Railway private networking, and adds the Kassette service secret server-side. the browser never receives that secret and never connects to Kassette's HTTP listener directly. Kassette authenticates Tower, not end users.

media does not follow the private HTTP path. the browser and Kassette negotiate WebRTC through managed TURN. use authenticated `turns:` on port 443, usually TCP/TLS, and treat host or server-reflexive candidates as optional. do not assign Kassette a public Railway domain or assume that a Railway container has a stable public ICE address. Kassette consumes managed TURN; it does not build or operate a TURN server.

Pipecat receives the validated ICE configuration through its supported `PIPECAT_ICE_SERVERS` surface. Kassette passes the same ICE servers to the authorized signaling client and to its own peer connection. TURN credentials and service credentials are excluded from argv, startup output, validation errors, and lifecycle logs.

hosted sessions use one replacement slot and one media lease per logical session ID. distinct IDs run concurrently. reconnecting one ID replaces only its previous generation, and stale callbacks cannot close or release the newer generation. local and terminal audio retain the process-wide hardware lease.

build and run the production image with:

```bash
podman build --tag kassette:0.2.0 .
podman run --rm \
  --env PORT=7860 \
  --env KASSETTE_SERVICE_SECRET \
  --env KASSETTE_ICE_SERVERS \
  --publish 127.0.0.1:7860:7860 \
  kassette:0.2.0
```

the image installs from `uv.lock`, runs as the non-root `kassette` user, starts with `kassette serve --hosted`, exposes the default application port, and shuts down active session workers and WebRTC connections on termination.

### environment contract

| variable | mode | requirement | purpose |
| --- | --- | --- | --- |
| `PORT` | hosted | required unless `--port` is passed | application port supplied by Railway |
| `KASSETTE_SERVICE_SECRET` | hosted | required, at least 32 characters | dedicated Tower-to-Kassette bearer secret |
| `KASSETTE_ICE_SERVERS` | hosted | required | JSON ICE server array with at least one authenticated `turn:` or `turns:` relay |
| `KASSETTE_VOICE_BACKEND` | both | optional, defaults to `cascade` | selects `cascade`, `quicksilver`, or `gemini-live` |
| `GOOGLE_API_KEY` | both | required for Gemini transcription | provider credential, never used as service auth |
| `OPENAI_API_KEY` | both | required when cascade transcription uses OpenAI | provider credential, never used as service auth |
| `FISH_API_KEY` | both | required for Fish Audio TTS | provider credential, never used as service auth |
| `FISH_MODEL`, `FISH_VOICE_ID` | both | optional | Fish Audio model and voice selection |
| `KASSETTE_TRANSCRIPTION_PROVIDER` | both | optional, defaults to `gemini` | selects cascade STT provider |
| `KASSETTE_OPENAI_TRANSCRIPTION_MODEL` | both | optional | OpenAI transcription model |
| `KASSETTE_GEMINI_LIVE_MODEL`, `KASSETTE_GEMINI_LIVE_THINKING_LEVEL` | both | optional | delegated Gemini Live configuration |
| `KASSETTE_VAD_STOP_SECS`, `KASSETTE_VAD_MIN_VOLUME` | both | optional | voice activity thresholds |
| `KASSETTE_TRANSCRIPT_GROOMING_PROFILE`, `KASSETTE_TRANSCRIPT_GROOMING_TIMEOUT_SECS` | both | optional | external transcript grooming profile and timeout |
| `KASSETTE_INPUT_DEVICE_NAME`, `KASSETTE_OUTPUT_DEVICE_NAME` | local | optional | stable terminal audio device names |
| `KASSETTE_INPUT_DEVICE_INDEX`, `KASSETTE_OUTPUT_DEVICE_INDEX` | local | optional | numeric terminal audio device fallbacks |
| `KASSETTE_TRANSCRIPTION_API_TOKEN` | both | optional, required for batch transcription | separate bearer token for `/v1/audio/transcriptions` |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | tooling | optional | only used by `scripts/compare_tts.py` |

### Screenpipe batch transcription

Kassette exposes `POST /v1/audio/transcriptions` for completed audio chunks using
Google's `gemini-3.5-transcribe` model. This is separate from live voice sessions and
does not change the selected live provider. Audio is sent to Google and uses your
Gemini API quota and billing. The route sends inline audio with `store: false` and
does not create Files API uploads or retain audio or transcripts locally. Google's
API data-use policies still apply.

Set `GOOGLE_API_KEY` and a random, at least 32-character
`KASSETTE_TRANSCRIPTION_API_TOKEN` in the service's local environment, then restart
kassette. The endpoint fails closed without the local token. Do not use your Google
key as the local token. Browser requests with an `Origin` header are rejected.

Configure Screenpipe:

- Transcription engine: `openai-compatible`.
- Endpoint: `http://127.0.0.1:7860` (Screenpipe appends `/v1/audio/transcriptions`).
- API key: the local `KASSETTE_TRANSCRIPTION_API_TOKEN` value.
- Model: `gemini-3.5-transcribe`.
- Raw audio: enabled (PCM16 WAV). MP3 uploads are not supported.
- Live meeting transcription: disabled for this batch-only integration.

The multipart interface accepts `file`, `model`, `response_format=json`, and optional
`language`, `prompt`, and `context`. Comma-separated prompt/context vocabulary is
combined and deduplicated. Responses contain `{"text":"..."}`. WAV files must contain
one or two channels, 8–48 kHz PCM16 audio, and at most 60 seconds. The entire multipart
request is capped at 8 MiB before parsing. At most two requests are active; excess
requests receive HTTP 429 without queueing. The request deadline is 25 seconds, below
Screenpipe's 30-second upstream request budget. Malformed audio is rejected rather
than passed to Google. Auth, provider, and timeout failures return sanitized errors.

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

version 1 profiles support boundaried word overrides, whitespace normalization, optional lowercase output, and restoration of the pronoun `I`. version 2 adds opt-in filler-word filtering and spoken-symbol commands, including `new line`. these transformations are disabled by default and are never enabled for existing version 1 profiles. rules apply to interim and final transcripts, fail open to provider text, and stay upstream of tts so grooming can't delay audio playback. personal vocabulary belongs in the external profile, not the repository. see [adr 0003](docs/adr/0003-groom-transcripts-at-a-provider-neutral-seam.md).

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
- identified transient voice sessions with an explicit process or session lease policy
- loopback-bound local smallwebrtc with an explicit browser-origin allowlist
- authenticated hosted smallwebrtc on `0.0.0.0:$PORT`
- concurrent hosted session IDs with generation-fenced reconnect replacement
- managed STUN/TURN configuration through Pipecat's supported ICE surface
- non-root locked container packaging and bounded `/healthz`
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
- public ingress or TLS termination
- TURN server implementation or operation
- mobile and background-audio clients
- a separate system-wide desktop overlay or target router
- orca and orkastrator bridges
- cross-device session handoff
- custom-voice enrollment and administration

the original architecture document still has useful design context, but its phased roadmap is historical: [original voice gateway proposal](https://app.notion.com/p/3c7b3a0a9c1980b1a4c8c859b5322778).
