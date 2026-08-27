# Keep the audio path inside Pipecat

kassette will implement its first GPT-Live adapter inside the Python Pipecat service rather than wrap the existing TypeScript client in a Node sidecar.

Reusing the TypeScript client would avoid porting an undocumented protocol. Its native binding captures and plays audio outside Pipecat, though, so it would not test the media path kassette is meant to own.

The Python Quicksilver adapter is isolated behind the provider boundary. This costs more upfront and depends on an unstable protocol, but it lets Pipecat carry audio, interruption, and session lifecycle from the first working loop.
