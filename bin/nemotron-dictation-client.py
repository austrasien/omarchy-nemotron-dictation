#!/usr/bin/env python3
"""Live Nemotron 3.5 dictation: mic -> websocket -> type as you speak.

Open (incomplete) words are typed live. If the ASR revises a hypothesis
("démarre" -> "démarrage"), we Backspace the divergent suffix and retype.
"""
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
PUNCT_FILE = Path.home() / ".config/omarchy/nemotron/spoken-punctuation.txt"
# Regexes matched against "class | title" of the active window; never type there.
NO_TYPE_FILE = Path.home() / ".config/omarchy/nemotron/no-type-windows.txt"
_NO_TYPE_DEFAULT = (
    r"^org\.gnome\.Nautilus ",
    r"^(thunar|nemo|pcmanfm(-qt)?|org\.kde\.dolphin|dolphin|caja) ",
    r"\| (Yazi|ranger|lf|nnn)\b",
)

# Longest phrases first. FR + EN. Override/extend via spoken-punctuation.txt.
_SPOKEN_PUNCT_DEFAULT: tuple[tuple[str, str], ...] = (
    ("points de suspension", "…"),
    ("trois points", "…"),
    ("point d'interrogation", "?"),
    ("point d interrogation", "?"),
    ("point interrogation", "?"),
    ("point interrogatif", "?"),
    ("point d'exclamation", "!"),
    ("point d exclamation", "!"),
    ("point exclamation", "!"),
    ("point exclamatif", "!"),
    ("point virgule", ";"),
    ("point-virgule", ";"),
    ("deux points", ":"),
    ("deux-points", ":"),
    ("nouvelle ligne", "\n"),
    ("nouvel alinéa", "\n\n"),
    ("nouveau paragraphe", "\n\n"),
    ("retour à la ligne", "\n"),
    ("à la ligne", "\n"),
    ("ouvrir parenthèse", "("),
    ("parenthèse ouvrante", "("),
    ("parenthèse ouverte", "("),
    ("fermer parenthèse", ")"),
    ("parenthèse fermante", ")"),
    ("parenthèse fermée", ")"),
    ("ouvrir crochet", "["),
    ("crochet ouvrant", "["),
    ("fermer crochet", "]"),
    ("crochet fermant", "]"),
    ("ouvrir guillemets", "«"),
    ("guillemets ouvrants", "«"),
    ("fermer guillemets", "»"),
    ("guillemets fermants", "»"),
    ("trait d'union", "-"),
    ("trait d union", "-"),
    ("tiret bas", "_"),
    ("barre oblique", "/"),
    ("exclamation mark", "!"),
    ("exclamation point", "!"),
    ("question mark", "?"),
    ("semi colon", ";"),
    ("semicolon", ";"),
    ("full stop", "."),
    ("new line", "\n"),
    ("newline", "\n"),
    ("new paragraph", "\n\n"),
    ("open paren", "("),
    ("open parenthesis", "("),
    ("left paren", "("),
    ("close paren", ")"),
    ("close parenthesis", ")"),
    ("right paren", ")"),
    ("open bracket", "["),
    ("close bracket", "]"),
    ("open brace", "{"),
    ("close brace", "}"),
    ("virgule", ","),
    ("point", "."),
    ("arobase", "@"),
    ("arobas", "@"),
    ("hashtag", "#"),
    ("dièse", "#"),
    ("diese", "#"),
    ("tiret", "-"),
    ("underscore", "_"),
    ("slash", "/"),
    ("antislash", "\\"),
    ("backslash", "\\"),
    ("period", "."),
    ("comma", ","),
    ("colon", ":"),
    ("hyphen", "-"),
    ("dash", "—"),
    ("tab", "\t"),
)


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


def _unescape_punct_repl(value: str) -> str:
    return (
        value.replace("\\\\", "\0")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\0", "\\")
    )


def load_spoken_punctuation() -> list[tuple[str, str]]:
    """Builtin FR/EN map, then optional overrides from spoken-punctuation.txt.

    File format (one per line):  spoken phrase = replacement
    Example:  point = .
    Longer phrases win (sorted by length). Use \\n and \\t in replacements.
    """
    mapping: dict[str, str] = {k.casefold(): v for k, v in _SPOKEN_PUNCT_DEFAULT}
    if PUNCT_FILE.is_file():
        for raw in PUNCT_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            left, right = line.split("=", 1)
            phrase = left.strip()
            repl = _unescape_punct_repl(right.strip())
            if phrase:
                mapping[phrase.casefold()] = repl
    items = list(mapping.items())
    items.sort(key=lambda kv: len(kv[0]), reverse=True)
    return items


_SPOKEN_PUNCT_RES: list[tuple[re.Pattern[str], str]] | None = None

# Ambiguous tokens: don't fire inside common collocations.
_PUNCT_LOOKAHEAD = {
    "point": r"(?!\s+de\b)",
    "period": r"(?!\s+of\b)",
}


