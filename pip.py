"""
Pip — self-regulation companion for Esther
Unihiker M10 device
"""

import os
import time
import math
import threading
import json
import base64

import websocket  # websocket-client

# Unihiker / PinPong imports — available on device
try:
    from unihiker import GUI
    from pinpong.board import Board, Pin, NeoPixel
    import pyaudio
    ON_DEVICE = True
except ImportError:
    ON_DEVICE = False
    print("[dev] Running off-device — hardware calls stubbed")

# ── Config ────────────────────────────────────────────────────────────────────

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

NEOPIXEL_PIN   = "P0"   # change to match your wiring
NEOPIXEL_COUNT = 8

RECORD_SECONDS  = 5
SAMPLE_RATE     = 16000
CHANNELS        = 1
CHUNK           = 1024
SILENCE_THRESH  = 500   # RMS below this = silence
SILENCE_SECS    = 1.5   # consecutive silence before early stop

TTS_VOICE = "verse"

SYSTEM_PROMPT = (
    "You are Pip, a tiny magical creature who lives in a special device just for Esther. "
    "You have big feelings too, so you always understand. You are silly and warm — you might "
    "use a little sound effect word (like \"oh whoosh\") but you never make light of what "
    "Esther is feeling. You always validate first, then gently offer one simple thing she can "
    "try. Keep every response to 2-3 sentences maximum. Never sound like a parent or a teacher. "
    "Sound like a tiny best friend who gets it."
)

# ── Colour palette ─────────────────────────────────────────────────────────────

IDLE_COLOR     = (80, 180, 255)   # soft sky blue
LISTEN_COLOR   = (255, 200,  60)  # warm amber
THINK_COLOR    = (160,  80, 255)  # gentle purple
SPEAK_COLOR    = (80,  255, 140)  # mint green

# ── Sprite paths ──────────────────────────────────────────────────────────────

SPRITE_DIR   = os.path.dirname(os.path.abspath(__file__))
SPRITE_IDLE  = os.path.join(SPRITE_DIR, "idle.png")
SPRITE_LISTEN  = os.path.join(SPRITE_DIR, "listening.png")
SPRITE_THINK   = os.path.join(SPRITE_DIR, "thinking.png")
SPRITE_SPEAK   = os.path.join(SPRITE_DIR, "speaking.png")

# ── Hardware helpers ──────────────────────────────────────────────────────────

class Hardware:
    def __init__(self):
        self.gui = None
        self.np  = None
        self._breathing = False
        self._breath_thread = None

        if ON_DEVICE:
            Board("UNIHIKER").begin()
            self.gui = GUI()
            self.np  = NeoPixel(Pin(Pin.P0), NEOPIXEL_COUNT)

    # ── NeoPixel helpers ──

    def set_color(self, color):
        if self.np:
            for i in range(NEOPIXEL_COUNT):
                self.np[i] = color

    def pixels_off(self):
        self.set_color((0, 0, 0))

    def start_breathing(self, color=IDLE_COLOR):
        self._breathing = True
        self._breath_thread = threading.Thread(
            target=self._breath_loop, args=(color,), daemon=True
        )
        self._breath_thread.start()

    def stop_breathing(self):
        self._breathing = False
        if self._breath_thread:
            self._breath_thread.join(timeout=2)
        self.pixels_off()

    def _breath_loop(self, color):
        # 4-count in / 4-count out  (~8 s cycle, ~60 bpm resting)
        period = 8.0
        while self._breathing:
            t = time.time() % period
            brightness = (math.sin(math.pi * t / period)) ** 2
            r = int(color[0] * brightness)
            g = int(color[1] * brightness)
            b = int(color[2] * brightness)
            self.set_color((r, g, b))
            time.sleep(0.05)
        self.pixels_off()

    def pulse_once(self, color, duration=0.6):
        steps = 20
        for i in range(steps):
            b = math.sin(math.pi * i / steps)
            self.set_color((int(color[0]*b), int(color[1]*b), int(color[2]*b)))
            time.sleep(duration / steps)
        self.pixels_off()

    # ── Screen helpers ──

    def _show_sprite(self, sprite_path, label):
        if not self.gui:
            print(f"[screen] {label}")
            return
        self.gui.clear()
        self.gui.draw_image(x=120, y=120, w=200, h=200,
                            image=sprite_path, origin="center")
        self.gui.draw_text(x=120, y=230, text=label,
                           font_size=15, color="#ffffff", origin="center")

    def show_idle(self):
        self._show_sprite(SPRITE_IDLE, "I'm here ♥")

    def show_listening(self):
        self._show_sprite(SPRITE_LISTEN, "I'm listening…")

    def show_thinking(self):
        self._show_sprite(SPRITE_THINK, "hmm…")

    def show_speaking(self):
        self._show_sprite(SPRITE_SPEAK, "talking to you…")

    def choose_profile(self, options):
        """Show a row of soft on-screen buttons; block until one is tapped."""
        if not self.gui:
            print(f"[screen] Who's there? {options}")
            choice = input(f"[dev] Type a name {options} (or press ENTER for default): ").strip()
            return choice if choice in options else options[0]

        chosen = {"name": None}

        def make_handler(name):
            def handler():
                chosen["name"] = name
            return handler

        self.gui.clear()
        self.gui.draw_text(x=120, y=60, text="Who's with you?",
                           font_size=18, color="#ffffff", origin="center")

        spacing = 240 // (len(options) + 1)
        for i, name in enumerate(options, start=1):
            self.gui.add_button(
                x=spacing * i, y=140, w=spacing - 20, h=60,
                text=name, origin="center",
                onclick=make_handler(name),
            )

        while chosen["name"] is None:
            time.sleep(0.05)

        return chosen["name"]

    # ── Button ──

    def wait_for_button(self):
        if not ON_DEVICE:
            input("[dev] Press ENTER to simulate button press…")
            return
        btn = Pin(Pin.P23, Pin.IN)
        while btn.read_digital() == 1:
            time.sleep(0.05)


