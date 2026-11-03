# macos toggle dictation

`kassette dictate` is a local, single-user dictation mode for macos. press the hotkey once to start recording and again to stop. kassette streams the microphone to AWS Transcribe while you speak. when you stop, it ends the stream, waits for the final transcript, runs it through your transcript grooming profile and pastes it at the cursor in the focused app.

dictation runs in its own foreground process. it doesn't start the webrtc service, and it doesn't use a voice session or a provider other than `aws`.

## prerequisites

kassette needs PortAudio before the python dependencies install:

```bash
brew install portaudio
uv sync --locked
```

dictation needs AWS Transcribe streaming, so set `KASSETTE_TRANSCRIPTION_PROVIDER=aws`. gemini and openai don't end a stream with a definitive final transcript, so `dictate` rejects them.

## aws setup

use a personal AWS account. kassette reads credentials from the standard AWS chain, so any of these work:

- a named profile: `AWS_PROFILE=personal`, set up with `aws configure --profile personal` or `aws configure sso --profile personal`. run `aws sso login --profile personal` when an SSO session expires.
- environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and optionally `AWS_SESSION_TOKEN`.

the identity needs `transcribe:StartStreamTranscriptionWebSocket`. kassette never logs credentials or the presigned stream URL.

the region comes from `KASSETTE_AWS_REGION`, then `AWS_REGION`, then `AWS_DEFAULT_REGION` or the profile's region, then `us-east-1`.

two optional AWS settings improve the raw transcript before grooming:

- `KASSETTE_AWS_VOCABULARY_NAME` names a custom vocabulary in the same region. use it for product names and other words AWS mishears.
- `KASSETTE_AWS_VOCABULARY_FILTER_NAME` names a vocabulary filter. kassette applies it with the `remove` method, so filtered words such as "um" and "uh" are dropped from the transcript.

create both in the AWS console under Amazon Transcribe, or with `aws transcribe create-vocabulary` and `aws transcribe create-vocabulary-filter`. a vocabulary must finish building (state `READY`) before a stream can use it.

## macos permissions

macos attributes dictation's permissions to the terminal app that runs kassette, such as Terminal, iTerm2 or Ghostty.

- **Microphone.** kassette checks microphone access at startup. the first run shows the system prompt. if access is denied, kassette exits with an error. turn it on under System Settings → Privacy & Security → Microphone.
- **Accessibility.** the global hotkey and the Cmd-V paste both need Accessibility access. if it's missing, kassette shows the system prompt and exits with an error. turn it on under System Settings → Privacy & Security → Accessibility, then run `kassette dictate` again. if the hotkey still doesn't fire, also allow the terminal under Input Monitoring.

restart the terminal app after you change either permission. macos applies some changes only to new processes.

## run it

```bash
KASSETTE_TRANSCRIPTION_PROVIDER=aws AWS_PROFILE=personal uv run kassette dictate
```

the default hotkey is `ctrl+option+d`. change it with `KASSETTE_DICTATE_HOTKEY` or `--hotkey`. a hotkey is one key plus modifiers joined with `+`, for example `cmd+shift+space` or `option+f13`. the modifiers are `ctrl`, `option`, `shift` and `cmd`. function keys `f1` to `f20` may be used alone. kassette swallows the hotkey so the focused app doesn't receive it.

status messages go to stderr: `recording`, `finalizing`, and then the character count and the time from stop to paste. they never include transcript text. press ctrl-c to quit.

a toggle while kassette is still finalizing queues a new recording, which starts right after the paste. another toggle cancels the queued recording. both are reported on stderr.

## paste behavior

kassette saves the current clipboard, writes the transcript, sends Cmd-V to the focused app and then restores the saved clipboard, including images and other non-text types. if you copy something else during the paste, kassette leaves your new copy alone.

if Accessibility is revoked while kassette runs, or the keystroke can't be sent, the transcript stays on the clipboard and kassette says so on stderr. paste it by hand.

audio and transcripts are never written to disk. kassette keeps the transcript in memory only until it's pasted.

## grooming profile for dictation

grooming uses the same versioned profile as the cascade. see [adr 0003](adr/0003-groom-transcripts-at-a-provider-neutral-seam.md). keep personal profiles outside the repository, for example at `~/.config/kassette/dictation-grooming.json`:

```json
{
  "version": 2,
  "lowercase": false,
  "preserve_pronoun_i": true,
  "collapse_whitespace": true,
  "word_overrides": {
    "Jeff": "jepp"
  },
  "filter_filler_words": true,
  "filler_words": ["um", "uh", "erm", "you know"],
  "symbol_replacements": false
}
```

then point kassette at it:

```bash
KASSETTE_TRANSCRIPT_GROOMING_PROFILE=$HOME/.config/kassette/dictation-grooming.json
```

word overrides match whole words without regard to case. only override words that AWS consistently mishears for you, because an override also rewrites the word when you actually say it. set `symbol_replacements` to `true` if you want spoken commands such as "new line" turned into symbols.

grooming runs once on the joined final transcript. if it fails or times out (`KASSETTE_TRANSCRIPT_GROOMING_TIMEOUT_SECS`), kassette pastes the provider transcript unchanged.
