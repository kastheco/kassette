# separate hosted runtime auth and session leases

## status

accepted for kassette 0.2.0.

## context

kassette began as a loopback service with one process-wide audio lease. that invariant protects one machine's microphone and speaker from competing local and terminal sessions. it is wrong for hosted WebRTC, where each browser session has an independent remote media path and distinct operators must run concurrently.

Tower will expose the product surface. Tower authenticates users on its HTTPS origin and proxies signaling to Kassette over Railway private networking. the browser must not receive Kassette's service credential. Railway does not give the container a stable public ICE address, so WebRTC media needs managed TURN.

## decision

kassette has two explicit runtime modes.

local mode remains the default. it permits loopback binds only, keeps the process-scoped audio lease, and preserves the current ClickClack and `pi-kassette` flow.

hosted mode requires `kassette serve --hosted`. it binds to `0.0.0.0` by default, reads `PORT`, and fails before startup unless both `KASSETTE_SERVICE_SECRET` and a usable `KASSETTE_ICE_SERVERS` relay configuration are present. hosted authentication uses `Authorization: Bearer <secret>` with constant-time comparison. every route is protected except the bounded `GET /healthz` probe. provider keys are never valid service credentials.

hosted WebRTC uses a session-scoped lease and replacement slot. distinct logical session IDs own independent leases and run concurrently. a reconnect with the same ID creates a newer generation and replaces only that ID's prior generation. registry transitions, release, reap, and coordinator cleanup compare generations, so stale work cannot close or release a replacement.

local and terminal audio keep the process-scoped policy. the policy is modeled rather than inferred from transport behavior.

Pipecat's supported ICE server configuration is the single ICE seam. Kassette validates a bounded JSON configuration with at least one authenticated `turn:` or `turns:` relay, then supplies it through `PIPECAT_ICE_SERVERS`. the expected Railway shape is managed `turns:` over port 443. Kassette does not expose a public Railway domain and does not run a TURN server.

## consequences

Tower remains responsible for user authentication, authorization, HTTPS, and adding the service bearer secret. Kassette authenticates Tower and owns only transient voice-session state.

TURN credentials must reach the authorized WebRTC client because the browser uses them for relay allocation. they must not reach logs, argv, errors, diagnostics, or repository files. the Kassette service secret never reaches the browser.

shutdown closes every current session generation before Pipecat closes its WebRTC connection handler. the container runs as a non-root user and uses `/healthz` for Railway health checks.

hosted mode does not implement Tower's proxy, browser UI, Jarvis handoff, Railway infrastructure, public ingress, or TURN operation. those remain outside this repository.
