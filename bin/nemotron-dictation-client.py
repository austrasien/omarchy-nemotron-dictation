#!/usr/bin/env python3
"""Live Nemotron 3.5 dictation: mic -> websocket -> type whole words."""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 18765
PATH = "/v1/audio/transcriptions/realtime"
RATE = 16000
CHUNK_MS = 160
CHUNK_BYTES = RATE * 2 * CHUNK_MS // 1000
# Omakoe's meter ticks at 50 ms. A 160 ms ASR chunk would paint ~3 identical
# columns, so the pixel shape lagged the voice. HUD frames are independent.
HUD_MS = 50
HUD_BYTES = RATE * 2 * HUD_MS // 1000
# Typical speech peak here is ~0.08; Omakoe's sqrt staircase needs ~0.2–1.0
# to use the 8-pixel height. This is display-only — ASR still gets raw PCM.
HUD_PEAK_REF = 0.12
SILENCE_SECS = 10.0
RMS_SPEECH = 0.012
DUCK_VOLUME = 0.35
PHRASES_FILE = Path.home() / ".config/omarchy/nemotron/phrases.txt"
WORD_RE = re.compile(r"^([\s\S]*?[\s.,;:!?…»]+)(.*)$", re.DOTALL)


class Flags:
    flush = False
    abort = False


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))


def write_state(value: str) -> None:
    rt = runtime_dir()
    for rel in ("nemotron/state", "voxtype/state"):
        path = rt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)


def load_phrases() -> list[str]:
    if not PHRASES_FILE.is_file():
        return []
    out = []
    for line in PHRASES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def mark_typing() -> None:
    path = runtime_dir() / "nemotron" / "last-type"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time_ns()))


def type_text(text: str) -> None:
    if not text:
        return
    mark_typing()
    try:
        subprocess.run(["wtype", "--", text], check=False, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f"wtype failed: {exc}")
    finally:
        mark_typing()


def pcm_metrics(chunk: bytes) -> tuple[float, float, int]:
    n = len(chunk) // 2
    if n <= 0:
        return 0.0, 0.0, 0
    samples = struct.unpack(f"<{n}h", chunk[: n * 2])
    peak = max(abs(s) for s in samples) / 32768.0
    rms = (sum(s * s for s in samples) / n) ** 0.5 / 32768.0
    return peak, rms, 1 if rms >= RMS_SPEECH else 0


def hud_peak(peak: float) -> float:
    if HUD_PEAK_REF <= 0:
        return peak
    return min(1.0, max(0.0, peak / HUD_PEAK_REF))


class LevelWriter:
    def __init__(self) -> None:
        path = runtime_dir() / "nemotron" / "levels.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.fp = path.open("w", encoding="utf-8")

    def write(self, peak: float, rms: float, vad: int) -> None:
        self.fp.write(
            json.dumps(
                {"peak": round(peak, 4), "rms": round(rms, 4), "vad": vad, "ts_ms": int(time.time() * 1000)}
            )
            + "\n"
        )
        self.fp.flush()

    def close(self) -> None:
        try:
            self.fp.close()
        except OSError:
            pass


class Duck:
    def __init__(self) -> None:
        self.original: float | None = None

    def apply(self) -> None:
        try:
            out = subprocess.check_output(
                ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], text=True, timeout=2
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return
        try:
            vol = float(out.split()[1])
        except (IndexError, ValueError):
            return
        self.original = vol
        if vol <= DUCK_VOLUME + 0.01:
            return
        subprocess.run(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", str(DUCK_VOLUME)],
            check=False,
            timeout=2,
        )
        log(f"duck {vol:.2f} -> {DUCK_VOLUME:.2f}")

    def restore(self) -> None:
        if self.original is None:
            return
        subprocess.run(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{self.original:.3f}"],
            check=False,
            timeout=2,
        )
        log(f"unduck -> {self.original:.2f}")
        self.original = None


