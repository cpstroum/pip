# Nemma — Build Learnings

## Architecture: why we use the OpenAI Realtime API

The original design used a three-hop pipeline: Whisper STT → Claude LLM → OpenAI TTS.
Each turn required three sequential network round-trips, making it too slow for a child
in distress. We switched to `gpt-realtime-2` via a single WebSocket session — mic audio
streams in as PCM16, speech audio streams back the same way, with no separate STT/TTS hops.

**Tradeoff:** Nemma's personality now lives entirely in the session instructions rather
than in Claude, and per-turn cost is higher (~$0.02–0.03/turn vs fractions of a cent).
For occasional use by a child, this is acceptable.

## OpenAI Realtime API — GA vs Beta differences

The GA API (`/v1/realtime`) differs significantly from the beta:

| Thing | Beta | GA (`gpt-realtime-2`) |
|---|---|---|
| Model | `gpt-4o-realtime-preview` | `gpt-realtime-2` |
| Auth header | `OpenAI-Beta: realtime=v1` | **Remove this header** |
| Audio event | `response.audio.delta` | `response.output_audio.delta` |
| Transcript event | `response.audio_transcript.delta` | `response.output_audio_transcript.delta` |
| Session format | Flat (`input_audio_format`, `voice`, etc.) | Nested under `session.audio.input/output` |
| Session type field | Not required | `"type": "realtime"` required |
| `response.create` | Accepts `modalities` | **`modalities` is unknown parameter — remove it** |
| Input rate | 16kHz typical | 24kHz (`audio/pcm` format) |
| Output rate | 16kHz | 24kHz |

## Audio output on the Unihiker

PyAudio output was silently routing to the wrong device. Switching to `aplay` subprocess
(piping raw PCM16 to stdin) reliably drives the Unihiker speaker via ALSA:

```python
_aplay_proc = subprocess.Popen(
    ["aplay", "-r", "24000", "-f", "S16_LE", "-c", "1", "-"],
    stdin=subprocess.PIPE,
)
_aplay_proc.stdin.write(pcm16_bytes)
```

## Server VAD concurrency

With server VAD enabled, the model auto-commits the audio buffer and starts generating
a response while the mic is still streaming. Running `stream_microphone` sequentially
before `pump_until_response_done` meant we missed the audio delta events entirely
(response showed 0 bytes). Fix: run both in parallel threads.

## GitHub App vs OAuth

Claude Code needs to be installed as a **GitHub App** (via github.com/apps/claude),
not just authorized as an OAuth app, to have write access for creating PRs.
The OAuth connector in Claude settings is for chat/projects only.