def spoken_punct_rules() -> list[tuple[re.Pattern[str], str]]:
    global _SPOKEN_PUNCT_RES
    if _SPOKEN_PUNCT_RES is None:
        rules: list[tuple[re.Pattern[str], str]] = []
        for phrase, repl in load_spoken_punctuation():
            extra = _PUNCT_LOOKAHEAD.get(phrase, "")
            rules.append(
                (
                    re.compile(
                        rf"(?<!\w){re.escape(phrase)}(?!\w){extra}",
                        re.IGNORECASE,
                    ),
                    repl,
                )
            )
        _SPOKEN_PUNCT_RES = rules
    return _SPOKEN_PUNCT_RES


def apply_spoken_punctuation(text: str) -> str:
    """Convert 'point' → '.', 'virgule' → ',', etc. before typing."""
    if not text:
        return text
    out = text
    for pat, repl in spoken_punct_rules():
        # Callable repl so backslash tokens (\\) are literal, not re templates.
        out = pat.sub(lambda _m, r=repl: r, out)
    # "bonjour ." → "bonjour." ; keep a space after the mark when more words follow
    out = re.sub(r"\s+([.,;:!?…»)\]])", r"\1", out)
    out = re.sub(r"([«(\[])\s+", r"\1", out)
    out = re.sub(r"\n +", "\n", out)
    out = re.sub(r" {2,}", " ", out)
    return out


def typing_target(committed: str, partial: str = "") -> str:
    return apply_spoken_punctuation(committed + partial)


class KeyStop:
    """Hyprland submap "dictation": a real key press stops and finishes.

    The submap holds the key back (bindings.lua records it); disarm() replays
    it into the window once the dictation is typed, so it lands after it.

    The catchall would also swallow wtype's own keys, and bind device filters
    can't reliably tell the wtype virtual keyboard apart. So we leave the
    submap for the duration of each wtype call (~3 ms per switch).
    """

    active = False

    @staticmethod
    def _eval(code: str) -> None:
        try:
            subprocess.run(
                ["hyprctl", "eval", code],
                check=False,
                timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    @classmethod
    def _submap(cls, name: str) -> None:
        cls._eval(f"hl.dispatch(hl.dsp.submap('{name}'))")

    @classmethod
    def arm(cls) -> None:
        cls.active = True
        cls._eval("nemotron_keys_clear()")
        cls._submap("dictation")

    @classmethod
    def disarm(cls, replay: bool) -> None:
        """Leave the submap, then type the held-back keys after the dictation."""
        if not cls.active:
            return
        cls.active = False
        cls._submap("reset")
        cls._eval("nemotron_keys_replay()" if replay else "nemotron_keys_clear()")

    @classmethod
    def run_unguarded(cls, cmd: list[str], timeout: float) -> None:
        if cls.active:
            cls._submap("reset")
        try:
            subprocess.run(cmd, check=False, timeout=timeout)
        finally:
            if cls.active:
                cls._submap("dictation")


def mark_typing() -> None:
    path = runtime_dir() / "nemotron" / "last-type"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time_ns()))


def type_text(text: str) -> None:
    if not text:
        return
    mark_typing()
    try:
        KeyStop.run_unguarded(["wtype", "--", text], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f"wtype failed: {exc}")
    finally:
        mark_typing()


def backspace(n: int) -> None:
    """Delete n characters behind the caret (revising a live hypothesis)."""
    if n <= 0:
        return
    mark_typing()
    try:
        # Batch: wtype accepts repeated -k. Cap timeout with length.
        cmd: list[str] = ["wtype"]
        for _ in range(n):
            cmd.extend(["-k", "BackSpace"])
        KeyStop.run_unguarded(cmd, timeout=max(5.0, 0.02 * n + 2.0))
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log(f"wtype backspace failed: {exc}")
    finally:
        mark_typing()


def sync_plan(typed: str, target: str) -> tuple[int, str]:
    """How to turn the already-typed buffer into target: (backspaces, suffix)."""
    i = 0
    n = min(len(typed), len(target))
    while i < n and typed[i] == target[i]:
        i += 1
    return len(typed) - i, target[i:]


def sync_typed(typed: str, target: str) -> str:
    """Type live, including the open last word; revise with Backspace if ASR changes it.

    Example: typed "démarre" + new hyp "démarrage" → type "age".
    Example: typed "démarrage" + new hyp "démarrer" → BackSpace×3, type "er".
    """
    if typed == target or not Guard.allow():
        return typed
    n_bs, suffix = sync_plan(typed, target)
    if n_bs:
        log(f"revise -{n_bs} +{suffix!r}")
        backspace(n_bs)
    if suffix:
        type_text(suffix)
    return target


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

    @staticmethod
    def has_text_field() -> bool:
        """True if fcitx5 has a focused input context (an editable field).

        Apps enable text-input only while an editable widget has focus, so
        `fcitx5-remote` prints 0 on a bare window. Unknown counts as no.
        """
        try:
            out = subprocess.check_output(["fcitx5-remote"], text=True, timeout=1).strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return False
        return out not in ("", "0")


