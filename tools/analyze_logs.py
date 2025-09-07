#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple

# Defaults (can be overridden by env or CLI)
DEFAULT_TTS_LOG = os.getenv("HUME_TTS_LOG_PATH", "./logs/hume-tts.log")
DEFAULT_HA_LOG = os.getenv("HA_LOG_PATH", "/home/jon/smart-home-setup/homeassistant/home-assistant.log")

HELP = """
Analyze latest TTS + Home Assistant logs and summarize timing.

Usage:
  python tools/analyze_logs.py [--tts PATH] [--ha PATH] [--assert-first-audio-ms N]

Environment variables:
  HUME_TTS_LOG_PATH  - default path to TTS log (default: ./logs/hume-tts.log)
  HA_LOG_PATH        - default path to HA log (default: /home/jon/smart-home-setup/homeassistant/home-assistant.log)
  HUME_TTS_MAX_FIRST_AUDIO_MS - if set, assert that first-token -> first-audio latency is <= this many ms
""".strip()

def parse_ts_prefix(line: str) -> Optional[datetime]:
    # 2025-09-07 15:25:10.498 or 2025-09-07 15:25:10,498
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d{3,6})?)", line)
    if not m:
        return None
    ts_str = m.group(1).replace(',', '.')
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts_str, fmt)
        except Exception:
            pass
    return None

def last_match(pat: str, text: str) -> Optional[re.Match]:
    matches = list(re.finditer(pat, text, re.M))
    return matches[-1] if matches else None

def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="ignore") if path.exists() else ""
    except Exception:
        return ""


def find_ha_log_fallback() -> Optional[Path]:
    # Try to locate an HA log under /home/jon if default not present
    base = Path("/home/jon")
    try:
        candidates = list(base.rglob("home-assistant.log"))
        if not candidates:
            candidates = list(base.rglob("home-assistant*.log"))
        if candidates:
            # pick the most recently modified
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return candidates[0]
    except Exception:
        pass
    return None