# ── Audio helpers ─────────────────────────────────────────────────────────────
#
# The realtime model wants raw PCM16 chunks pushed to it as they're captured,
# and hands speech back the same way — so we stream both directions instead of
# recording/playing whole files.

_playback_stream = None
_playback_pa     = None


def stream_microphone(session, duration=RECORD_SECONDS):
    """Capture mic audio and push PCM16 chunks straight into the session."""
    if not ON_DEVICE:
        print("[dev] Skipping real recording — sending silent stub chunk")
        session.send_audio_chunk(b"\x00\x00" * CHUNK)
        return

    pa     = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )

    silence_time = 0.0
    start        = time.time()

    while True:
        data = stream.read(CHUNK, exception_on_overflow=False)
        session.send_audio_chunk(data)

        rms = _rms(data)
        dt  = CHUNK / SAMPLE_RATE
        if rms < SILENCE_THRESH:
            silence_time += dt
        else:
            silence_time = 0.0

        elapsed = time.time() - start
        if elapsed >= duration:
            break
        if silence_time >= SILENCE_SECS and elapsed > 1.0:
            break

    stream.stop_stream()
    stream.close()
    pa.terminate()


def _rms(data: bytes) -> float:
    import struct
    count  = len(data) // 2
    shorts = struct.unpack(f"{count}h", data)
    if not shorts:
        return 0.0
    return math.sqrt(sum(s * s for s in shorts) / count)


def play_audio_chunk(pcm16_bytes: bytes):
    """Play a streamed PCM16 chunk as it arrives, keeping one open output stream."""
    global _playback_stream, _playback_pa

    if not ON_DEVICE:
        return

    if _playback_pa is None:
        _playback_pa = pyaudio.PyAudio()
    if _playback_stream is None:
        _playback_stream = _playback_pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
        )

    _playback_stream.write(pcm16_bytes)


def close_playback():
    global _playback_stream, _playback_pa

    if _playback_stream is not None:
        _playback_stream.stop_stream()
        _playback_stream.close()
        _playback_stream = None
    if _playback_pa is not None:
        _playback_pa.terminate()
        _playback_pa = None


# ── Realtime pipeline ─────────────────────────────────────────────────────────
#
# Instead of chaining Whisper STT → Claude → TTS (three sequential network
# round-trips per turn), we use OpenAI's realtime speech-to-speech model over
# a single streaming websocket session. Pip's persona now lives entirely in
# REALTIME_INSTRUCTIONS since the realtime model both "thinks" and "speaks".

REALTIME_MODEL = "gpt-realtime"
REALTIME_URL   = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"

REALTIME_BASE_INSTRUCTIONS = SYSTEM_PROMPT + (
    " Speak in a warm, gentle, slightly playful voice — like a tiny best friend, "
    "never like a parent or teacher."
)

# ── Profiles ──────────────────────────────────────────────────────────────────
#
# Esther taps a screen button to say who's with her before she talks to Pip.
# This lets Pip adjust its tone without ever needing to ask "who's there?" —
# one less bit of friction between Esther and feeling heard.

PROFILES = {
    "Esther": (
        " You're talking directly with Esther. Speak right to her, warmly and "
        "personally, like you've known her forever."
    ),
    "Miriam": (
        " You're talking with Miriam, Esther's grown-up. Stay just as warm, but "
        "you can speak a little more plainly — no need to over-explain feelings "
        "the way you would for Esther."
    ),
    "Friend": (
        " You're talking with one of Esther's friends. Be extra welcoming and a "
        "little more playful and curious — help them feel like part of Esther's "
        "world too."
    ),
}