class Guard:
    """Only type into the window that had a text field when typing began.

    wtype sends keys to whatever has focus. In a file manager they are
    shortcuts: dictating into Nautilus trashed the selected folder. So:
    - never type into NO_TYPE_FILE classes / titles (file managers),
    - require a focused fcitx5 input context on the first keystroke,
    - stop typing for good if the active window changes mid-dictation.
    The clipboard always gets the dictation at the end (see copy_dictation).
    """

    blocked = False
    reason = ""
    address: str | None = None
    final_text = ""
    _patterns: list[re.Pattern[str]] | None = None

    @classmethod
    def patterns(cls) -> list[re.Pattern[str]]:
        if cls._patterns is None:
            lines = list(_NO_TYPE_DEFAULT)
            if NO_TYPE_FILE.is_file():
                for raw in NO_TYPE_FILE.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if line and not line.startswith("#"):
                        lines.append(line)
            cls._patterns = [re.compile(x, re.IGNORECASE) for x in lines]
        return cls._patterns

    @staticmethod
    def active_window() -> tuple[str, str, str] | None:
        try:
            out = subprocess.check_output(["hyprctl", "activewindow", "-j"], text=True, timeout=1)
            win = json.loads(out)
        except (subprocess.SubprocessError, FileNotFoundError, json.JSONDecodeError):
            return None
        if not isinstance(win, dict) or not win.get("address"):
            return None
        return win["address"], win.get("class") or "", win.get("title") or ""

    @classmethod
    def block(cls, reason: str) -> bool:
        if not cls.blocked:
            cls.blocked = True
            cls.reason = reason
            log(f"typing blocked: {reason}")
        return False

    @classmethod
    def allow(cls) -> bool:
        if cls.blocked:
            return False
        win = cls.active_window()
        if win is None:
            return cls.block("no active window")
        address, klass, title = win
        if cls.address is not None:
            return True if address == cls.address else cls.block(f"focus moved to {klass}")
        label = f"{klass} | {title}"
        for pat in cls.patterns():
            if pat.search(label):
                return cls.block(f"no-type window {label!r}")
        if not Ime.has_text_field():
            return cls.block(f"no text field in {klass}")
        cls.address = address
        log(f"typing into {label!r}")
        return True


def copy_dictation() -> None:
    """Always keep the dictation in the clipboard; notify if it wasn't typed."""
    text = Guard.final_text.strip()
    if not text:
        return
    try:
        subprocess.run(["wl-copy", "--", text], check=False, timeout=2)
        if Guard.blocked:
            subprocess.run(
                ["notify-send", "-a", "Dictée", "Dictée copiée", "Pas de champ texte : Ctrl+V pour coller."],
                check=False,
                timeout=2,
            )
        log("copied to clipboard")
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        log(f"clipboard copy failed: {exc}")


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


def drain_events(ws: WebSocket, sel: selectors.BaseSelector, handle, seconds: float, done=lambda: False) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and not Flags.abort and not done():
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
    finals = 0

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
        nonlocal committed, typed, partial, first_delta_at, last_speech, heard_speech, finals
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
                typed = sync_typed(typed, typing_target(committed, partial))
        elif kind == "conversation.item.input_audio_transcription.completed":
            text = (ev.get("transcript") or ev.get("text") or "").strip()
            if text:
                committed = (committed + " " if committed and not committed.endswith(" ") else committed) + text
                if not committed.endswith(" "):
                    committed += " "
            partial = ""
            finals += 1
            log(f"final {text!r}")
            if do_type:
                typed = sync_typed(typed, typing_target(committed))
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
    # Keys stay swallowed until we finish typing, so stop at the first final
    # after the commit rather than always waiting the full 3 s.
    finals_before = finals
    try:
        ws.send_json({"type": "input_audio_buffer.commit"})
    except OSError:
        pass
    drain_events(ws, sel, handle_msg, 3.0, lambda: finals > finals_before)
    if do_type:
        typed = sync_typed(typed, typing_target(committed, partial))
    Guard.final_text = typing_target(committed, partial)
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
        if not args.no_type:
            KeyStop.arm()
        typed = session_loop(ws, audio, not args.no_type, args.language, levels)
        log(f"typed={typed!r}")
        if not args.no_type and not Flags.abort:
            copy_dictation()
        return 0
    except KeyboardInterrupt:
        Flags.flush = True
        log("interrupted")
        return 0
    except Exception as exc:
        log(f"fail: {exc}")
        return 1
    finally:
        KeyStop.disarm(replay=not Flags.abort)
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
