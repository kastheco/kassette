# Run kassette as a standalone service

kassette will run as one local service instead of being embedded separately in ClickClack, Pi, KASHH, or each future client. Embedding a library would make the first integration smaller, but every host would end up with its own live-session lifecycle and audio stack.

The standalone boundary keeps the voice infrastructure replaceable. Each product still owns its durable conversation and target semantics.