def analyze(tts_path: Path, ha_path: Optional[Path], assert_ms: Optional[int] = None) -> int:
    tts_text = read_text(tts_path)
    if not tts_text:
        print(f"ERROR: No TTS log content at {tts_path}")
        return 1

    # Identify last synth session by last "Synthesizing text" marker or last t_first_chunk_sent marker
    m_synth = last_match(r"^\d{4}-\d{2}-\d{2} .* - DEBUG - Synthesizing text ", tts_text)
    synth_ts = None
    window = None
    if m_synth:
        window = tts_text[m_synth.start():]
        synth_line = window.splitlines()[0]
        synth_ts = parse_ts_prefix(synth_line)
    else:
        # Fallback: use last t_first_chunk_sent line as anchor window
        m_first_chunk_line = last_match(r"^\d{4}-\d{2}-\d{2} .* - DEBUG - t_first_chunk_sent=\d+ms", tts_text)
        if not m_first_chunk_line:
            print("ERROR: No synth sessions or first chunk markers found in TTS log.")
            return 2
        anchor = m_first_chunk_line.start()
        window = tts_text[max(0, anchor - 40000): anchor + 2000]
        # synth_ts may be absent; we'll compute absolute first-audio time directly from this line

    def get_num(p: str) -> Optional[int]:
        m = re.search(p, window)
        return int(m.group(1)) if m else None

    def get_str(p: str) -> Optional[str]:
        m = re.search(p, window)
        return m.group(1) if m else None

    segmented = get_str(r"Segmented input into (\d+) parts")
    content_type = get_str(r"content_type=([^\s]+)")
    t_connect = get_num(r"t_connect=(\d+)ms")
    t_first_sse = get_num(r"t_first_sse=(\d+)ms")
    t_first_pcm = get_num(r"t_first_pcm=(\d+)ms")
    t_audio_start_sent = get_num(r"t_audio_start_sent=(\d+)ms")
    t_first_chunk_sent = get_num(r"t_first_chunk_sent=(\d+)ms")
    # Detect persistent WAV decoder presence
    used_persist_wav = bool(last_match(r"Using persistent WAV PCM pipeline", tts_text))

    # HA chat arrival: last Adding assistant content entry and first token
    chat_ts = None
    first_token_ts = None
    delta_count = 0
    ha_tail = None
    if ha_path is not None:
        ha_text = read_text(ha_path)
        if ha_text:
            m_chat = last_match(r"^\d{4}-\d{2}-\d{2} .*homeassistant\.components\.conversation\.chat_log.*Adding assistant content:(.*)$", ha_text)
            if m_chat:
                chat_ts = parse_ts_prefix(m_chat.group(0))
                try:
                    # Extract text after the colon for a tail comparison
                    content_after = m_chat.group(1).strip()
                    if content_after:
                        words = re.findall(r"\S+", content_after)
                        tail_words = words[-12:] if words else []
                        ha_tail = ("... " + " ".join(tail_words)) if len(words) > 12 else " ".join(tail_words)
                except Exception:
                    ha_tail = None
                # Search backward window for deltas
                window_text = ha_text[max(0, m_chat.start() - 20000): m_chat.start()]
                delta_matches = list(re.finditer(r"^\d{4}-\d{2}-\d{2} .*homeassistant\.components\.conversation\.chat_log\] Received delta:", window_text, re.M))
                delta_count = len(delta_matches)
                if delta_matches:
                    # Find the first such line in the window for first_token_ts
                    for line in window_text.splitlines():
                        if "homeassistant.components.conversation.chat_log" in line and "Received delta:" in line:
                            first_token_ts = parse_ts_prefix(line)
                            break

    # Always compute first-audio absolute timestamp from the log line timestamp of t_first_chunk_sent
    first_audio_ts = None
    m_abs = last_match(r"^(\d{4}-\d{2}-\d{2} .*?) .* - DEBUG - t_first_chunk_sent=\d+ms", tts_text)
    if m_abs:
        first_audio_ts = parse_ts_prefix(m_abs.group(1))

    def fmt_dt(d: Optional[datetime]) -> str:
        return d.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if d else "N/A"

    print("Latest session timing summary:")
    print(f"- Segments: {segmented or 'N/A'}")
    print(f"- content_type: {content_type or 'N/A'}")
    print(f"- synth_start: {fmt_dt(synth_ts)} (raw)")
    print(f"- t_connect: {t_connect if t_connect is not None else 'N/A'} ms")
    print(f"- t_first_sse: {t_first_sse if t_first_sse is not None else 'N/A'} ms")
    print(f"- t_first_pcm: {t_first_pcm if t_first_pcm is not None else 'N/A'} ms")
    print(f"- t_audio_start_sent: {t_audio_start_sent if t_audio_start_sent is not None else 'N/A'} ms")
    print(f"- t_first_chunk_sent: {t_first_chunk_sent if t_first_chunk_sent is not None else 'N/A'} ms")
    if used_persist_wav:
        print(f"- decoder: persistent_wav")

    # Tails for cut-off verification (from TTS log)
    final_tail = None
    m_final_tail = last_match(r"^\d{4}-\d{2}-\d{2} .* - DEBUG - Final combined text tail: (.*)$", tts_text)
    if m_final_tail:
        final_tail = m_final_tail.group(1).strip()
    seg_tails = re.findall(r"^\d{4}-\d{2}-\d{2} .* - DEBUG - Eager: streaming segment len=\d+ tail=(.*)$", tts_text, re.M)
    recent_seg_tails = seg_tails[-3:] if seg_tails else []

    print("\nCut-off verification tails (TTS):")
    print(f"- Final combined text tail: {final_tail or 'N/A'}")
    for i, t in enumerate(recent_seg_tails, 1):
        print(f"- Segment tail [{len(seg_tails)-len(recent_seg_tails)+i}/{len(seg_tails)}]: {t}")

    print("\nCross-system (HA -> first audio):")
    print(f"- HA chat arrival (final content): {fmt_dt(chat_ts)}")
    print(f"- HA first token received: {fmt_dt(first_token_ts)} (deltas={delta_count})")
    # first_audio_ts computed above from absolute log line timestamp
    failure = False
    if chat_ts and first_audio_ts:
        d1 = (first_audio_ts - chat_ts).total_seconds()
        print(f"- ChatGPT (final text) -> first audio: {d1:.3f} s")
        if first_token_ts:
            d2 = (first_audio_ts - first_token_ts).total_seconds()
            print(f"- ChatGPT (first token) -> first audio: {d2:.3f} s")
            if assert_ms is not None:
                thr_s = assert_ms / 1000.0
                if d2 <= thr_s:
                    print(f"ASSERT first-token->first-audio <= {assert_ms}ms: PASS ({d2*1000:.0f}ms)")
                else:
                    print(f"ASSERT first-token->first-audio <= {assert_ms}ms: FAIL ({d2*1000:.0f}ms)")
                    failure = True
    else:
        print("- Not enough markers to compute ChatGPT->first audio.")

    return 0 if not failure else 10

def main(argv: list[str]) -> int:
    if "-h" in argv or "--help" in argv:
        print(HELP)
        return 0
    tts_arg: Optional[str] = None
    ha_arg: Optional[str] = None
    assert_ms_env = os.getenv("HUME_TTS_MAX_FIRST_AUDIO_MS")
    try:
        default_assert_ms: Optional[int] = int(assert_ms_env) if assert_ms_env else None
    except Exception:
        default_assert_ms = None
    assert_ms_arg: Optional[int] = None
    i = 0
    while i < len(argv):
        if argv[i] == "--tts" and i + 1 < len(argv):
            tts_arg = argv[i + 1]
            i += 2
        elif argv[i] == "--ha" and i + 1 < len(argv):
            ha_arg = argv[i + 1]
            i += 2
        elif argv[i] == "--assert-first-audio-ms" and i + 1 < len(argv):
            try:
                assert_ms_arg = int(argv[i + 1])
            except Exception:
                assert_ms_arg = None
            i += 2
        else:
            i += 1

    tts_path = Path(tts_arg or DEFAULT_TTS_LOG).resolve()
    ha_path: Optional[Path]
    if ha_arg:
        ha_path = Path(ha_arg).resolve()
    else:
        p = Path(DEFAULT_HA_LOG)
        ha_path = p if p.exists() else find_ha_log_fallback()

    rc = analyze(tts_path, ha_path, assert_ms=(assert_ms_arg if assert_ms_arg is not None else default_assert_ms))
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
