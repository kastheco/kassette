# Pi Kassette

## What we're building

Pi stays text-first. A shortcut or `/kassette` swaps the normal editor for a voice surface. The surface shows whether Kassette can hear you, streams the transcript as you speak, sends finished text to Pi, reads Pi's answer aloud, and lets you interrupt naturally.

This first version is for Kas's Linux terminal. It should be clean enough to share later, but it is still a personal tool. Build the parts needed to make it work well. Don't add remote access, accounts, policy systems, or abstractions for clients that don't exist.

## How it should feel

Entering the voice surface connects to Kassette and starts with the mic paused. Leaving ends the voice session and puts any finished, unsent transcript back into Pi's normal editor.

The surface uses about the same space as Pi's editor. It always shows:

- a waveform driven by real mic or playback levels
- the current state: connecting, mic paused, listening, transcribing, thinking, speaking, interrupted, reconnecting, or failed
- the latest user transcript
- a short live caption while Pi answers
- the keys that currently do something

The waveform can't be fake. If level data stops, show that instead of animating something synthetic. Text carries every important state, so the UI still works without color or sound. On a narrow terminal, drop the waveform before dropping the state or transcript.

### Keys

Both provider modes use:

- configured shortcut or `/kassette`: enter or leave
- `Space`: pause or resume the mic
- `A`: toggle auto-send
- `Enter`: send the current transcript or native delegation
- `M`: mute or unmute response audio
- Pi's cancel key: stop speech and interrupt the active Pi turn
- `Escape`: end voice and return to the normal editor

Cascade mode also uses `Backspace` to remove the last finished utterance.

The surface keeps auto-send state visible in both provider modes. In cascade mode, a pause in speech finishes an utterance without sending it unless auto-send is on, and finished utterances build one draft until they are sent or removed. In Quicksilver mode, a native delegation sends automatically when auto-send is on. With manual send enabled, its recognized request stays visible until `Enter` sends it to Pi.

Speaking while Pi is talking is a barge-in. Kassette stops current and queued audio immediately, then the finished utterance steers the running Pi turn. Speech that finishes while Pi is busy without a detected barge-in stays as a draft.

In cascade mode, send one complete Pi answer to TTS so sentence boundaries keep natural pacing. In Quicksilver mode, return the complete Pi answer through the matching native delegation and let Quicksilver render it as speech. Don't read tool payloads, reasoning, empty text, or raw Markdown noise. Keep Pi's saved assistant message intact after an interruption even if the user didn't hear all of it.

## The boundary

Kassette owns the mic, speaker, audio lease, transcription, and playback. The extension owns Pi integration and the terminal UI. The extension does not become an OS audio client and does not open a companion browser.

Add a loopback terminal-session API and a bidirectional control channel to the Kassette service. Reuse the existing `{label, type, data}` message shape and session events. Add what terminal clients need: session open and close, version and capability negotiation, input and output audio levels, input pause, output mute, playback cancellation, and queued speech.

Each session gets a short-lived random token. The extension and service check protocol versions and required capabilities before taking the audio lease. If the running service is incompatible, show the mismatch. Don't start a second service and let both fight over the same devices.

Pi voice sessions may use cascade or Quicksilver. Pi owns reasoning, tools, and durable history in both modes. Cascade sends transcripts to Pi and speaks Pi's completed text through Fish. Quicksilver delegates each request to Pi, receives the matching Pi answer as bounded client context, and renders the speech natively. Quicksilver's built-in reasoning stays disabled in Pi mode.

## The extension

Add `packages/pi-kassette/` as an ESM TypeScript Pi package with its own tests and typecheck. Keep lifecycle registration, protocol parsing, connection handling, Pi turn behavior, state, and UI in separate small modules where that separation pays for itself.

The extension first tries the configured loopback URL. If nothing is running, it starts the configured command, defaulting to `kassette serve`, and waits for readiness. It only kills a process it started.

Pi session changes such as `/new`, `/resume`, `/fork`, and `/reload` end the current voice session. A dropped control connection gets a short visible retry window. Old session IDs, connection attempts, provider generations, and event sequences can't affect a replacement connection. Reconnect never resends a draft or replays old speech.

Configuration covers the activation shortcut, loopback URL, launch command, input and output device IDs, reconnect window, auto-send default, and output-mute default. Provider choice, VAD tuning, visual style, and voice choice stay in Kassette.

Logs don't contain transcript or response text unless an explicit diagnostic mode is enabled. IDs, state changes, timings, byte counts, and cleaned errors are enough for normal debugging.

## Test seams

Use red-green TDD around the boundaries we agreed on:

1. protocol parsing, limits, versions, and capabilities
2. terminal-session and audio-lease lifecycle
3. transcript ordering, deduplication, undo, and editor handoff
4. idle, busy, auto-send, and barge-in behavior
5. complete-response buffering and speech filtering
6. Quicksilver delegation request and response pairing
7. voice-surface state changes and provider-specific keys
8. process ownership, reconnect fencing, and cleanup

Run focused tests and typechecks while each slice is being built. Run both full suites at the end. Test semantic UI output instead of freezing whole ANSI frames.

## Done means usable

The automated checks need to pass, but this isn't done until the real path works:

1. switch from Pi's editor into voice
2. see real mic levels move
3. watch interim text become a finished draft
4. send it as a real Pi message
5. see and hear Pi's response through the selected provider
6. speak over Pi and stop both playback and the active turn
7. pause and resume with `Space`
8. toggle auto-send with `A`
9. return to the normal editor without losing an unsent draft
10. force one control-channel disconnect without duplicate text or speech

If the current environment can't run the physical audio check, record exactly what remains. Don't call the feature fully accepted until somebody runs it.

## Not in this version

No macOS or Windows support, browser companion, remote Kassette connection, device picker, arbitrary editing inside the voice surface, direct Quicksilver reasoning inside Pi, Orca bridge, assistant-message rewriting, or npm release.