DEFAULT_PROFILE = "Esther"

# Lower temperature keeps Pip's tone gentle and consistent rather than wild;
# the output token cap keeps replies short so Esther isn't overwhelmed.
REALTIME_TEMPERATURE = 0.7
REALTIME_MAX_OUTPUT_TOKENS = 200

# Tuned so Pip waits for a real pause (kids often pause mid-thought) before
# deciding Esther is done talking.
REALTIME_TURN_DETECTION = {
    "type": "server_vad",
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 700,
}


def instructions_for_profile(profile: str) -> str:
    return REALTIME_BASE_INSTRUCTIONS + PROFILES.get(profile, PROFILES[DEFAULT_PROFILE])


class RealtimeSession:
    """One persistent websocket connection to the realtime model.

    Streams mic audio in, streams speech audio out — no separate STT/TTS hops.
    """

    def __init__(self, on_audio_chunk, on_state_change, profile=DEFAULT_PROFILE):
        self._on_audio_chunk  = on_audio_chunk
        self._on_state_change = on_state_change
        self._profile = profile
        self._ws = websocket.create_connection(
            REALTIME_URL,
            header=[
                f"Authorization: Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta: realtime=v1",
            ],
        )
        self._configure_session()

    def _send(self, event: dict):
        self._ws.send(json.dumps(event))

    def _configure_session(self):
        self._send({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": instructions_for_profile(self._profile),
                "voice": TTS_VOICE,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": REALTIME_TURN_DETECTION,
                "temperature": REALTIME_TEMPERATURE,
                "max_response_output_tokens": REALTIME_MAX_OUTPUT_TOKENS,
            },
        })

    def send_audio_chunk(self, pcm16_bytes: bytes):
        self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16_bytes).decode("ascii"),
        })

    def commit_and_respond(self):
        self._send({"type": "input_audio_buffer.commit"})
        self._send({"type": "response.create"})

    def pump_until_response_done(self):
        """Read events until the model finishes speaking, dispatching callbacks."""
        while True:
            event = json.loads(self._ws.recv())
            etype = event.get("type", "")

            if etype == "response.audio.delta":
                self._on_state_change("speaking")
                self._on_audio_chunk(base64.b64decode(event["delta"]))
            elif etype == "response.audio_transcript.delta":
                print(f"[pip]   {event.get('delta', '')}", end="", flush=True)
            elif etype == "response.done":
                print()
                return
            elif etype == "error":
                raise RuntimeError(event.get("error", event))

    def close(self):
        self._ws.close()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    hw = Hardware()

    print("Pip is starting up…")
    hw.show_idle()
    hw.start_breathing(IDLE_COLOR)

    while True:
        # ── who's there? — soft button picker instead of Pip introducing itself ──
        hw.stop_breathing()
        profile = hw.choose_profile(list(PROFILES.keys()))
        print(f"[profile] {profile}")
        hw.show_idle()
        hw.start_breathing(IDLE_COLOR)

        # ── wait for button ──────────────────────────────────────
        hw.wait_for_button()

        try:
            session = RealtimeSession(
                on_audio_chunk=play_audio_chunk,
                on_state_change=lambda s: None,
                profile=profile,
            )

            # ── listening — stream mic straight into the session ──
            hw.stop_breathing()
            hw.show_listening()
            hw.start_breathing(LISTEN_COLOR)

            stream_microphone(session, duration=RECORD_SECONDS)
            session.commit_and_respond()

            hw.stop_breathing()

            # ── thinking → speaking — handled by streamed callbacks ──
            hw.show_thinking()
            hw.pulse_once(THINK_COLOR, duration=0.3)

            hw.show_speaking()
            hw.start_breathing(SPEAK_COLOR)
            session.pump_until_response_done()
            hw.stop_breathing()
            close_playback()

            session.close()

        except Exception as exc:
            print(f"[error] {exc}")
            try:
                fallback_session = RealtimeSession(
                    on_audio_chunk=play_audio_chunk,
                    on_state_change=lambda s: None,
                )
                hw.show_speaking()
                fallback_session._send({
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio"],
                        "instructions": (
                            "Say, gently and warmly: Oh whoosh — I had a little "
                            "hiccup. I'm still here though, I promise."
                        ),
                    },
                })
                fallback_session.pump_until_response_done()
                close_playback()
                fallback_session.close()
            except Exception:
                pass

        # ── back to idle ─────────────────────────────────────────
        hw.show_idle()
        hw.start_breathing(IDLE_COLOR)


if __name__ == "__main__":
    main()
