# kassette

kassette provides one local boundary for realtime voice sessions used by multiple products and agent runtimes. It owns the live interaction, not the durable product conversation.

## Language

**kassette service**  
The long-running local service through which voice clients create and control voice sessions.  
_Avoid_: voice daemon, Pipecat service, gateway process

**Voice session**  
One identified live interaction owned by the kassette service. It is transient and ends without becoming durable conversation history.  
_Avoid_: call, connection, conversation

**Native voice session**  
A voice session where one provider handles speech input, reasoning, and speech output.  
_Avoid_: end-to-end mode, speech-to-speech pipeline

**Cascaded voice session**  
A voice session where transcription, agent reasoning, and speech rendering remain separate stages.  
_Avoid_: traditional mode, fallback mode

**Voice client**  
A program that requests and controls voice sessions through the kassette service.  
_Avoid_: consumer, frontend, integration

**Provider adapter**  
kassette's boundary around one provider-specific voice protocol and its event semantics.  
_Avoid_: provider wrapper, driver

**Audio lease**  
Exclusive ownership of a local microphone and speaker path by one voice session.  
_Avoid_: audio lock, device reservation

**Session event**  
A normalized notification about voice-session lifecycle, transcript, speech, interruption, or failure.  
_Avoid_: frame, provider event, message
