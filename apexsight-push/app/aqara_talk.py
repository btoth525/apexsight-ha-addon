"""Aqara camera LAN talkback — send audio to an Aqara doorbell/camera speaker.

The Aqara G400 (and compatible lumi.camera models) accept talkback over the LAN with no cloud,
no hub, and no auth: a TCP control channel (:54324) does a START_VOICE / STOP_VOICE / HEARTBEAT
handshake, and audio is streamed as AAC-LC ADTS frames in RTP packets over UDP (:54323), 16 kHz
mono. This lets the ApexSight relay play a clip (or, later, a live mic stream) straight to the
doorbell speaker.

Protocol reverse-engineered from the Aqara Android app; ported from the MIT-licensed reference
https://github.com/absent42/aqara-doorbell (aqara_lan_talk.py) and cross-checked against the
Scrypted plugin https://github.com/DTse/aqara-scrypted (src/intercom-session.ts). Validated live
against a real G400 (START_VOICE → ACK(0) → STOP_VOICE → ACK(0)).
"""
from __future__ import annotations

import random
import socket
import struct
import subprocess
import threading
import time
from typing import Callable, List, Sequence, Tuple

CONTROL_PORT = 54324
AUDIO_PORT = 54323
RTP_PAYLOAD_TYPE = 97  # AAC dynamic payload type
HEARTBEAT_INTERVAL = 5.0
CONNECT_TIMEOUT = 3.0
SAMPLES_PER_FRAME = 1024  # AAC-LC frame @ 16 kHz
FRAME_SECONDS = SAMPLES_PER_FRAME / 16000  # ~64 ms per AAC frame

# The Aqara opens its speaker ~0.5-0.8s AFTER the START_VOICE handshake, and it primes on the RTP
# audio stream itself (not the ACK) — so streaming the real clip immediately swallows the first
# word, and firing STOP_VOICE the instant ffmpeg hits EOF flushes the decoder buffer before it has
# played the last word. We bracket the real audio with paced silence: a lead-in warms the speaker
# while wall-clock elapses, a tail keeps the stream alive so the buffer drains, then a short pause
# before STOP_VOICE. (An ffmpeg `-af adelay` lead-in was tried and DROPPED under `-re` — it emits
# non-monotonic DTS and the silence vanishes; sending silence RTP frames is the reliable path.)
SILENCE_LEAD_SECONDS = 0.8
SILENCE_TAIL_SECONDS = 0.5
DRAIN_SECONDS = 0.3

MAGIC = b"\xFE\xEF"
TYPE_START_VOICE = 0
TYPE_STOP_VOICE = 1
TYPE_ACK = 2
TYPE_HEARTBEAT = 3

