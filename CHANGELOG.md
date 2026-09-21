# changelog

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
