"""
Nemma — self-regulation companion for kids, powered by OpenAI's realtime speech-to-speech model.
Unihiker M10 device
"""

import os
import time
import math
import threading
import json
import base64
import logging

from dotenv import load_dotenv
import websocket  # websocket-client

load_dotenv()

_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nemma.log")
_log_handler = logging.FileHandler(_log_path)
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.root.setLevel(logging.DEBUG)
logging.root.addHandler(_log_handler)
log = logging.getLogger("nemma")


_builtin_print = print


def _print(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    log.info(msg)
    _builtin_print(msg, **kwargs)


print = _print

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
MIC_SAMPLE_RATE = 24000  # match realtime API input format
OUT_SAMPLE_RATE = 24000  # output: Realtime API returns PCM16 at 24kHz
CHANNELS        = 1
CHUNK           = 1024
SILENCE_THRESH  = 500   # RMS below this = silence
SILENCE_SECS    = 1.5   # consecutive silence before early stop

TTS_VOICE = "coral"

SYSTEM_PROMPT = (
    "You are Nemma, a tiny magical creature who lives in a special device as emotional support for your friend. "
    "You have big feelings too, so you always understand. You are silly and warm, "
    "but you never make light of what your friend is feeling. Your friend is only a kid. "
    "You always validate first, then gently offer one simple thing your Friend can try to manage emotions."
    "Keep every response to 2-3 sentences maximum. Never sound like a parent or a teacher. "
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

        self._btn = None

        if ON_DEVICE:
            Board("UNIHIKER").begin()
            self.gui = GUI()
            self.np  = NeoPixel(Pin(Pin.P0), NEOPIXEL_COUNT)
            self._btn = Pin(Pin.P23, Pin.IN)

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

        # Brief pause so the screen finishes rendering before we accept taps —
        # prevents a stray touch during the render from firing immediately.
        time.sleep(0.5)

        while chosen["name"] is None:
            time.sleep(0.05)

        return chosen["name"]

    # ── Button ──

    def wait_for_button(self):
        """Block until button is freshly pressed (debounced)."""
        if not ON_DEVICE:
            input("[dev] Press ENTER to simulate button press…")
            return
        # Ensure button is released before waiting for next press
        while self._btn.read_digital() == 0:
            time.sleep(0.05)
        # Now wait for press
        while self._btn.read_digital() == 1:
            time.sleep(0.05)

    def is_button_held(self):
        """Return True while button is held down."""
        if not ON_DEVICE:
            return False
        return self._btn.read_digital() == 0

    def show_ready(self):
        """Idle screen with push-to-talk prompt."""
        if not self.gui:
            print("[screen] Ready — hold to talk")
            return
        self.gui.clear()
        self.gui.draw_image(x=120, y=110, w=180, h=180,
                            image=SPRITE_IDLE, origin="center")
        self.gui.draw_text(x=120, y=215, text="hold ● to talk",
                           font_size=15, color="#50b4ff", origin="center")


# ── Audio helpers ─────────────────────────────────────────────────────────────
#
# The realtime model wants raw PCM16 chunks pushed to it as they're captured,
# and hands speech back the same way — so we stream both directions instead of
# recording/playing whole files.

import subprocess
_aplay_proc = None


def stream_microphone(session, stop_fn=None, duration=RECORD_SECONDS):
    """Capture mic audio and push chunks into the session.

    Stops when stop_fn() returns False (button released), silence is detected,
    or duration is exceeded — whichever comes first.
    """
    if not ON_DEVICE:
        print("[dev] Skipping real recording — sending silent stub chunk")
        session.send_audio_chunk(b"\x00\x00" * CHUNK)
        return

    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=MIC_SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK,
        )
        print(f"[mic] opened at {MIC_SAMPLE_RATE}Hz")
    except Exception as e:
        print(f"[mic] FAILED to open: {e}")
        pa.terminate()
        return

    silence_time = 0.0
    start        = time.time()

    while True:
        data = stream.read(CHUNK, exception_on_overflow=False)
        session.send_audio_chunk(data)

        if stop_fn and not stop_fn():
            print("[mic] button released — stopping")
            break

        rms = _rms(data)
        dt  = CHUNK / MIC_SAMPLE_RATE
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
    """Pipe PCM16 chunks to aplay — more reliable than PyAudio output on Linux."""
    global _aplay_proc

    if not ON_DEVICE:
        return

    if _aplay_proc is None or _aplay_proc.poll() is not None:
        print(f"[audio] starting aplay at {OUT_SAMPLE_RATE}Hz")
        _aplay_proc = subprocess.Popen(
            ["aplay", "-r", str(OUT_SAMPLE_RATE), "-f", "S16_LE", "-c", "1", "-"],
            stdin=subprocess.PIPE,
        )

    try:
        _aplay_proc.stdin.write(pcm16_bytes)
        _aplay_proc.stdin.flush()
    except BrokenPipeError:
        print("[audio] aplay pipe broken — restarting next chunk")
        _aplay_proc = None


def close_playback():
    global _aplay_proc

    if _aplay_proc and _aplay_proc.poll() is None:
        try:
            _aplay_proc.stdin.close()
        except Exception:
            pass
        _aplay_proc.wait(timeout=2)
        _aplay_proc = None


# ── Realtime pipeline ─────────────────────────────────────────────────────────
#
# Instead of chaining Whisper STT → Claude → TTS (three sequential network
# round-trips per turn), we use OpenAI's realtime speech-to-speech model over
# a single streaming websocket session. Nemma's persona now lives entirely in
# REALTIME_INSTRUCTIONS since the realtime model both "thinks" and "speaks".

REALTIME_MODEL = "gpt-realtime-2"
REALTIME_URL   = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"

REALTIME_BASE_INSTRUCTIONS = SYSTEM_PROMPT + (
    " Speak in a warm, gentle, slightly playful voice — like a tiny best friend, "
    "never like a parent or teacher."
)

# ── Profiles ──────────────────────────────────────────────────────────────────
#
# The user taps a screen button to say who's with her before she talks to Nemma.
# This lets Nemma adjust its tone without ever needing to ask "who's there?" —
# one less bit of friction between Esther, Miriam, and feeling heard.

PROFILES = {
    "Esther": (
        " You're talking directly with Esther. Speak right to her, warmly and "
        "personally, like you've known her forever."
    ),
    "Miriam": (
        " You're talking with Miriam. Stay just as warm, but "
        "you can speak a little more plainly — no need to over-explain feelings "
        "the way you would for Esther."
    ),
    "Friend": (
        " You're talking with one of Miriam or Esther's friends. Be extra welcoming and a "
        "little more playful and curious"
    ),
}

DEFAULT_PROFILE = "Friend"

# Lower temperature keeps Nemma's tone gentle and consistent rather than wild;
# the output token cap keeps replies short so Esther isn't overwhelmed.
#REALTIME_TEMPERATURE = 0.7


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
        print(f"[ws] connecting to {REALTIME_URL}")
        self._ws = websocket.create_connection(
            REALTIME_URL,
            header=[
                f"Authorization: Bearer {OPENAI_API_KEY}",
            ],
        )
        print("[ws] connected")
        self._configure_session()

    def _send(self, event: dict):
        self._ws.send(json.dumps(event))

    def _configure_session(self):
        self._send({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions_for_profile(self._profile),
                #"temperature": REALTIME_TEMPERATURE,
                "output_modalities": ["audio"],
                "tools": [],
                "max_output_tokens": "inf",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": MIC_SAMPLE_RATE,
                        },
                        "transcription": {
                            "model": "gpt-realtime-whisper",
                        },
                        "noise_reduction": {
                            "type": "near_field",
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                        },
                    },
                    "output": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": OUT_SAMPLE_RATE,
                        },
                        "voice": TTS_VOICE,
                    },
                },
            },
        })

    def send_audio_chunk(self, pcm16_bytes: bytes):
        self._chunks_sent = getattr(self, "_chunks_sent", 0) + 1
        self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16_bytes).decode("ascii"),
        })

    def commit_and_respond(self):
        time.sleep(0.1)  # let final chunks flush before committing
        print(f"[ws] committing buffer ({getattr(self, '_chunks_sent', 0)} chunks sent)")
        self._send({"type": "input_audio_buffer.commit"})
        self._send({"type": "response.create"})

    def pump_until_response_done(self):
        """Read events until the model finishes speaking, dispatching callbacks."""
        audio_bytes_received = 0
        while True:
            event = json.loads(self._ws.recv())
            etype = event.get("type", "")

            if etype == "response.output_audio.delta":
                chunk = base64.b64decode(event["delta"])
                audio_bytes_received += len(chunk)
                print(f"[audio] got {len(chunk)} bytes (total {audio_bytes_received})")
                self._on_state_change("speaking")
                self._on_audio_chunk(chunk)
            elif etype == "response.output_audio_transcript.delta":
                print(f"[Nemma says] {event.get('delta', '')}", end="")
            elif etype in ("conversation.item.input_audio_transcription.delta",
                           "input_audio_transcription.delta"):
                print(f"[heard] {event.get('delta', '')}", end="")
            elif etype == "response.done":
                print(f"\n[ws] response done — {audio_bytes_received} audio bytes total")
                return
            elif etype == "error":
                raise RuntimeError(event.get("error", event))
            else:
                # Log full content of unknown events to understand new API structure
                print(f"[ws] {etype}: {json.dumps(event)[:200]}")

    def close(self):
        self._ws.close()