# CRC-16/KERMIT (Aqara variant): poly 0x8408 reflected, init 0xFFFF, final XOR 0xFFFF.
_CRC16_TABLE = [
    0x0000, 0x1189, 0x2312, 0x329B, 0x4624, 0x57AD, 0x6536, 0x74BF, 0x8C48, 0x9DC1, 0xAF5A, 0xBED3,
    0xCA6C, 0xDBE5, 0xE97E, 0xF8F7, 0x1081, 0x0108, 0x3393, 0x221A, 0x56A5, 0x472C, 0x75B7, 0x643E,
    0x9CC9, 0x8D40, 0xBFDB, 0xAE52, 0xDAED, 0xCB64, 0xF9FF, 0xE876, 0x2102, 0x308B, 0x0210, 0x1399,
    0x6726, 0x76AF, 0x4434, 0x55BD, 0xAD4A, 0xBCC3, 0x8E58, 0x9FD1, 0xEB6E, 0xFAE7, 0xC87C, 0xD9F5,
    0x3183, 0x200A, 0x1291, 0x0318, 0x77A7, 0x662E, 0x54B5, 0x453C, 0xBDCB, 0xAC42, 0x9ED9, 0x8F50,
    0xFBEF, 0xEA66, 0xD8FD, 0xC974, 0x4204, 0x538D, 0x6116, 0x709F, 0x0420, 0x15A9, 0x2732, 0x36BB,
    0xCE4C, 0xDFC5, 0xED5E, 0xFCD7, 0x8868, 0x99E1, 0xAB7A, 0xBAF3, 0x5285, 0x430C, 0x7197, 0x601E,
    0x14A1, 0x0528, 0x37B3, 0x263A, 0xDECD, 0xCF44, 0xFDDF, 0xEC56, 0x98E9, 0x8960, 0xBBFB, 0xAA72,
    0x6306, 0x728F, 0x4014, 0x519D, 0x2522, 0x34AB, 0x0630, 0x17B9, 0xEF4E, 0xFEC7, 0xCC5C, 0xDDD5,
    0xA96A, 0xB8E3, 0x8A78, 0x9BF1, 0x7387, 0x620E, 0x5095, 0x411C, 0x35A3, 0x242A, 0x16B1, 0x0738,
    0xFFCF, 0xEE46, 0xDCDD, 0xCD54, 0xB9EB, 0xA862, 0x9AF9, 0x8B70, 0x8408, 0x9581, 0xA71A, 0xB693,
    0xC22C, 0xD3A5, 0xE13E, 0xF0B7, 0x0840, 0x19C9, 0x2B52, 0x3ADB, 0x4E64, 0x5FED, 0x6D76, 0x7CFF,
    0x9489, 0x8500, 0xB79B, 0xA612, 0xD2AD, 0xC324, 0xF1BF, 0xE036, 0x18C1, 0x0948, 0x3BD3, 0x2A5A,
    0x5EE5, 0x4F6C, 0x7DF7, 0x6C7E, 0xA50A, 0xB483, 0x8618, 0x9791, 0xE32E, 0xF2A7, 0xC03C, 0xD1B5,
    0x2942, 0x38CB, 0x0A50, 0x1BD9, 0x6F66, 0x7EEF, 0x4C74, 0x5DFD, 0xB58B, 0xA402, 0x9699, 0x8710,
    0xF3AF, 0xE226, 0xD0BD, 0xC134, 0x39C3, 0x284A, 0x1AD1, 0x0B58, 0x7FE7, 0x6E6E, 0x5CF5, 0x4D7C,
    0xC60C, 0xD785, 0xE51E, 0xF497, 0x8028, 0x91A1, 0xA33A, 0xB2B3, 0x4A44, 0x5BCD, 0x6956, 0x78DF,
    0x0C60, 0x1DE9, 0x2F72, 0x3EFB, 0xD68D, 0xC704, 0xF59F, 0xE416, 0x90A9, 0x8120, 0xB3BB, 0xA232,
    0x5AC5, 0x4B4C, 0x79D7, 0x685E, 0x1CE1, 0x0D68, 0x3FF3, 0x2E7A, 0xE70E, 0xF687, 0xC41C, 0xD595,
    0xA12A, 0xB0A3, 0x8238, 0x93B1, 0x6B46, 0x7ACF, 0x4854, 0x59DD, 0x2D62, 0x3CEB, 0x0E70, 0x1FF9,
    0xF78F, 0xE606, 0xD49D, 0xC514, 0xB1AB, 0xA022, 0x92B9, 0x8330, 0x7BC7, 0x6A4E, 0x58D5, 0x495C,
    0x3DE3, 0x2C6A, 0x1EF1, 0x0F78,
]


class TalkbackError(Exception):
    """Raised when the camera refuses the voice session or is unreachable."""


# The camera accepts ONE voice session at a time — serialize all plays through this lock so a
# concurrent request (app clip + HA say at once) fails fast and clean instead of colliding on the
# camera (garbled audio / a cut-off clip / a raw 500).
_session_lock = threading.Lock()


