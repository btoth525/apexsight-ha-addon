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


def _ffmpeg_args(ffmpeg: str, input_args: Sequence[str], volume_gain: float) -> List[str]:
    # -re (in input_args) paces at real time; low-delay flags keep latency down. Output is the
    # camera's required format: AAC-LC ADTS, 16 kHz, mono, 32 kbps.
    vol: List[str] = []
    if volume_gain and abs(volume_gain - 1.0) > 1e-3 and volume_gain > 0:
        vol = ["-af",
               f"acompressor=threshold=-20dB:ratio=4:attack=5:release=50:makeup=2,"
               f"volume={volume_gain:.2f}"]
    # NB: no `-flags low_delay` / `-fflags nobuffer` — on modern ffmpeg those break audio-only
    # encoding ("No filtered frames for output stream"), and `-re` already paces at real time.
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        *input_args, *vol,
        "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "32k", "-ar", "16000", "-ac", "1",
        "-f", "adts", "pipe:1",
    ]


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
        tcp.sendall(build_packet(TYPE_START_VOICE, session_ts))
        ack = parse_packet(tcp.recv(1024))
        if not ack or ack["type"] != TYPE_ACK or ack["value"] != 0:
            raise TalkbackError(f"voice session rejected: {ack}")
        log(f"[talk] voice session up to {camera_ip} (ssrc={ssrc})")

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        proc = subprocess.Popen(
            _ffmpeg_args(ffmpeg, input_args, volume_gain),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

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
        buf = b""
        started = time.monotonic()
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read1(4096)  # returns what's available; -re keeps it realtime
                if not chunk:
                    break
                buf += chunk
                frames, buf = _extract_adts_frames(buf)
                for frame in frames:
                    header = _build_rtp_header(seq, seq * SAMPLES_PER_FRAME, ssrc)
                    udp.sendto(header + frame, (camera_ip, AUDIO_PORT))
                    seq += 1
                if time.monotonic() - started > max_seconds:
                    log(f"[talk] hit max_seconds={max_seconds}, stopping")
                    break
        finally:
            stop_hb.set()
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
            udp.close()

        log(f"[talk] sent {seq} AAC frames (~{seq * SAMPLES_PER_FRAME / 16000:.1f}s)")
        return seq
    finally:
        try:
            tcp.sendall(build_packet(TYPE_STOP_VOICE, session_ts))
            tcp.settimeout(1.0)
            tcp.recv(1024)
        except OSError:
            pass
        tcp.close()


def probe(camera_ip: str) -> bool:
    """Silent reachability + handshake check (START_VOICE → ACK → STOP_VOICE), no audio."""
    session_ts = int(time.time() * 1000)
    try:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tcp.settimeout(CONNECT_TIMEOUT)
        tcp.connect((camera_ip, CONTROL_PORT))
        tcp.sendall(build_packet(TYPE_START_VOICE, session_ts))
        ack = parse_packet(tcp.recv(1024))
        ok = bool(ack and ack["type"] == TYPE_ACK and ack["value"] == 0)
        try:
            tcp.sendall(build_packet(TYPE_STOP_VOICE, session_ts))
        except OSError:
            pass
        tcp.close()
        return ok
    except OSError:
        return False
