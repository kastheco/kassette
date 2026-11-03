# changelog

## unreleased

### added

- `aws` transcription provider using AWS Transcribe streaming, with credentials from the standard AWS chain, an optional region, and an optional custom vocabulary and vocabulary filter applied with `remove`
- `kassette dictate`, a macos toggle dictation mode: one global hotkey starts and stops recording, then kassette waits for the final AWS transcript, grooms it and pastes it at the cursor while preserving the clipboard. it talks to AWS Transcribe through `amazon-transcribe` and records with `sounddevice`, without pipecat
- dictation docs covering Microphone and Accessibility permissions, AWS setup and a sample dictation grooming profile
- ci license report for `kassette[dictate]`, which fails on any license outside MIT, MIT-0, BSD, Apache-2.0 and PSF and on any voice-stack package

### changed

- **breaking for installs:** pipecat, aiortc, av, google-genai, aiohttp and typer moved from the base dependencies to a `voice` extra. install `kassette[voice]` (or `uv sync --extra voice`) for `kassette serve` and `kassette call`. the container image installs it. `kassette dictate` needs only the new `dictate` extra
- `kassette.transcript_grooming` is now pipecat-free. `TranscriptGroomingProcessor` moved to `kassette.transcript_grooming_processor`
- the `kassette` entry point dispatches `dictate` to an argparse command and every other command to the typer app, so neither extra imports the other's dependencies

### fixed

- `kassette dictate` reports missing `dictate` dependencies and the install command before checking configuration or importing macOS adapters
- dictation's generated Cmd-V events reach the focused app even when `cmd+v` is the hotkey
- clipboard restoration runs when shutdown interrupts paste settling, without overwriting a newer copy
- global hotkey startup reports failure when setup exits early or takes longer than ten seconds
- resumed AWS cascade streams resolve fresh credentials without blocking the event loop
- initial AWS cascade startup resolves credentials off the event loop before building the provider pipeline
- provider construction uses the configured readiness timeout during startup, switches and rollback, and late adapter results are closed
- runtime cleanup waits for pending provider construction and prevents initial adapters from starting after shutdown
- AWS cascade resume stays paused after a failed reconnect so a later resume can retry
- dictation drains audio callbacks queued before capture stops, preserving the last recorded words
- AWS streaming websocket URLs use the China partition's DNS suffix for `cn-` regions
- the dictate license report respects parentheses and `AND` precedence over `OR`, and rejects malformed expressions

### security

- AWS credentials, presigned stream URLs and transcript text stay out of logs and status output
- dictation keeps audio and transcripts off disk

## 0.2.0

### added

- explicit `kassette serve --hosted` runtime mode for `0.0.0.0:$PORT`
- dedicated constant-time bearer authentication for private hosted routes
- managed STUN/TURN configuration through Pipecat's supported ICE server interface
- concurrent hosted WebRTC sessions with per-session replacement and lease scope
- bounded unauthenticated `/healthz` contract and clean hosted shutdown
- locked non-root container packaging for private Railway deployment
- post-merge GitHub release, Python asset, and GHCR container publishing
- hosted-runtime ADR, environment contract, topology notes, and two-browser validation steps

### preserved

- local mode remains the default and rejects non-loopback binds
- `kassette call`, ClickClack, and `pi-kassette` keep the existing loopback workflow
- local and terminal audio retain the process-wide hardware lease
- provider switching and stale cleanup remain generation-fenced

### security

- hosted startup fails without a dedicated service secret and authenticated TURN relay
- service secrets, authorization headers, TURN credentials, provider keys, SDP, and provider response bodies stay out of argv, startup errors, and lifecycle logs
- Kassette remains private behind Tower and does not add public Railway ingress or operate a TURN server