def _crc16_kermit(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = _CRC16_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return (~crc) & 0xFFFF


def build_packet(pkt_type: int, value: int) -> bytes:
    payload = struct.pack(">B", value & 0xFF) if pkt_type == TYPE_ACK else struct.pack(">Q", value)
    header = MAGIC + struct.pack(">B", pkt_type) + struct.pack(">H", len(payload))
    crc = _crc16_kermit(header[2:] + payload)
    return header + payload + struct.pack(">H", crc)


def parse_packet(data: bytes) -> dict | None:
    if len(data) < 8 or data[0:2] != MAGIC:
        return None
    pkt_type = data[2]
    if pkt_type > 3:
        return None
    payload_len = struct.unpack(">H", data[3:5])[0]
    if len(data) < 5 + payload_len + 2:
        return None
    crc_data = data[2:5 + payload_len]
    expected = struct.unpack(">H", data[5 + payload_len:7 + payload_len])[0]
    if _crc16_kermit(crc_data) != expected:
        return None
    payload = data[5:5 + payload_len]
    value = payload[0] if pkt_type == TYPE_ACK else int.from_bytes(payload, "big")
    return {"type": pkt_type, "value": value}


def _build_rtp_header(seq: int, timestamp: int, ssrc: int) -> bytes:
    # V=2, no padding/ext/CSRC; marker 0; dynamic PT 97.
    return struct.pack(">BBHII", 0x80, RTP_PAYLOAD_TYPE & 0x7F, seq & 0xFFFF,
                       timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)


def _extract_adts_frames(buf: bytes) -> Tuple[List[bytes], bytes]:
    frames: List[bytes] = []
    off = 0
    while off <= len(buf) - 7:
        if buf[off] != 0xFF or (buf[off + 1] & 0xF0) != 0xF0:
            off += 1
            continue
        frame_len = ((buf[off + 3] & 0x03) << 11) | (buf[off + 4] << 3) | (buf[off + 5] >> 5)
        if frame_len < 7:
            break
        if off + frame_len > len(buf):
            break
        frames.append(buf[off:off + frame_len])
        off += frame_len
    return frames, buf[off:]


_silence_lock = threading.Lock()
_silence_frames_cache: "List[bytes] | None" = None


def _silence_frames(ffmpeg: str) -> List[bytes]:
    """AAC-LC ADTS silent frames (16 kHz mono, 32 kbps) — encoded exactly like the real audio so
    the camera decoder sees one continuous stream. Generated once via ffmpeg and cached; returns
    an empty list (padding simply skipped) if ffmpeg can't be run."""
    global _silence_frames_cache
    with _silence_lock:
        if _silence_frames_cache is None:
            try:
                out = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error",
                     "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1.0",
                     "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "32k",
                     "-ar", "16000", "-ac", "1", "-f", "adts", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                ).stdout
                frames, _ = _extract_adts_frames(out)
                _silence_frames_cache = frames or []
            except (OSError, subprocess.SubprocessError):
                _silence_frames_cache = []
        return _silence_frames_cache


def _ffmpeg_args(ffmpeg: str, input_args: Sequence[str], volume_gain: float,
                 max_seconds: float) -> List[str]:
    # -re (in input_args) paces at real time; low-delay flags keep latency down. Output is the
    # camera's required format: AAC-LC ADTS, 16 kHz, mono, 32 kbps.
    vol: List[str] = []
    if volume_gain and abs(volume_gain - 1.0) > 1e-3 and volume_gain > 0:
        vol = ["-af",
               f"acompressor=threshold=-20dB:ratio=4:attack=5:release=50:makeup=2,"
               f"volume={volume_gain:.2f}"]
    # NB: no `-flags low_delay` / `-fflags nobuffer` — on modern ffmpeg those break audio-only
    # encoding ("No filtered frames for output stream"), and `-re` already paces at real time.
    # A network input (play-url) gets an I/O timeout so a hanging host can't stall ffmpeg forever,
    # and `-t` hard-caps the output duration so the read loop always reaches EOF.
    args = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    input_list = list(input_args)
    if any(str(a).startswith(("http://", "https://", "rtsp://")) for a in input_list):
        args += ["-rw_timeout", "10000000"]   # 10s, microseconds
    args += input_list
    args += vol
    args += ["-t", f"{max_seconds:.0f}",
             "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "32k", "-ar", "16000", "-ac", "1",
             "-f", "adts", "pipe:1"]
    return args


def play_audio(
    camera_ip: str,
    input_args: Sequence[str],
    *,
    ffmpeg: str = "ffmpeg",
    volume_gain: float = 1.0,
    max_seconds: float = 30.0,
    log: Callable[[str], None] = print,
) -> int:
    """Open a talkback session to ``camera_ip`` and stream ffmpeg-decoded audio to its speaker.

    ``input_args`` is the ffmpeg input spec, e.g. ``["-re", "-i", "/path/clip.mp3"]``. Blocking —
    run it in a thread from async code. Returns the number of AAC frames sent. Raises
    ``TalkbackError`` if the camera can't be reached or refuses the voice session.
    """
    # One voice session at a time (camera limitation). Fail fast — the caller surfaces "busy".
    if not _session_lock.acquire(blocking=False):
        raise TalkbackError("talkback busy — another clip is already playing at the door")
    try:
        return _play_locked(camera_ip, input_args, ffmpeg=ffmpeg, volume_gain=volume_gain,
                            max_seconds=max_seconds, log=log)
    finally:
        _session_lock.release()


