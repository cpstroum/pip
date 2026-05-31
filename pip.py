"""
Pip — self-regulation companion for Esther
Unihiker M10 device
"""

import os
import time
import math
import threading
import tempfile
import wave

import anthropic
import openai

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

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY    = os.environ["OPENAI_API_KEY"]

NEOPIXEL_PIN   = "P0"   # change to match your wiring
NEOPIXEL_COUNT = 8

RECORD_SECONDS  = 5
SAMPLE_RATE     = 16000
CHANNELS        = 1
CHUNK           = 1024
SILENCE_THRESH  = 500   # RMS below this = silence
SILENCE_SECS    = 1.5   # consecutive silence before early stop

TTS_VOICE  = "nova"
TTS_MODEL  = "tts-1"
STT_MODEL  = "whisper-1"
LLM_MODEL  = "claude-sonnet-4-20250514"

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

# ── API clients ───────────────────────────────────────────────────────────────

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client    = openai.OpenAI(api_key=OPENAI_API_KEY)

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

    # ── Button ──

    def wait_for_button(self):
        if not ON_DEVICE:
            input("[dev] Press ENTER to simulate button press…")
            return
        btn = Pin(Pin.P23, Pin.IN)
        while btn.read_digital() == 1:
            time.sleep(0.05)


# ── Audio helpers ─────────────────────────────────────────────────────────────

def record_audio() -> bytes:
    """Record from built-in mic; stop early on sustained silence."""
    if not ON_DEVICE:
        print("[dev] Skipping real recording — returning silent stub")
        buf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        _write_silent_wav(buf.name)
        with open(buf.name, "rb") as f:
            return f.read()

    pa     = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )

    frames       = []
    silence_time = 0.0
    start        = time.time()

    while True:
        data = stream.read(CHUNK, exception_on_overflow=False)
        frames.append(data)

        rms = _rms(data)
        dt  = CHUNK / SAMPLE_RATE
        if rms < SILENCE_THRESH:
            silence_time += dt
        else:
            silence_time = 0.0

        elapsed = time.time() - start
        if elapsed >= RECORD_SECONDS:
            break
        if silence_time >= SILENCE_SECS and elapsed > 1.0:
            break

    stream.stop_stream()
    stream.close()
    pa.terminate()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))

    with open(path, "rb") as f:
        return f.read()


def _rms(data: bytes) -> float:
    import struct
    count  = len(data) // 2
    shorts = struct.unpack(f"{count}h", data)
    if not shorts:
        return 0.0
    return math.sqrt(sum(s * s for s in shorts) / count)


def _write_silent_wav(path: str):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"\x00\x00" * SAMPLE_RATE)


def play_audio(audio_bytes: bytes):
    if not ON_DEVICE:
        print("[dev] Would play audio — skipping on desktop")
        return

    pa = pyaudio.PyAudio()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        path = f.name

    with wave.open(path, "rb") as wf:
        stream = pa.open(
            format=pa.get_format_from_width(wf.getsampwidth()),
            channels=wf.getnchannels(),
            rate=wf.getframerate(),
            output=True,
        )
        data = wf.readframes(CHUNK)
        while data:
            stream.write(data)
            data = wf.readframes(CHUNK)
        stream.stop_stream()
        stream.close()

    pa.terminate()


# ── AI pipeline ───────────────────────────────────────────────────────────────

def transcribe(audio_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_bytes)
        path = f.name
    with open(path, "rb") as f:
        result = openai_client.audio.transcriptions.create(
            model=STT_MODEL,
            file=f,
            language="en",
        )
    return result.text.strip()


def ask_pip(transcript: str) -> str:
    message = anthropic_client.messages.create(
        model=LLM_MODEL,
        max_tokens=150,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": transcript}],
    )
    return message.content[0].text.strip()


def speak(text: str) -> bytes:
    response = openai_client.audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        response_format="wav",
    )
    return response.content


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    hw = Hardware()

    print("Pip is starting up…")
    hw.show_idle()
    hw.start_breathing(IDLE_COLOR)

    while True:
        # ── wait for button ──────────────────────────────────────
        hw.wait_for_button()

        # ── listening ────────────────────────────────────────────
        hw.stop_breathing()
        hw.show_listening()
        hw.start_breathing(LISTEN_COLOR)

        audio = record_audio()

        hw.stop_breathing()

        # ── transcribe ───────────────────────────────────────────
        hw.show_thinking()
        hw.pulse_once(THINK_COLOR, duration=0.4)

        try:
            transcript = transcribe(audio)
            print(f"[heard] {transcript!r}")

            if not transcript:
                transcript = "I don't know what to say."

            # ── ask Pip ──────────────────────────────────────────
            response_text = ask_pip(transcript)
            print(f"[pip]   {response_text!r}")

            # ── TTS ──────────────────────────────────────────────
            hw.show_speaking()
            hw.start_breathing(SPEAK_COLOR)
            audio_out = speak(response_text)
            play_audio(audio_out)
            hw.stop_breathing()

        except Exception as exc:
            print(f"[error] {exc}")
            try:
                fallback = "Oh whoosh — I had a little hiccup. I'm still here though, I promise."
                hw.show_speaking()
                audio_out = speak(fallback)
                play_audio(audio_out)
            except Exception:
                pass

        # ── back to idle ─────────────────────────────────────────
        hw.show_idle()
        hw.start_breathing(IDLE_COLOR)


if __name__ == "__main__":
    main()
