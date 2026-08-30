# Keep terminal audio in the kassette service

For terminal voice clients, the kassette service captures microphone input and plays response audio while clients connect through a loopback control and event channel. We rejected making each client implement OS audio or requiring a companion browser because kassette already owns the audio lease and Pipecat audio path; keeping that boundary gives Pi and future terminal clients one transport-neutral voice-session contract.