class Ime:
    """Inactivate fcitx5 while we inject keys so it cannot rewrite or Esc us."""

    def __init__(self) -> None:
        self.prev: str | None = None

    def sleep(self) -> None:
        try:
            self.prev = subprocess.check_output(["fcitx5-remote"], text=True, timeout=1).strip()
            subprocess.run(["fcitx5-remote", "-c"], check=False, timeout=1)
        except (subprocess.SubprocessError, FileNotFoundError):
            self.prev = None

    def restore(self) -> None:
        if self.prev == "2":
            subprocess.run(["fcitx5-remote", "-o"], check=False, timeout=1)
        self.prev = None


class WebSocket:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()

    @classmethod
    def connect(cls, host: str, port: int, path: str) -> WebSocket:
        sock = socket.create_connection((host, port), timeout=10)
        sock.settimeout(None)
        key = __import__("base64").b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed")
            data += chunk
        if b"101" not in data.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket handshake failed")
        return cls(sock)

    def send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", n))
        header.extend(mask)
        self.sock.sendall(header + masked)

    def send_bin(self, payload: bytes) -> None:
        self.send(2, payload)

    def send_json(self, obj: dict) -> None:
        self.send(1, json.dumps(obj, ensure_ascii=False).encode())

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv(self) -> tuple[int, bytes]:
        payload = bytearray()
        opcode = None
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            op = b1 & 0x0F
            masked = b2 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(n)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 9:
                self.send(10, data)
                continue
            if op == 8:
                raise ConnectionError("websocket close")
            if op == 10:
                continue
            if opcode is None:
                opcode = op
            payload.extend(data)
            if fin:
                return opcode or 1, bytes(payload)


def type_complete_words(typed: str, target: str, flush: bool) -> str:
    """Type only finished words (trailing space/punct), or everything if flush."""
    if not target.startswith(typed):
        if not typed:
            extra = target if flush else (WORD_RE.match(target).group(1) if WORD_RE.match(target) else "")
            type_text(extra)
            return typed + extra
        return typed
    rest = target[len(typed) :]
    if flush:
        type_text(rest)
        return target
    m = WORD_RE.match(rest)
    if not m:
        return typed
    type_text(m.group(1))
    return typed + m.group(1)