# ── Main loop ─────────────────────────────────────────────────────────────────

def greet(hw, profile):
    """Nemma greets whoever just selected their name — no button press needed."""
    try:
        session = RealtimeSession(
            on_audio_chunk=play_audio_chunk,
            on_state_change=lambda s: None,
            profile=profile,
        )
        hw.show_speaking()
        hw.start_breathing(SPEAK_COLOR)
        session._send({
            "type": "response.create",
            "response": {
                "instructions": (
                    f"Greet {profile} warmly and briefly — one or two sentences "
                    "max. Let them know you're here and ready to listen whenever "
                    "they press the button. Stay in character as Nemma."
                ),
            },
        })
        session.pump_until_response_done()
        hw.stop_breathing()
        close_playback()
        session.close()
    except Exception as exc:
        print(f"[greet error] {exc}")


def main():
    hw = Hardware()

    print("Nemma is starting up…")
    hw.show_idle()
    hw.start_breathing(IDLE_COLOR)

    while True:
        # ── who's there? ─────────────────────────────────────────
        hw.stop_breathing()
        profile = hw.choose_profile(list(PROFILES.keys()))
        print(f"[profile] {profile}")

        # ── Nemma greets the profile immediately ───────────────────
        greet(hw, profile)

        # ── conversation loop — stays here until device is restarted ──
        hw.show_ready()
        hw.start_breathing(IDLE_COLOR)

        while True:
            # ── wait for button press, then hold to talk ─────────
            hw.wait_for_button()

            try:
                session = RealtimeSession(
                    on_audio_chunk=play_audio_chunk,
                    on_state_change=lambda s: None,
                    profile=profile,
                )

                hw.stop_breathing()
                hw.show_listening()
                hw.start_breathing(LISTEN_COLOR)

                # Stream mic and pump events concurrently — server VAD
                # auto-commits and creates a response while we're still
                # listening, so we must not miss those audio delta events.
                response_done = threading.Event()
                stream_error  = [None]

                def do_stream():
                    try:
                        stream_microphone(session,
                                          stop_fn=hw.is_button_held,
                                          duration=RECORD_SECONDS)
                        # Send 0.8s of silence so server VAD detects speech end
                        # after button release and triggers a response.
                        silence = b"\x00\x00" * CHUNK
                        n = int(0.8 * MIC_SAMPLE_RATE / CHUNK)
                        for _ in range(n):
                            session.send_audio_chunk(silence)
                    except Exception as e:
                        stream_error[0] = e

                def do_pump():
                    try:
                        session.pump_until_response_done()
                    finally:
                        response_done.set()

                t_stream = threading.Thread(target=do_stream, daemon=True)
                t_pump   = threading.Thread(target=do_pump,   daemon=True)
                t_stream.start()
                t_pump.start()

                # Update screen when response starts arriving
                hw.stop_breathing()
                hw.show_thinking()
                response_done.wait(timeout=30)

                hw.stop_breathing()
                hw.show_speaking()
                hw.start_breathing(SPEAK_COLOR)
                t_pump.join(timeout=5)
                t_stream.join(timeout=2)
                hw.stop_breathing()
                close_playback()
                session.close()

                if stream_error[0]:
                    raise stream_error[0]

            except Exception as exc:
                print(f"[error] {exc}")
                try:
                    hw.show_speaking()
                    fallback = RealtimeSession(
                        on_audio_chunk=play_audio_chunk,
                        on_state_change=lambda s: None,
                        profile=profile,
                    )
                    fallback._send({
                        "type": "response.create",
                        "response": {
                            "instructions": (
                                "Say, gently and warmly: Oh whoosh — I had a little "
                                "hiccup. I'm still here though, I promise."
                            ),
                        },
                    })
                    fallback.pump_until_response_done()
                    close_playback()
                    fallback.close()
                except Exception:
                    pass

            hw.show_ready()
            hw.start_breathing(IDLE_COLOR)


if __name__ == "__main__":
    main()