def _play_locked(camera_ip, input_args, *, ffmpeg, volume_gain, max_seconds, log) -> int:
    session_ts = int(time.time() * 1000)
    ssrc = random.randint(1, 0x7FFFFFFF)

    try:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tcp.settimeout(CONNECT_TIMEOUT)
        tcp.connect((camera_ip, CONTROL_PORT))
    except OSError as exc:
        raise TalkbackError(f"cannot reach {camera_ip}:{CONTROL_PORT}: {exc}") from exc

    try:
        # The handshake recv can time out (camera busy / slow) — that's a talkback failure the
        # caller should see as 502 "camera didn't answer", never a raw 500.
        try:
            tcp.sendall(build_packet(TYPE_START_VOICE, session_ts))
            ack = parse_packet(tcp.recv(1024))
        except OSError as exc:
            raise TalkbackError(f"camera didn't answer the voice handshake: {exc}") from exc
        if not ack or ack["type"] != TYPE_ACK or ack["value"] != 0:
            raise TalkbackError(f"voice session rejected: {ack}")
        log(f"[talk] voice session up to {camera_ip} (ssrc={ssrc})")

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            try:
                proc = subprocess.Popen(
                    _ffmpeg_args(ffmpeg, input_args, volume_gain, max_seconds),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            except OSError as exc:   # ffmpeg missing / not executable
                raise TalkbackError(f"cannot run ffmpeg: {exc}") from exc

            # Backstop for a wedged ffmpeg (input host that accepts but never sends, despite
            # -rw_timeout): kill it so the read loop below always reaches EOF and this thread
            # can never be leaked holding the camera's one voice session.
            killer = threading.Timer(max_seconds + 20, proc.kill)
            killer.daemon = True
            killer.start()

            stop_hb = threading.Event()

            def _heartbeat() -> None:
                while not stop_hb.wait(HEARTBEAT_INTERVAL):
                    try:
                        tcp.sendall(build_packet(TYPE_HEARTBEAT, session_ts))
                    except OSError:
                        break

            hb_thread = threading.Thread(target=_heartbeat, daemon=True)
            hb_thread.start()

            seq = 0

            def _emit(frame: bytes) -> None:
                nonlocal seq
                header = _build_rtp_header(seq, seq * SAMPLES_PER_FRAME, ssrc)
                udp.sendto(header + frame, (camera_ip, AUDIO_PORT))
                seq += 1

            def _emit_silence(seconds: float) -> None:
                # Paced at real time so the speaker actually warms / the buffer actually drains —
                # a burst would arrive in ~1ms and defeat the purpose.
                for i in range(int(seconds / FRAME_SECONDS)):
                    _emit(silence[i % len(silence)])
                    time.sleep(FRAME_SECONDS)

            silence = _silence_frames(ffmpeg)
            if silence:
                _emit_silence(SILENCE_LEAD_SECONDS)  # warm the speaker before the first word

            real_frames = 0
            buf = b""
            try:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read1(4096)  # -t caps output, so EOF always arrives
                    if not chunk:
                        break
                    buf += chunk
                    frames, buf = _extract_adts_frames(buf)
                    for frame in frames:
                        _emit(frame)  # real audio is already real-time paced by ffmpeg -re
                        real_frames += 1
            finally:
                stop_hb.set()
                killer.cancel()
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    err = proc.stderr.read().decode("utf-8", "ignore").strip() if proc.stderr else ""
                    if err:
                        log(f"[talk][ffmpeg] {err}")
                except OSError:
                    pass

            # Keep the RTP stream alive past the last word so the camera's decode buffer plays it
            # out, then pause before STOP_VOICE (the finally below) flushes the speaker.
            if silence:
                _emit_silence(SILENCE_TAIL_SECONDS)
            time.sleep(DRAIN_SECONDS)
        finally:
            udp.close()

        log(f"[talk] sent {real_frames} AAC frames "
            f"(~{real_frames * SAMPLES_PER_FRAME / 16000:.1f}s) + silence pad")
        return real_frames
    finally:
        try:
            tcp.sendall(build_packet(TYPE_STOP_VOICE, session_ts))
            tcp.settimeout(1.0)
            tcp.recv(1024)
        except OSError:
            pass
        tcp.close()


def probe(camera_ip: str) -> bool:
    """Non-invasive reachability check: TCP connect to the control port and close, WITHOUT the
    START_VOICE handshake. The camera has exactly ONE voice session — the bridge polls this every
    ~30s for the HA "Reachable" sensor, and a handshake probe would collide with (or cut off) a
    real clip playing at the door. A connectable control port is reachable enough."""
    tcp = None
    try:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.settimeout(CONNECT_TIMEOUT)
        tcp.connect((camera_ip, CONTROL_PORT))
        return True
    except OSError:
        return False
    finally:
        if tcp is not None:
            try:
                tcp.close()
            except OSError:
                pass