def drain_events(ws: WebSocket, sel: selectors.BaseSelector, handle, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and not Flags.abort:
        if not sel.select(timeout=0.15):
            continue
        try:
            op, payload = ws.recv()
        except ConnectionError:
            return
        if op == 1:
            handle(payload)


def session_loop(ws: WebSocket, audio_iter, do_type: bool, language: str, levels: LevelWriter) -> str:
    committed = ""
    typed = ""
    partial = ""
    first = time.monotonic()
    first_delta_at = None
    last_speech = time.monotonic()
    heard_speech = False

    hello = ws.recv()
    log(f"session {hello[1][:180]!r}")
    phrases = load_phrases()
    session = {
        "sample_rate": RATE,
        "language": language,
        "automatic_punctuation": True,
        "endpointing_ms": 800,
    }
    if phrases:
        session["speech_contexts"] = [{"phrases": phrases, "boost": 6.0}]
        log(f"boost {len(phrases)} phrases")
    ws.send_json({"type": "session.update", "session": session})

    sel = selectors.DefaultSelector()
    sel.register(ws.sock, selectors.EVENT_READ)

    def handle_msg(raw: bytes) -> None:
        nonlocal committed, typed, partial, first_delta_at, last_speech, heard_speech
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = ev.get("type")
        if kind == "conversation.item.input_audio_transcription.delta":
            piece = ev.get("delta") or ev.get("text") or ""
            if not piece:
                return
            if first_delta_at is None:
                first_delta_at = time.monotonic() - first
                log(f"first delta after {first_delta_at:.2f}s: {piece!r}")
            if ev.get("text"):
                partial = ev["text"]
            else:
                partial += piece
            log(f"delta {piece!r}")
            last_speech = time.monotonic()
            heard_speech = True
            if do_type:
                typed = type_complete_words(typed, committed + partial, flush=False)
        elif kind == "conversation.item.input_audio_transcription.completed":
            text = (ev.get("transcript") or ev.get("text") or "").strip()
            if text:
                committed = (committed + " " if committed and not committed.endswith(" ") else committed) + text
                if not committed.endswith(" "):
                    committed += " "
            partial = ""
            log(f"final {text!r}")
            if do_type:
                typed = type_complete_words(typed, committed, flush=True)
        elif kind == "error":
            log(f"server error: {ev}")
        elif kind in ("session.created", "session.updated", "input_audio_buffer.committed"):
            log(kind)

    pending = b""
    hud_pending = b""
    try:
        for block in audio_iter:
            if Flags.abort or Flags.flush:
                break
            pending += block
            hud_pending += block
            while len(hud_pending) >= HUD_BYTES:
                piece = hud_pending[:HUD_BYTES]
                hud_pending = hud_pending[HUD_BYTES:]
                peak, rms, vad = pcm_metrics(piece)
                levels.write(hud_peak(peak), rms, vad)
            while len(pending) >= CHUNK_BYTES:
                chunk = pending[:CHUNK_BYTES]
                pending = pending[CHUNK_BYTES:]
                _peak, rms, vad = pcm_metrics(chunk)
                now = time.monotonic()
                if vad:
                    heard_speech = True
                    last_speech = now
                elif now - first > 1.0 and now - last_speech >= SILENCE_SECS:
                    log("10s silence, auto-stop")
                    Flags.flush = True
                    break
                ws.send_bin(chunk)
                deadline = time.monotonic() + 0.01
                while time.monotonic() < deadline:
                    if not sel.select(timeout=0):
                        break
                    try:
                        op, payload = ws.recv()
                    except ConnectionError:
                        return typed
                    if op == 1:
                        handle_msg(payload)
            if Flags.abort or Flags.flush:
                break
    except KeyboardInterrupt:
        if not Flags.abort:
            Flags.flush = True

    if pending and not Flags.abort:
        ws.send_bin(pending)

    if Flags.abort:
        log("cancelled, drop buffer")
        return typed

    write_state("transcribing")
    try:
        ws.send_json({"type": "input_audio_buffer.commit"})
    except OSError:
        pass
    drain_events(ws, sel, handle_msg, 3.0)
    if do_type:
        typed = type_complete_words(typed, committed + partial, flush=True)
    return typed


def mic_chunks():
    proc = subprocess.Popen(
        [
            "pw-record",
            "--rate", str(RATE),
            "--channels", "1",
            "--format", "s16",
            "--raw",
            "--latency", f"{HUD_MS}ms",
            "--media-type", "Audio",
            "--media-category", "Capture",
            "--media-role", "Communication",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    try:
        while not Flags.abort and not Flags.flush:
            buf = proc.stdout.read(CHUNK_BYTES)
            if not buf:
                break
            yield buf
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def file_chunks(pcm: bytes):
    for i in range(0, len(pcm), CHUNK_BYTES):
        if Flags.abort or Flags.flush:
            break
        yield pcm[i : i + CHUNK_BYTES]
        time.sleep(CHUNK_MS / 1000)


def wav_pcm16_mono_16k(path: Path) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(RATE),
            "pipe:1",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path)
    ap.add_argument("--no-type", action="store_true")
    ap.add_argument("--language", default="fr-FR")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    def on_int(_s, _f):
        if not Flags.abort:
            Flags.flush = True

    def on_usr1(_s, _f):
        Flags.abort = True

    signal.signal(signal.SIGINT, on_int)
    signal.signal(signal.SIGTERM, on_int)
    signal.signal(signal.SIGUSR1, on_usr1)

    duck = Duck()
    ime = Ime()
    levels = LevelWriter()
    ws = None
    write_state("recording")
    duck.apply()
    ime.sleep()
    try:
        ws = WebSocket.connect(args.host, args.port, PATH)
        if args.file:
            pcm = wav_pcm16_mono_16k(args.file)
            log(f"file {args.file} pcm={len(pcm)} bytes")
            audio = file_chunks(pcm)
        else:
            log("microphone capture")
            audio = mic_chunks()
        typed = session_loop(ws, audio, not args.no_type, args.language, levels)
        log(f"typed={typed!r}")
        return 0
    except KeyboardInterrupt:
        Flags.flush = True
        log("interrupted")
        return 0
    except Exception as exc:
        log(f"fail: {exc}")
        return 1
    finally:
        ime.restore()
        duck.restore()
        write_state("idle")
        levels.close()
        if ws is not None:
            try:
                ws.sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
