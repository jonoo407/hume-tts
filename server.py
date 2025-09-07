__version__ = "0.95"
#!/usr/bin/env python3
"""
Hume AI TTS Wyoming Protocol Server
Provides streaming text-to-speech using Hume AI's Octave TTS with custom voices.
"""

import argparse
import asyncio
import io
import json
import logging
import re
from logging.handlers import RotatingFileHandler
import sys
import os
import socket
import struct
from typing import Dict, List, Optional, Union
from uuid import UUID

import aiohttp
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.info import (
    Attribution,
    Describe,
    Info,
    TtsProgram,
    TtsVoice,
)
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import (
    Synthesize,
    SynthesizeStart,
    SynthesizeChunk,
    SynthesizeStop,
    SynthesizeStopped,
    SynthesizeVoice,
)

# For audio conversion
import wave
import tempfile
import subprocess
import time

_LOGGER = logging.getLogger(__name__)


class HumeTtsEventHandler(AsyncEventHandler):
    """Event handler for Hume AI TTS requests."""
    
    def __init__(
        self,
        reader,
        writer,
        hume_api_key: str,
        hume_secret_key: str,
        default_voice_id: str,
        sample_rate: int = 22050,
        sample_width: int = 2,  # 16-bit = 2 bytes
        channels: int = 1,
        wyoming_info = None,
        stream_format: str = "mp3",
        instant_mode: bool = False,
        samples_per_chunk: int = 512,
        voice_map: Optional[Dict[str, Dict[str, str]]] = None,
        id_provider_map: Optional[Dict[str, str]] = None,
        *args,
        **kwargs,
    ):
        super().__init__(reader, writer, *args, **kwargs)
        
        self.hume_api_key = hume_api_key
        self.hume_secret_key = hume_secret_key
        self.default_voice_id = default_voice_id
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels
        self.wyoming_info = wyoming_info
        self.stream_format = stream_format.lower().strip()
        self.instant_mode = instant_mode
        self.samples_per_chunk = int(samples_per_chunk) if samples_per_chunk and samples_per_chunk > 0 else 512
        # Map from friendly voice name (lowercased) -> spec {provider, key_type, value}
        self.voice_map: Dict[str, Dict[str, str]] = {}
        if voice_map:
            try:
                for k, v in voice_map.items():
                    if isinstance(v, dict):
                        self.voice_map[str(k).lower()] = {
                            "provider": str(v.get("provider", "CUSTOM_VOICE")),
                            "key_type": str(v.get("key_type", "id")),
                            "value": str(v.get("value", "")),
                        }
            except Exception:
                pass
        # Map from UUID (id) -> provider, used when user specifies a raw UUID
        self.id_provider_map: Dict[str, str] = {}
        if id_provider_map:
            try:
                for k, v in id_provider_map.items():
                    self.id_provider_map[str(k)] = str(v)
            except Exception:
                pass
        # Keep a direct reference to the StreamWriter for explicit drains and socket tweaks
        self._writer = writer
        # Logging verbosity controls (env)
        def _env_bool(name: str, default: bool = False) -> bool:
            try:
                val = os.environ.get(name, str(default))
                return str(val).strip().lower() in ("1", "true", "yes", "on")
            except Exception:
                return default
        self.log_chunks: bool = _env_bool("HUME_TTS_LOG_CHUNKS", False)
        self.log_events: bool = _env_bool("HUME_TTS_LOG_EVENTS", False)
        self.log_ffmpeg: bool = _env_bool("HUME_TTS_LOG_FFMPEG", False)
        # Streaming input session state (for SynthesizeStart/Chunk/Stop)
        self._streaming_in: bool = False
        self._streaming_text_parts: List[str] = []
        self._streaming_voice: Optional[SynthesizeVoice] = None
        self._streaming_session_start_ts: Optional[float] = None
        self._streaming_first_input_ts: Optional[float] = None
        # Eager streaming configuration/state
        self.eager_streaming: bool = _env_bool("HUME_TTS_EAGER_STREAMING", True)
        def _env_int_default(name: str, default: int) -> int:
            try:
                return int(str(os.environ.get(name, default)).strip())
            except Exception:
                return default
        self.first_segment_target_len: int = _env_int_default("HUME_TTS_FIRST_SEGMENT_LEN", 160)
        self.first_segment_min_len: int = _env_int_default("HUME_TTS_FIRST_SEGMENT_MIN", 80)
        self.next_segment_target_len: int = _env_int_default("HUME_TTS_SEGMENT_LEN", 240)
        self._eager_queue: Optional[asyncio.Queue] = None
        self._eager_task: Optional[asyncio.Task] = None
        self._eager_buffer: str = ""
        self._eager_first_done: bool = False
        self._eager_stop_requested: bool = False
        # Eager flush mode: 'sentence' (flush on sentence boundaries) or 'hold_after_first'
        try:
            self.eager_flush_mode: str = str(os.environ.get("HUME_TTS_EAGER_FLUSH_MODE", "sentence")).strip().lower()
        except Exception:
            self.eager_flush_mode = "sentence"
        # Persistent WAV decode/resample flag (default off; use per-chunk if False)
        def _env_bool_default(name: str, default: bool) -> bool:
            try:
                v = os.environ.get(name, str(default))
                return str(v).strip().lower() in ("1","true","yes","on")
            except Exception:
                return default
        self.persistent_wav: bool = _env_bool_default("HUME_TTS_PERSISTENT_WAV", False)
        self.word_wrap_segmentation: bool = _env_bool_default("HUME_TTS_WORD_WRAP_SEGMENTATION", False)
        self.force_per_chunk: bool = _env_bool_default("HUME_TTS_FORCE_PER_CHUNK", False)
        # Add a small silence pad at end to avoid client clipping the tail
        try:
            self.end_silence_ms: int = _env_int_default("HUME_TTS_END_SILENCE_MS", 250)
        except Exception:
            self.end_silence_ms = 250
        # Disable Nagle's algorithm to reduce latency for small AudioChunk frames
        try:
            sock_obj = writer.get_extra_info('socket')
            if isinstance(sock_obj, socket.socket):
                sock_obj.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # Try to enable QUICKACK when available (Linux-specific); ignore if unsupported
                try:
                    quickack = getattr(socket, "TCP_QUICKACK", None)
                    if quickack is not None:
                        sock_obj.setsockopt(socket.IPPROTO_TCP, quickack, 1)
                except Exception:
                    pass
        except Exception:
            pass

        # Audio conversion
        self.converter = AudioChunkConverter(
            rate=sample_rate,
            width=sample_width,
            channels=channels,
        )

    async def handle_event(self, event) -> bool:
        """Handle incoming Wyoming protocol events."""
        try:
            _LOGGER.debug(f"Event received: {getattr(event, 'type', 'unknown')}")
        except Exception:
            pass
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info.event())
            # Ensure immediate flush so simple clients don't hang waiting
            try:
                await self._drain_writer()
            except Exception:
                pass
            _LOGGER.debug("Describe handled: sent Info")
            return True
        elif SynthesizeStart.is_type(event.type):
            start = SynthesizeStart.from_event(event)
            await self.handle_synthesize_start(start)
            return True
        elif SynthesizeChunk.is_type(event.type):
            chunk = SynthesizeChunk.from_event(event)
            await self.handle_synthesize_chunk(chunk)
            return True
        elif SynthesizeStop.is_type(event.type):
            stop = SynthesizeStop.from_event(event)
            await self.handle_synthesize_stop(stop)
            return True
        elif Synthesize.is_type(event.type):
            # If we're in a streaming session, ignore the fallback Synthesize event
            if self._streaming_in:
                _LOGGER.debug("Ignoring fallback Synthesize during active streaming session")
                return True
            await self.handle_synthesize(Synthesize.from_event(event))
            return True
        
        return True

    async def _drain_writer(self) -> None:
        """Explicitly drain the StreamWriter to flush small frames immediately."""
        try:
            if getattr(self, "_writer", None) is not None and hasattr(self._writer, "drain"):
                await self._writer.drain()
        except Exception:
            # Draining is best-effort; ignore errors to avoid breaking stream
            pass


    def _segment_text(self, text: str) -> List[str]:
        """Split long text into sentence-sized chunks to minimize time-to-first-audio.

        Heuristic: split on . ! ? boundaries and wrap to ~240 chars max.
        """
        try:
            parts = re.split(r'(?<=[.!?])\s+', (text or '').strip())
            out: List[str] = []
            # Use configurable targets; fallback to defaults when not in eager mode
            first_max = getattr(self, 'first_segment_target_len', 160)
            max_len = getattr(self, 'next_segment_target_len', 240)
            first_done = False
            if self.word_wrap_segmentation:
                for sent in parts:
                    if not sent:
                        continue
                    limit = first_max if not first_done else max_len
                    # Word-wrap this sentence into chunks up to limit, avoiding mid-word splits
                    words = re.findall(r"\S+", sent)
                    cur = ""
                    for w in words:
                        if not cur:
                            cur = w
                        elif len(cur) + 1 + len(w) <= limit:
                            cur = f"{cur} {w}"
                        else:
                            out.append(cur)
                            first_done = True
                            cur = w
                    if cur:
                        out.append(cur)
                        first_done = True
            else:
                # Previous simple char-based accumulation across sentences
                buf = ""
                for p in parts:
                    if not p:
                        continue
                    limit = first_max if not first_done else max_len
                    if len(p) > limit:
                        # split sentence across limit-sized chunks (may split words)
                        for i in range(0, len(p), limit):
                            out.append(p[i:i+limit])
                            first_done = True
                        continue
                    if not buf:
                        buf = p
                    elif len(buf) + 1 + len(p) <= limit:
                        buf = f"{buf} {p}"
                    else:
                        out.append(buf)
                        first_done = True
                        buf = p
                if buf:
                    out.append(buf)
            return out or [text]
        except Exception:
            return [text]

    async def handle_synthesize(self, synthesize: Synthesize) -> None:
        """Handle TTS synthesis request."""
        try:
            _LOGGER.debug(f"Synthesizing text ({len(synthesize.text)} chars): {synthesize.text[:50]}...")
            start_ts = time.monotonic()
            _LOGGER.debug(f"Config: rate={self.sample_rate} width={self.sample_width} ch={self.channels} fmt={self.stream_format} instant={self.instant_mode} spc={self.samples_per_chunk}")
            
            # Resolve voice: map friendly name or UUID to provider-aware spec
            voice_spec: Dict[str, str] = {
                "provider": "CUSTOM_VOICE",
                "key_type": "id",
                "value": self.default_voice_id,
            }
            if synthesize.voice and synthesize.voice.name:
                requested = str(synthesize.voice.name).strip()
                def looks_like_uuid(x: str) -> bool:
                    try:
                        UUID(x)
                        return True
                    except Exception:
                        return False
                if looks_like_uuid(requested):
                    provider_guess = self.id_provider_map.get(requested, "CUSTOM_VOICE")
                    voice_spec = {"provider": provider_guess, "key_type": "id", "value": requested}
                    _LOGGER.debug(f"Using explicit voice UUID override: id={requested} provider={provider_guess}")
                else:
                    mapped = None
                    try:
                        mapped = self.voice_map.get(requested.lower()) if hasattr(self, 'voice_map') else None
                    except Exception:
                        mapped = None
                    if mapped:
                        voice_spec = {
                            "provider": mapped.get("provider", "CUSTOM_VOICE"),
                            "key_type": mapped.get("key_type", "id"),
                            "value": mapped.get("value"),
                        }
                        _LOGGER.debug(f"Mapped voice name '{requested}' -> spec {voice_spec}")
                    else:
                        _LOGGER.debug(f"Unknown voice name '{requested}', using default spec {voice_spec}")
            
            _LOGGER.debug(f"Using voice_spec resolved: provider={voice_spec['provider']} {voice_spec['key_type']}={voice_spec['value']}")
            emitted = False
            sent_audio_start = False
            # Metrics for writer/drain timings
            first_chunk_sent_ts = None
            drain_count = 0
            drain_total_ms = 0.0
            drain_max_ms = 0.0
            
            # Try streaming TTS first (segment long inputs for faster first audio)
            segments = self._segment_text(synthesize.text)
            if len(segments) > 1:
                _LOGGER.debug(f"Segmented input into {len(segments)} parts (<=~240 chars)")
            for seg in segments:
                async for pcm_chunk in self._stream_hume_tts(seg, voice_spec):
                    # Convert and send audio chunk
                    if not sent_audio_start:
                        await self.write_event(
                            AudioStart(
                                rate=self.sample_rate,
                                width=self.sample_width,
                                channels=self.channels,
                            ).event()
                        )
                        await self._drain_writer()
                        sent_audio_start = True
                        try:
                            _LOGGER.debug(f"t_audio_start_sent={int((time.monotonic()-start_ts)*1000)}ms")
                        except Exception:
                            pass
                    chunk = AudioChunk(
                        audio=pcm_chunk,
                        rate=self.sample_rate,
                        width=self.sample_width,
                        channels=self.channels,
                    )
                    send_start = time.monotonic()
                    await self.write_event(chunk.event())
                    await self._drain_writer()
                    send_ms = (time.monotonic() - send_start) * 1000.0
                    if first_chunk_sent_ts is None:
                        first_chunk_sent_ts = send_start
                        _LOGGER.debug(f"t_first_chunk_sent={int((first_chunk_sent_ts - start_ts)*1000)}ms")
                    drain_total_ms += send_ms
                    drain_count += 1
                    if send_ms > drain_max_ms:
                        drain_max_ms = send_ms
                    emitted = True
            
            # Fallback: non-streaming one-shot if no audio was emitted
            if not emitted:
                _LOGGER.debug("No audio emitted from stream; attempting one-shot synthesis fallback")
                pcm_data = await self._synthesize_hume_tts_once(synthesize.text, voice_spec)
                if pcm_data:
                    if not sent_audio_start:
                        await self.write_event(
                            AudioStart(
                                rate=self.sample_rate,
                                width=self.sample_width,
                                channels=self.channels,
                            ).event()
                        )
                        await self._drain_writer()
                        sent_audio_start = True
                    chunk = AudioChunk(
                        audio=pcm_data,
                        rate=self.sample_rate,
                        width=self.sample_width,
                        channels=self.channels,
                    )
                    await self.write_event(chunk.event())
                    await self._drain_writer()
                    emitted = True
                else:
                    _LOGGER.warning("Fallback one-shot synthesis produced no audio")
            
            # End audio stream
            if not sent_audio_start:
                # Ensure protocol correctness: send an empty start before stop
                await self.write_event(
                    AudioStart(
                        rate=self.sample_rate,
                        width=self.sample_width,
                        channels=self.channels,
                    ).event()
                )
                await self._drain_writer()
            # Optional end-of-stream silence padding to prevent abrupt cutoff
            if sent_audio_start and getattr(self, "end_silence_ms", 0) > 0:
                pad_samples = int(self.sample_rate * self.end_silence_ms / 1000)
                if pad_samples > 0:
                    pad_bytes = pad_samples * self.sample_width * self.channels
                    try:
                        await self.write_event(
                            AudioChunk(
                                audio=b"\x00" * pad_bytes,
                                rate=self.sample_rate,
                                width=self.sample_width,
                                channels=self.channels,
                            ).event()
                        )
                        await self._drain_writer()
                        drain_count += 1
                    except Exception:
                        pass
            await self.write_event(AudioStop().event())
            await self._drain_writer()
            # Report writer/drain timings
            if drain_count:
                avg_ms = drain_total_ms / max(drain_count, 1)
                _LOGGER.debug(f"Wyoming write summary: chunks={drain_count} avg_send_ms={avg_ms:.1f} max_send_ms={drain_max_ms:.1f}")
            
        except Exception as e:
            _LOGGER.error(f"Error during synthesis: {e}")
            # Send empty audio stream to indicate failure
            await self.write_event(
                AudioStart(
                    rate=self.sample_rate,
                    width=self.sample_width,
                    channels=self.channels,
                ).event()
            )
            await self.write_event(AudioStop().event())

    async def handle_synthesize_start(self, start: SynthesizeStart) -> None:
        """Begin a streaming text input session."""
        self._streaming_in = True
        self._streaming_text_parts = []
        self._streaming_voice = start.voice
        try:
            self._streaming_session_start_ts = time.monotonic()
            self._streaming_first_input_ts = None
        except Exception:
            self._streaming_session_start_ts = None
            self._streaming_first_input_ts = None
        if self.eager_streaming:
            # Initialize eager streaming machinery
            self._eager_queue = asyncio.Queue()
            self._eager_task = asyncio.create_task(self._run_eager_stream())
            self._eager_buffer = ""
            self._eager_first_done = False
            self._eager_stop_requested = False
            _LOGGER.debug("Eager streaming mode: initialized background stream task")
        _LOGGER.debug("SynthesizeStart received: initialized streaming input session")

    async def handle_synthesize_chunk(self, chunk: SynthesizeChunk) -> None:
        """Accumulate a chunk of streaming text input."""
        if not self._streaming_in:
            _LOGGER.debug("SynthesizeChunk received outside of streaming session; starting implicit session")
            self._streaming_in = True
            self._streaming_text_parts = []
            self._streaming_voice = None
            try:
                self._streaming_session_start_ts = time.monotonic()
            except Exception:
                self._streaming_session_start_ts = None
            if self.eager_streaming:
                # Initialize eager streaming when chunks arrive without an explicit start
                self._eager_queue = asyncio.Queue()
                self._eager_task = asyncio.create_task(self._run_eager_stream())
                self._eager_buffer = ""
                self._eager_first_done = False
                self._eager_stop_requested = False
                _LOGGER.debug("Eager streaming mode: initialized background stream task (implicit)")
        self._streaming_text_parts.append(chunk.text)
        # Log first input timing relative to session start
        try:
            if self._streaming_first_input_ts is None:
                self._streaming_first_input_ts = time.monotonic()
                if self._streaming_session_start_ts is not None:
                    dt_ms = int((self._streaming_first_input_ts - self._streaming_session_start_ts) * 1000)
                    _LOGGER.debug(f"t_first_input_chunk={dt_ms}ms after SynthesizeStart")
        except Exception:
            pass
        _LOGGER.debug(f"SynthesizeChunk received: +{len(chunk.text)} chars (total={sum(len(p) for p in self._streaming_text_parts)})")
        if self.eager_streaming:
            # Accumulate in eager buffer and flush ready segments
            try:
                self._eager_buffer += chunk.text
                await self._eager_try_flush_segments()
            except Exception as e:
                _LOGGER.warning(f"Eager flush failed: {e}")

    async def handle_synthesize_stop(self, _stop: SynthesizeStop) -> None:
        """Finalize streaming input and perform synthesis, then signal stopped."""
        try:
            total_text = "".join(self._streaming_text_parts) if self._streaming_text_parts else ""
            _LOGGER.debug(f"SynthesizeStop received: finalizing streaming input (chars={len(total_text)})")
            try:
                _LOGGER.debug(f"Final combined text tail: {self._tail_words(total_text, 12)}")
            except Exception:
                pass
            if self.eager_streaming and (self._eager_task is not None) and (self._eager_queue is not None):
                # Flush any remaining buffered text as final segments
                try:
                    if self._eager_buffer.strip():
                        if getattr(self, 'eager_flush_mode', 'sentence') == 'hold_after_first':
                            # Enqueue the entire remainder as a single segment to avoid mid-story gaps
                            s = self._eager_buffer.strip()
                            try:
                                _LOGGER.debug(f"Eager flush final (single) segment_tail: {self._tail_words(s, 8)}")
                            except Exception:
                                pass
                            await self._eager_queue.put(s)
                        else:
                            segs = self._segment_text(self._eager_buffer)
                            for s in segs:
                                try:
                                    _LOGGER.debug(f"Eager flush final segment_tail: {self._tail_words(s, 8)}")
                                except Exception:
                                    pass
                                await self._eager_queue.put(s)
                        self._eager_buffer = ""
                except Exception as e:
                    _LOGGER.warning(f"Eager final flush failed: {e}")
                # Signal end of input to background task and wait for completion
                self._eager_stop_requested = True
                try:
                    await self._eager_queue.put(None)  # sentinel to terminate
                except Exception:
                    pass
                try:
                    await self._eager_task
                except Exception as e:
                    _LOGGER.warning(f"Eager streaming task ended with error: {e}")
            else:
                # Reuse existing synthesis path (non-eager mode)
                await self.handle_synthesize(
                    Synthesize(text=total_text, voice=self._streaming_voice)
                )
        finally:
            # Always send SynthesizeStopped to inform client we're done
            try:
                await self.write_event(SynthesizeStopped().event())
                await self._drain_writer()
            except Exception:
                pass
            # Reset streaming state
            self._streaming_in = False
            self._streaming_text_parts = []
            self._streaming_voice = None
            # Reset eager state
            self._eager_queue = None
            self._eager_task = None
            self._eager_buffer = ""
            self._eager_first_done = False
            self._eager_stop_requested = False

    def _tail_words(self, text: str, n: int = 8) -> str:
        """Return a compact string of the last N words for logging/comparison."""
        try:
            words = re.findall(r"\S+", text or "")
            tail = words[-n:] if words else []
            s = " ".join(tail)
            if len(words) > n:
                s = "... " + s
            return s
        except Exception:
            return ""

    def _resolve_voice_spec(self, voice: Optional[SynthesizeVoice]) -> Dict[str, str]:
        """Resolve a SynthesizeVoice into our internal provider-aware voice spec."""
        voice_spec: Dict[str, str] = {
            "provider": "CUSTOM_VOICE",
            "key_type": "id",
            "value": self.default_voice_id,
        }
        try:
            if voice and voice.name:
                requested = str(voice.name).strip()
                def looks_like_uuid(x: str) -> bool:
                    try:
                        UUID(x)
                        return True
                    except Exception:
                        return False
                if looks_like_uuid(requested):
                    provider_guess = self.id_provider_map.get(requested, "CUSTOM_VOICE")
                    voice_spec = {"provider": provider_guess, "key_type": "id", "value": requested}
                    _LOGGER.debug(f"Using explicit voice UUID override: id={requested} provider={provider_guess}")
                else:
                    mapped = None
                    try:
                        mapped = self.voice_map.get(requested.lower()) if hasattr(self, 'voice_map') else None
                    except Exception:
                        mapped = None
                    if mapped:
                        voice_spec = {
                            "provider": mapped.get("provider", "CUSTOM_VOICE"),
                            "key_type": mapped.get("key_type", "id"),
                            "value": mapped.get("value"),
                        }
                        _LOGGER.debug(f"Mapped voice name '{requested}' -> spec {voice_spec}")
                    else:
                        _LOGGER.debug(f"Unknown voice name '{requested}', using default spec {voice_spec}")
        except Exception:
            pass
        return voice_spec

    async def _run_eager_stream(self) -> None:
        """Background task that consumes ready segments and streams audio immediately.

        Sends a single Wyoming AudioStart followed by AudioChunk frames for all segments,
        then a final AudioStop when the queue is exhausted and stop is requested.
        """
        try:
            queue = self._eager_queue
            if queue is None:
                return
            voice_spec = self._resolve_voice_spec(self._streaming_voice)
            sent_audio_start = False
            emitted = False
            first_chunk_sent_ts = None
            drain_count = 0
            drain_total_ms = 0.0
            drain_max_ms = 0.0
            # Anchor for timing logs
            start_ts = time.monotonic()
            while True:
                seg = await queue.get()
                if seg is None:
                    break
                try:
                    try:
                        _LOGGER.debug(f"Eager: streaming segment len={len(seg)} tail={self._tail_words(seg, 8)}")
                    except Exception:
                        _LOGGER.debug(f"Eager: streaming segment len={len(seg)}")
                    async for pcm_chunk in self._stream_hume_tts(seg, voice_spec):
                        if not sent_audio_start:
                            await self.write_event(
                                AudioStart(
                                    rate=self.sample_rate,
                                    width=self.sample_width,
                                    channels=self.channels,
                                ).event()
                            )
                            await self._drain_writer()
                            sent_audio_start = True
                            try:
                                _LOGGER.debug(f"t_audio_start_sent={int((time.monotonic()-start_ts)*1000)}ms")
                            except Exception:
                                pass
                        chunk = AudioChunk(
                            audio=pcm_chunk,
                            rate=self.sample_rate,
                            width=self.sample_width,
                            channels=self.channels,
                        )
                        send_start = time.monotonic()
                        await self.write_event(chunk.event())
                        await self._drain_writer()
                        send_ms = (time.monotonic() - send_start) * 1000.0
                        if first_chunk_sent_ts is None:
                            first_chunk_sent_ts = send_start
                            _LOGGER.debug(f"t_first_chunk_sent={int((first_chunk_sent_ts - start_ts)*1000)}ms")
                        drain_total_ms += send_ms
                        drain_count += 1
                        if send_ms > drain_max_ms:
                            drain_max_ms = send_ms
                        emitted = True
                except Exception as e:
                    _LOGGER.warning(f"Eager segment streaming failed: {e}")
                    continue
            # End audio stream
            if not sent_audio_start:
                # Ensure protocol correctness: send an empty start before stop
                await self.write_event(
                    AudioStart(
                        rate=self.sample_rate,
                        width=self.sample_width,
                        channels=self.channels,
                    ).event()
                )
                await self._drain_writer()
            # Optional end-of-stream silence padding to prevent abrupt cutoff
            if sent_audio_start and getattr(self, "end_silence_ms", 0) > 0:
                pad_samples = int(self.sample_rate * self.end_silence_ms / 1000)
                if pad_samples > 0:
                    pad_bytes = pad_samples * self.sample_width * self.channels
                    try:
                        await self.write_event(
                            AudioChunk(
                                audio=b"\x00" * pad_bytes,
                                rate=self.sample_rate,
                                width=self.sample_width,
                                channels=self.channels,
                            ).event()
                        )
                        await self._drain_writer()
                        drain_count += 1
                    except Exception:
                        pass
            await self.write_event(AudioStop().event())
            await self._drain_writer()
            if drain_count:
                avg_ms = drain_total_ms / max(drain_count, 1)
                _LOGGER.debug(f"Wyoming write summary: chunks={drain_count} avg_send_ms={avg_ms:.1f} max_send_ms={drain_max_ms:.1f}")
        except Exception as e:
            _LOGGER.warning(f"Eager streaming task error: {e}")

    async def _eager_try_flush_segments(self) -> None:
        """Flush any ready segments from the eager buffer into the queue.

        Strategy:
        - Use sentence-aware segmentation with size limits; flush all but the last (incomplete) piece.
        - If no split occurs and first segment is still pending but buffer >= first_segment_target_len,
          flush the first_segment_target_len as an early first segment to minimize time-to-first-audio.
        """
        if self._eager_queue is None:
            return
        buf = self._eager_buffer
        if not buf:
            return
        # If configured to hold after the first segment, only flush on stop
        if self._eager_first_done and self.eager_flush_mode == "hold_after_first":
            return
        segs = self._segment_text(buf)
        if len(segs) >= 2:
            # Flush all but the last piece, keep the tail in buffer
            for s in segs[:-1]:
                try:
                    _LOGGER.debug(f"Eager segment_tail: {self._tail_words(s, 8)}")
                except Exception:
                    pass
                await self._eager_queue.put(s)
                if not self._eager_first_done:
                    self._eager_first_done = True
            self._eager_buffer = segs[-1]
            return
        # If we haven't emitted the first segment yet, and we have a sentence-ending punctuation
        # beyond a minimum size, flush up to that punctuation even without trailing whitespace.
        if not self._eager_first_done:
            try:
                last_dot = buf.rfind('.')
                last_exc = buf.rfind('!')
                last_q = buf.rfind('?')
                punct_idx = max(last_dot, last_exc, last_q)
                if punct_idx >= (self.first_segment_min_len - 1):
                    first = buf[: punct_idx + 1]
                    try:
                        _LOGGER.debug(f"Eager segment_tail: {self._tail_words(first, 8)}")
                    except Exception:
                        pass
                    await self._eager_queue.put(first)
                    self._eager_buffer = buf[punct_idx + 1: ].lstrip()
                    self._eager_first_done = True
                    return
            except Exception:
                pass
        # No split yet; consider early flush for the first segment
        if (not self._eager_first_done) and (len(buf) >= self.first_segment_target_len):
            first = buf[: self.first_segment_target_len]
            try:
                _LOGGER.debug(f"Eager segment_tail: {self._tail_words(first, 8)}")
            except Exception:
                pass
            await self._eager_queue.put(first)
            self._eager_buffer = buf[self.first_segment_target_len:]
            self._eager_first_done = True
            return

    async def _stream_hume_tts(self, text: str, voice_spec: Dict[str, str]) -> bytes:
        """Stream TTS audio from Hume AI API and convert to PCM."""
        try:
            start_ts = time.monotonic()
            # Prepare Hume API request (NDJSON streaming)
            url = "https://api.hume.ai/v0/tts/stream/json"
            
            headers = {
                "X-Hume-Api-Key": self.hume_api_key,
                "X-Hume-Secret-Key": self.hume_secret_key,
                "Content-Type": "application/json",
                # Known-good: allow SSE or NDJSON; exclude plain JSON to avoid non-streaming fallback
                "Accept": "text/event-stream, application/x-ndjson",
                # Encourage immediate flush/streaming
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Avoid compression buffering on proxies/clients
                "Accept-Encoding": "identity",
            }
            
            fmt: Dict[str, Union[str, int]] = {"type": self.stream_format}
            if self.stream_format == "wav":
                # Ask Hume to produce WAV close to our output spec
                fmt.update({
                    "sample_rate": self.sample_rate,
                    "channels": self.channels,
                })
            # Build provider-aware voice payload
            provider = voice_spec.get("provider", "CUSTOM_VOICE")
            key_type = voice_spec.get("key_type", "id")
            value = voice_spec.get("value")
            # Do not include provider in payload; Hume expects only id or name
            voice_payload: Dict[str, str] = {}
            if key_type == "name":
                voice_payload["name"] = value
            else:
                voice_payload["id"] = value

            payload = {
                "utterances": [{
                    "text": text,
                    "voice": voice_payload,
                }],
                # instant_mode only at root; per-utterance causes 422 errors
                "instant_mode": self.instant_mode,
                "format": fmt,
            }
            
            _LOGGER.debug(f"Sending request to Hume API with voice_spec: provider={provider} {key_type}={value}")
            
            # Stream response from Hume AI
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise Exception(f"Hume API error {response.status}: {error_text}")
                    
                    _LOGGER.debug("Connected to Hume AI streaming API")
                    try:
                        _LOGGER.debug(f"Hume response headers: {dict(response.headers)}")
                    except Exception:
                        pass
                    content_type = (response.headers.get('Content-Type') or '').lower()
                    is_sse = 'text/event-stream' in content_type
                    _LOGGER.debug(f"content_type={content_type} is_sse={is_sse}")
                    _LOGGER.debug(f"t_connect={int((time.monotonic()-start_ts)*1000)}ms")
                    
                    # Helper: start a persistent transcoder process for streaming
                    async def start_transcoder(input_format: Optional[str], in_rate: Optional[int] = None, in_channels: Optional[int] = None):
                        args = [
                            'ffmpeg', '-hide_banner', '-loglevel', 'error',
                        ]
                        if input_format:
                            args += ['-f', input_format]
                            # For raw PCM formats, specify input rate/channels
                            if input_format in {'s16le','s24le','s32le','f32le','f64le'}:
                                if in_rate:
                                    args += ['-ar', str(in_rate)]
                                if in_channels:
                                    args += ['-ac', str(in_channels)]
                        # Common low-latency options
                        args += [
                            '-probesize', '32k', '-analyzeduration', '0',
                            '-fflags', 'nobuffer',
                            '-flags', 'low_delay',
                            '-i', 'pipe:0',
                        ]
                        args += [
                            '-f', 's16le', '-ar', str(self.sample_rate), '-ac', str(self.channels), 'pipe:1',
                        ]
                        return await asyncio.create_subprocess_exec(
                            *args,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )

                    async def stdout_reader(proc, q: asyncio.Queue):
                        try:
                            while True:
                                # smaller read size for lower end-to-end latency
                                read_size = max(1024, self.samples_per_chunk * self.sample_width * self.channels)
                                data = await proc.stdout.read(read_size)
                                if not data:
                                    break
                                await q.put(data)
                        except Exception:
                            pass

                    async def stderr_reader(proc):
                        try:
                            while True:
                                line = await proc.stderr.readline()
                                if not line:
                                    break
                                if self.log_ffmpeg:
                                    _LOGGER.debug(f"ffmpeg: {line.decode(errors='ignore').rstrip()}")
                        except Exception:
                            pass

                    # Process streaming JSON responses (SSE/NDJSON-safe)
                    text_buffer = ""
                    transcoder = None
                    stdout_task = None
                    stderr_task = None
                    pcm_queue: asyncio.Queue = asyncio.Queue()
                    chunk_buf = b''
                    frame_bytes = self.sample_width * self.channels
                    chunk_bytes = self.samples_per_chunk * frame_bytes
                    first_sse_ts = None
                    first_pcm_ts = None
                    chosen_input_fmt = None
                    # For WAV input, prebuffer to ensure we feed a complete header
                    wav_prebuffer = bytearray()
                    wav_header_needed = 44  # typical minimal RIFF header size
                    wav_per_chunk = False
                    mp3_per_chunk = False
                    is_wav_mode = False
                    wav_in_rate = None
                    wav_in_channels = None
                    wav_in_bits = None
                    # Diagnostics
                    sse_events = 0
                    audio_events = 0
                    bytes_in_total = 0
                    pcm_out_total = 0
                    emitted_chunks = 0

                    def _extract_audio_bytes(obj) -> Optional[bytes]:
                        """Extract audio bytes from various possible event shapes.
                        Supports keys: audio, audio_base64, audioBytes/audio_bytes (list of ints),
                        nested in data/chunk/payload. Returns None if not found.
                        """
                        import base64
                        if not isinstance(obj, (dict, list)):
                            return None
                        # direct fields in dict
                        if isinstance(obj, dict):
                            # Base64 string variants
                            for k in ("audio", "audio_base64", "audioB64", "b64_audio", "audioB64_base64"):
                                v = obj.get(k)
                                if isinstance(v, str):
                                    s = v
                                    if s.startswith('data:'):
                                        idx = s.find('base64,')
                                        if idx != -1:
                                            s = s[idx+7:]
                                    try:
                                        return base64.b64decode(s)
                                    except Exception:
                                        pass
                            # Byte list variants
                            for k in ("audio_bytes", "audioBytes"):
                                v = obj.get(k)
                                if isinstance(v, list) and v and all(isinstance(x, int) for x in v):
                                    try:
                                        return bytes(v)
                                    except Exception:
                                        pass
                            # Common containers
                            for k in ("data", "chunk", "payload", "utterance", "event"):
                                v = obj.get(k)
                                if isinstance(v, (dict, list)):
                                    b = _extract_audio_bytes(v)
                                    if b:
                                        return b
                        # list: search items
                        if isinstance(obj, list):
                            for it in obj:
                                b = _extract_audio_bytes(it)
                                if b:
                                    return b
                        return None

                    async for raw in response.content.iter_any():
                        # Decode to text and normalize line endings
                        try:
                            part = raw.decode('utf-8', errors='ignore')
                        except Exception:
                            continue
                        part = part.replace('\r\n', '\n').replace('\r', '\n')
                        text_buffer += part

                        # Determine event boundary: SSE uses double newline, NDJSON uses single newline per JSON
                        delimiter = '\n\n' if is_sse else '\n'
                        while delimiter in text_buffer:
                            event_text, text_buffer = text_buffer.split(delimiter, 1)
                            event_text = event_text.strip()
                            if not event_text:
                                continue

                            if is_sse:
                                # Concatenate all data: lines per SSE spec
                                data_lines: List[str] = []
                                for s in event_text.split('\n'):
                                    s = s.strip()
                                    if not s:
                                        continue
                                    if s.startswith('data:'):
                                        data_lines.append(s[5:].lstrip())
                                if not data_lines:
                                    continue
                                body = '\n'.join(data_lines)
                            else:
                                body = event_text

                            # Parse JSON body (accumulate if incomplete)
                            try:
                                chunk_data = json.loads(body)
                            except json.JSONDecodeError:
                                # Put back and wait for more
                                text_buffer = body + (('\n' + text_buffer) if text_buffer else '')
                                break

                            # Log structure of each event (safe: no large payloads)
                            if isinstance(chunk_data, dict):
                                try:
                                    keys = list(chunk_data.keys())
                                    etype = chunk_data.get('type')
                                    dkeys = list(chunk_data.get('data', {}).keys()) if isinstance(chunk_data.get('data'), dict) else None
                                    ckeys = list(chunk_data.get('chunk', {}).keys()) if isinstance(chunk_data.get('chunk'), dict) else None
                                    if first_sse_ts is None:
                                        first_sse_ts = time.monotonic()
                                        _LOGGER.debug(f"t_first_sse={int((first_sse_ts-start_ts)*1000)}ms")
                                    if self.log_events:
                                        _LOGGER.debug(f"Hume stream event: keys={keys} type={etype} data.keys={dkeys} chunk.keys={ckeys}")
                                except Exception:
                                    pass
                                sse_events += 1

                            # Extract audio bytes (supports many shapes)
                            audio_bytes = _extract_audio_bytes(chunk_data)

                            if audio_bytes:
                                audio_events += 1
                                encoded = audio_bytes

                                bytes_in_total += len(encoded)

                                # Lazily start transcoder when first audio arrives
                                if transcoder is None:
                                    # Prefer audio_format from payload if present
                                    input_fmt_field = None
                                    try:
                                        input_fmt_field = chunk_data.get('audio_format')
                                    except Exception:
                                        pass
                                    if isinstance(input_fmt_field, str):
                                        lf = input_fmt_field.lower()
                                        if 'mp3' in lf:
                                            chosen_input_fmt = 'mp3'
                                        elif 'wav' in lf:
                                            chosen_input_fmt = 'wav'
                                        elif 'mpeg' in lf:
                                            chosen_input_fmt = 'mp3'
                                        else:
                                            chosen_input_fmt = None
                                    else:
                                        chosen_input_fmt = 'mp3' if self.stream_format != 'wav' else 'wav'

                                    if chosen_input_fmt == 'wav':
                                        if self.persistent_wav and not self.force_per_chunk:
                                            # Use persistent WAV pipeline: extract PCM from each WAV and resample once if needed
                                            wav_per_chunk = False
                                            is_wav_mode = True
                                            _LOGGER.debug("Using persistent WAV PCM pipeline (fast-path when possible)")
                                        else:
                                            # Revert to per-chunk decode (historical behavior)
                                            wav_per_chunk = True
                                            is_wav_mode = False
                                            _LOGGER.debug("Using per-chunk WAV decode (one ffmpeg per event)")
                                    else:
                                        if self.force_per_chunk:
                                            mp3_per_chunk = True
                                            _LOGGER.debug("Using per-chunk MP3 decode (one ffmpeg per event)")
                                        else:
                                            transcoder = await start_transcoder(chosen_input_fmt)
                                            _LOGGER.debug(f"Started transcoder input_fmt={chosen_input_fmt}")
                                            stdout_task = asyncio.create_task(stdout_reader(transcoder, pcm_queue))
                                            stderr_task = asyncio.create_task(stderr_reader(transcoder))

                                # Handle current audio block
                                if wav_per_chunk:
                                    # Per-chunk WAV conversion (historical behavior)
                                    pcm = None
                                    fast_pcm = self._extract_wav_pcm_fast(encoded)
                                    if fast_pcm is not None:
                                        pcm = fast_pcm
                                    else:
                                        pcm = await self._convert_wav_to_pcm(encoded)
                                    if pcm:
                                        if first_pcm_ts is None:
                                            first_pcm_ts = time.monotonic()
                                            _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                        chunk_buf += pcm
                                        while len(chunk_buf) >= chunk_bytes:
                                            part_pcm = chunk_buf[:chunk_bytes]
                                            chunk_buf = chunk_buf[chunk_bytes:]
                                            if self.log_chunks:
                                                _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                            pcm_out_total += len(part_pcm)
                                            emitted_chunks += 1
                                            yield part_pcm
                                    else:
                                        _LOGGER.warning("WAV per-chunk conversion returned no PCM")
                                elif is_wav_mode:
                                    # Extract PCM payload and either fast-path or resample via persistent transcoder
                                    pcm_payload = None
                                    try:
                                        info = self._extract_wav_pcm_with_info(encoded)
                                    except Exception:
                                        info = None
                                    if info is not None:
                                        pcm_payload, rate, ch, bits = info
                                        # Cache input format
                                        if wav_in_rate is None:
                                            wav_in_rate, wav_in_channels, wav_in_bits = rate, ch, bits
                                        # Fast path when format matches output exactly
                                        if (rate == self.sample_rate) and (ch == self.channels) and (bits == 16):
                                            if first_pcm_ts is None:
                                                first_pcm_ts = time.monotonic()
                                                _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                            chunk_buf += pcm_payload
                                            while len(chunk_buf) >= chunk_bytes:
                                                part_pcm = chunk_buf[:chunk_bytes]
                                                chunk_buf = chunk_buf[chunk_bytes:]
                                                if self.log_chunks:
                                                    _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                                pcm_out_total += len(part_pcm)
                                                emitted_chunks += 1
                                                yield part_pcm
                                        else:
                                            # Persistent resampler using ffmpeg; select input fmt by bits
                                            in_fmt = 's16le' if bits == 16 else ('s24le' if bits == 24 else ('s32le' if bits == 32 else None))
                                            if in_fmt is None:
                                                # Fallback per-chunk convert for uncommon bit depths
                                                res_pcm = await self._convert_wav_to_pcm(encoded)
                                                if res_pcm:
                                                    if first_pcm_ts is None:
                                                        first_pcm_ts = time.monotonic()
                                                        _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                                    chunk_buf += res_pcm
                                                    while len(chunk_buf) >= chunk_bytes:
                                                        part_pcm = chunk_buf[:chunk_bytes]
                                                        chunk_buf = chunk_buf[chunk_bytes:]
                                                        if self.log_chunks:
                                                            _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                                        pcm_out_total += len(part_pcm)
                                                        emitted_chunks += 1
                                                        yield part_pcm
                                                continue
                                            if transcoder is None:
                                                transcoder = await start_transcoder(in_fmt)
                                                _LOGGER.debug(f"Started WAV PCM resampler in_fmt={in_fmt} in_rate={rate} in_ch={ch}")
                                                stdout_task = asyncio.create_task(stdout_reader(transcoder, pcm_queue))
                                                stderr_task = asyncio.create_task(stderr_reader(transcoder))
                                            try:
                                                transcoder.stdin.write(pcm_payload)
                                                if hasattr(transcoder.stdin, 'drain'):
                                                    await transcoder.stdin.drain()
                                            except Exception as e:
                                                _LOGGER.warning(f"Failed writing PCM to ffmpeg stdin: {e}")
                                                continue
                                    else:
                                        # Fallback when header parsing failed
                                        res_pcm = await self._convert_wav_to_pcm(encoded)
                                        if res_pcm:
                                            if first_pcm_ts is None:
                                                first_pcm_ts = time.monotonic()
                                                _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                            chunk_buf += res_pcm
                                            while len(chunk_buf) >= chunk_bytes:
                                                part_pcm = chunk_buf[:chunk_bytes]
                                                chunk_buf = chunk_buf[chunk_bytes:]
                                                if self.log_chunks:
                                                    _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                                pcm_out_total += len(part_pcm)
                                                emitted_chunks += 1
                                                yield part_pcm
                                elif mp3_per_chunk:
                                    # Per-chunk MP3 conversion
                                    pcm = await self._convert_mp3_to_pcm(encoded)
                                    if pcm:
                                        if first_pcm_ts is None:
                                            first_pcm_ts = time.monotonic()
                                            _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                        chunk_buf += pcm
                                        while len(chunk_buf) >= chunk_bytes:
                                            part_pcm = chunk_buf[:chunk_bytes]
                                            chunk_buf = chunk_buf[chunk_bytes:]
                                            if self.log_chunks:
                                                _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                            pcm_out_total += len(part_pcm)
                                            emitted_chunks += 1
                                            yield part_pcm
                                    else:
                                        _LOGGER.warning("MP3 per-chunk conversion returned no PCM")
                                else:
                                    # Persistent transcoder (e.g., mp3)
                                    if transcoder is None:
                                        try:
                                            transcoder = await start_transcoder(chosen_input_fmt or 'mp3')
                                            stdout_task = asyncio.create_task(stdout_reader(transcoder, pcm_queue))
                                            stderr_task = asyncio.create_task(stderr_reader(transcoder))
                                        except Exception as e:
                                            _LOGGER.warning(f"Failed to start ffmpeg transcoder: {e}")
                                            continue
                                    try:
                                        transcoder.stdin.write(encoded)
                                        if hasattr(transcoder.stdin, 'drain'):
                                            await transcoder.stdin.drain()
                                    except Exception as e:
                                        _LOGGER.warning(f"Failed writing to ffmpeg stdin: {e}")
                                        continue

                                # Drain any PCM currently available without blocking long
                                if transcoder is not None:
                                    while not pcm_queue.empty():
                                        out = await pcm_queue.get()
                                        if first_pcm_ts is None:
                                            first_pcm_ts = time.monotonic()
                                            _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                        chunk_buf += out
                                        while len(chunk_buf) >= chunk_bytes:
                                            part_pcm = chunk_buf[:chunk_bytes]
                                            chunk_buf = chunk_buf[chunk_bytes:]
                                            if self.log_chunks:
                                                _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                            pcm_out_total += len(part_pcm)
                                            emitted_chunks += 1
                                            yield part_pcm

                    # After stream ends, process any residual JSON body (e.g., single JSON without newlines)
                    if text_buffer.strip():
                        _LOGGER.debug(f"Residual text_buffer length after stream end: {len(text_buffer)}")
                        try:
                            final_obj = json.loads(text_buffer)
                            audio_bytes = _extract_audio_bytes(final_obj)
                            if audio_bytes:
                                bytes_in_total += len(audio_bytes)
                                if wav_per_chunk:
                                    pcm = self._extract_wav_pcm_fast(audio_bytes) or await self._convert_wav_to_pcm(audio_bytes)
                                    if pcm:
                                        if first_pcm_ts is None:
                                            first_pcm_ts = time.monotonic()
                                            _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                        chunk_buf += pcm
                                        while len(chunk_buf) >= chunk_bytes:
                                            part_pcm = chunk_buf[:chunk_bytes]
                                            chunk_buf = chunk_buf[chunk_bytes:]
                                            _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                            pcm_out_total += len(part_pcm)
                                            emitted_chunks += 1
                                            yield part_pcm
                                elif mp3_per_chunk:
                                    pcm = await self._convert_mp3_to_pcm(audio_bytes)
                                    if pcm:
                                        if first_pcm_ts is None:
                                            first_pcm_ts = time.monotonic()
                                            _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                        chunk_buf += pcm
                                        while len(chunk_buf) >= chunk_bytes:
                                            part_pcm = chunk_buf[:chunk_bytes]
                                            chunk_buf = chunk_buf[chunk_bytes:]
                                            _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                            pcm_out_total += len(part_pcm)
                                            emitted_chunks += 1
                                            yield part_pcm
                                else:
                                    # Persistent transcoder drain (mp3 or WAV resampler)
                                    if is_wav_mode:
                                        # Try to parse and resample as in-stream logic
                                        info = self._extract_wav_pcm_with_info(audio_bytes)
                                        if info is not None:
                                            pcm_payload, rate, ch, bits = info
                                            in_fmt = 's16le' if bits == 16 else ('s24le' if bits == 24 else ('s32le' if bits == 32 else ('f32le' if bits == 32 else None)))
                                            if in_fmt is None:
                                                in_fmt = None
                                            if (rate == self.sample_rate) and (ch == self.channels) and (bits == 16):
                                                if first_pcm_ts is None:
                                                    first_pcm_ts = time.monotonic()
                                                    _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                                chunk_buf += pcm_payload
                                            else:
                                                if transcoder is None:
                                                    transcoder = await start_transcoder(in_fmt or 's16le', rate, ch)
                                                    stdout_task = asyncio.create_task(stdout_reader(transcoder, pcm_queue))
                                                    stderr_task = asyncio.create_task(stderr_reader(transcoder))
                                                try:
                                                    transcoder.stdin.write(pcm_payload)
                                                    if hasattr(transcoder.stdin, 'drain'):
                                                        await transcoder.stdin.drain()
                                                except Exception:
                                                    pass
                                        # Drain available output now
                                        if transcoder is not None:
                                            while not pcm_queue.empty():
                                                out = await pcm_queue.get()
                                                if first_pcm_ts is None:
                                                    first_pcm_ts = time.monotonic()
                                                    _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                                chunk_buf += out
                                                while len(chunk_buf) >= chunk_bytes:
                                                    part_pcm = chunk_buf[:chunk_bytes]
                                                    chunk_buf = chunk_buf[chunk_bytes:]
                                                    _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                                    pcm_out_total += len(part_pcm)
                                                    emitted_chunks += 1
                                                    yield part_pcm
                                    else:
                                        if transcoder is None:
                                            transcoder = await start_transcoder(chosen_input_fmt or None)
                                            stdout_task = asyncio.create_task(stdout_reader(transcoder, pcm_queue))
                                            stderr_task = asyncio.create_task(stderr_reader(transcoder))
                                        try:
                                            transcoder.stdin.write(audio_bytes)
                                            if hasattr(transcoder.stdin, 'drain'):
                                                await transcoder.stdin.drain()
                                        except Exception:
                                            pass
                                        # Drain available output now
                                        while not pcm_queue.empty():
                                            out = await pcm_queue.get()
                                            if first_pcm_ts is None:
                                                first_pcm_ts = time.monotonic()
                                                _LOGGER.debug(f"t_first_pcm={int((first_pcm_ts-start_ts)*1000)}ms (Δ from first SSE={int((first_pcm_ts - (first_sse_ts or first_pcm_ts))*1000)}ms)")
                                            chunk_buf += out
                                            while len(chunk_buf) >= chunk_bytes:
                                                part_pcm = chunk_buf[:chunk_bytes]
                                                chunk_buf = chunk_buf[chunk_bytes:]
                                                _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                                pcm_out_total += len(part_pcm)
                                                emitted_chunks += 1
                                                yield part_pcm
                        except Exception:
                            pass

                    # After stream ends, flush transcoder (mp3 path only)
                    if transcoder is not None and not wav_per_chunk:
                        try:
                            transcoder.stdin.close()
                        except Exception:
                            pass
                        try:
                            if stdout_task is not None:
                                await asyncio.wait_for(stdout_task, timeout=2.0)
                            if stderr_task is not None:
                                await asyncio.wait_for(stderr_task, timeout=2.0)
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(transcoder.wait(), timeout=2.0)
                        except Exception:
                            pass
                        # Emit remaining PCM
                        while not pcm_queue.empty():
                            out = await pcm_queue.get()
                            chunk_buf += out
                            while len(chunk_buf) >= chunk_bytes:
                                part_pcm = chunk_buf[:chunk_bytes]
                                chunk_buf = chunk_buf[chunk_bytes:]
                                if self.log_chunks:
                                    _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                pcm_out_total += len(part_pcm)
                                emitted_chunks += 1
                                yield part_pcm
                    # Emit any final residual PCM, regardless of mode
                    if chunk_buf:
                        _LOGGER.debug(f"Emitting final PCM chunk of {len(chunk_buf)} bytes")
                        pcm_out_total += len(chunk_buf)
                        emitted_chunks += 1
                        yield chunk_buf
                    _LOGGER.debug(f"Stream summary: events={sse_events} audio_events={audio_events} bytes_in={bytes_in_total} pcm_out={pcm_out_total} chunks_emitted={emitted_chunks}")
                    
                    _LOGGER.debug("Finished streaming from Hume AI")
                    
        except Exception as e:
            _LOGGER.error(f"Error streaming from Hume AI: {e}")
            raise

    async def _synthesize_hume_tts_once(self, text: str, voice_spec: Dict[str, str]) -> Optional[bytes]:
        """Call non-streaming Hume TTS endpoint once and return PCM bytes."""
        try:
            url = "https://api.hume.ai/v0/tts"
            headers = {
                "X-Hume-Api-Key": self.hume_api_key,
                "X-Hume-Secret-Key": self.hume_secret_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            fmt: Dict[str, Union[str, int]] = {"type": self.stream_format}
            if self.stream_format == "wav":
                fmt.update({
                    "sample_rate": self.sample_rate,
                    "channels": self.channels,
                })
            # Build provider-aware voice payload
            provider = voice_spec.get("provider", "CUSTOM_VOICE")
            key_type = voice_spec.get("key_type", "id")
            value = voice_spec.get("value")
            # Do not include provider in payload; Hume expects only id or name
            voice_payload: Dict[str, str] = {}
            if key_type == "name":
                voice_payload["name"] = value
            else:
                voice_payload["id"] = value
            payload = {
                "utterances": [{
                    "text": text,
                    "voice": voice_payload,
                }],
                "format": fmt
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status != 200:
                        err = await response.text()
                        _LOGGER.error(f"Hume one-shot TTS error {response.status}: {err}")
                        return None
                    data = await response.json(content_type=None)
            audio_b64 = None
            if isinstance(data, dict):
                if 'audio' in data and isinstance(data['audio'], str):
                    audio_b64 = data['audio']
                elif 'data' in data and isinstance(data['data'], dict) and isinstance(data['data'].get('audio'), str):
                    audio_b64 = data['data']['audio']
                elif 'utterances' in data and isinstance(data['utterances'], list) and data['utterances'] and isinstance(data['utterances'][0], dict):
                    # some APIs return audio per-utterance
                    u0 = data['utterances'][0]
                    if isinstance(u0.get('audio'), str):
                        audio_b64 = u0['audio']
            if not audio_b64:
                _LOGGER.warning("Hume one-shot TTS returned no audio field")
                return None
            import base64
            audio_bytes = base64.b64decode(audio_b64)
            if self.stream_format == "wav":
                pcm_data = await self._convert_wav_to_pcm(audio_bytes)
            else:
                pcm_data = await self._convert_mp3_to_pcm(audio_bytes)
            return pcm_data
        except Exception as e:
            _LOGGER.error(f"Error in one-shot TTS: {e}")
            return None

    async def _convert_mp3_to_pcm(self, mp3_data: bytes) -> Optional[bytes]:
        """Convert MP3 audio data to PCM format using ffmpeg."""
        try:
            # Use ffmpeg to convert MP3 to PCM
            process = await asyncio.create_subprocess_exec(
                'ffmpeg',
                '-hide_banner', '-loglevel', 'error',
                '-i', 'pipe:0',           # Input from stdin
                '-f', 's16le',            # Output format: 16-bit signed little-endian PCM
                '-ar', str(self.sample_rate),  # Sample rate
                '-ac', str(self.channels), # Number of channels
                'pipe:1',                 # Output to stdout
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _LOGGER.debug(f"Converting MP3->PCM: in_bytes={len(mp3_data)}")
            
            # Send MP3 data and get PCM data back
            stdout, stderr = await process.communicate(input=mp3_data)
            
            if process.returncode == 0:
                _LOGGER.debug(f"MP3->PCM done: out_bytes={len(stdout)}")
                return stdout
            else:
                err = (stderr.decode(errors='ignore') if stderr else '').strip()
                _LOGGER.warning(f"ffmpeg conversion failed code={process.returncode} stderr={err}")
                return None
                
        except Exception as e:
            _LOGGER.error(f"Error converting MP3 to PCM: {e}")
            return None

    async def _convert_wav_to_pcm(self, wav_data: bytes) -> Optional[bytes]:
        """Convert WAV audio data to PCM with target rate/channels using ffmpeg.

        If the WAV already matches the target format, ffmpeg will pass-through frames.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                'ffmpeg',
                '-hide_banner', '-loglevel', 'error',
                '-i', 'pipe:0',                # auto-detect WAV from header
                '-f', 's16le',                 # 16-bit signed little-endian PCM
                '-ar', str(self.sample_rate),  # resample to target rate
                '-ac', str(self.channels),     # set target channels
                'pipe:1',
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _LOGGER.debug(f"Converting WAV->PCM (per-chunk): in_bytes={len(wav_data)}")
            stdout, stderr = await process.communicate(input=wav_data)
            if process.returncode == 0:
                _LOGGER.debug(f"WAV->PCM done: out_bytes={len(stdout)}")
                return stdout
            else:
                err = (stderr.decode(errors='ignore') if stderr else '').strip()
                _LOGGER.warning(f"ffmpeg WAV->PCM failed code={process.returncode} stderr={err}")
                return None
        except Exception as e:
            _LOGGER.error(f"Error converting WAV to PCM: {e}")
            return None

    def _extract_wav_pcm_with_info(self, wav_data: bytes) -> Optional[tuple[bytes, int, int, int]]:
        """Parse a WAV buffer and return (pcm_bytes, sample_rate, channels, bits_per_sample).

        Supports PCM (format 1) and common layouts. Returns None if parsing fails
        or the 'data' chunk is not present.
        """
        try:
            b = wav_data
            if len(b) < 44:
                return None
            # Check RIFF/WAVE
            if b[0:4] != b'RIFF' or b[8:12] != b'WAVE':
                return None
            pos = 12
            blen = len(b)
            fmt_rate = None
            fmt_channels = None
            fmt_bps = None
            fmt_audio_fmt = None
            data_start = None
            data_size = None
            # Iterate chunks
            while pos + 8 <= blen:
                chunk_id = b[pos:pos+4]
                if pos + 8 > blen:
                    break
                chunk_size = struct.unpack('<I', b[pos+4:pos+8])[0]
                pos += 8
                if pos + chunk_size > blen:
                    break
                if chunk_id == b'fmt ':
                    # Minimum 16 bytes for PCM
                    if chunk_size >= 16:
                        try:
                            (fmt_audio_fmt, fmt_channels, fmt_rate, _byte_rate, _block_align, fmt_bps) = struct.unpack('<HHIIHH', b[pos:pos+16])
                        except Exception:
                            pass
                elif chunk_id == b'data':
                    data_start = pos
                    data_size = chunk_size
                # advance to next chunk boundary (pad to even)
                pos += chunk_size + (chunk_size & 1)
                # early exit if we have key info
                if (fmt_audio_fmt is not None) and (data_start is not None):
                    break
            if (data_start is None) or (data_size is None) or (fmt_rate is None) or (fmt_channels is None) or (fmt_bps is None):
                return None
            end = min(data_start + data_size, blen)
            pcm = b[data_start:end]
            return (pcm, int(fmt_rate), int(fmt_channels), int(fmt_bps))
        except Exception:
            return None

    def _extract_wav_pcm_fast(self, wav_data: bytes) -> Optional[bytes]:
        """Fast-path extractor for PCM from a WAV buffer when formats already match.

        Returns raw PCM bytes if the WAV is PCM 16-bit with sample_rate/channels
        matching the server output settings. Otherwise returns None.
        """
        try:
            b = wav_data
            if len(b) < 44:
                return None
            # RIFF header
            if b[0:4] != b'RIFF' or b[8:12] != b'WAVE':
                return None
            # Parse chunks
            pos = 12
            fmt_rate = None
            fmt_channels = None
            fmt_bps = None
            fmt_audio_fmt = None
            data_start = None
            data_size = None
            blen = len(b)
            while pos + 8 <= blen:
                chunk_id = b[pos:pos+4]
                chunk_size = struct.unpack('<I', b[pos+4:pos+8])[0]
                pos += 8
                if pos + chunk_size > blen:
                    break
                if chunk_id == b'fmt ':
                    if chunk_size >= 16:
                        (fmt_audio_fmt, fmt_channels, fmt_rate, _byte_rate, _block_align, fmt_bps) = struct.unpack('<HHIIHH', b[pos:pos+16])
                    # else unsupported
                elif chunk_id == b'data':
                    data_start = pos
                    data_size = chunk_size
                # Align to even boundary
                pos += chunk_size + (chunk_size & 1)
                # Early exit if we have everything
                if (fmt_audio_fmt is not None) and (data_start is not None):
                    break
            # Validate format
            if (fmt_audio_fmt == 1) and (fmt_bps == 16) and (fmt_rate == self.sample_rate) and (fmt_channels == self.channels) and (data_start is not None) and (data_size is not None):
                end = min(data_start + data_size, blen)
                return b[data_start:end]
            return None
        except Exception:
            return None


async def main() -> None:
    """Main server function."""
    parser = argparse.ArgumentParser(description="Hume AI TTS Wyoming Server")
    parser.add_argument(
        "--uri",
        default="tcp://0.0.0.0:10201",
        help="URI to bind the server to",
    )
    parser.add_argument(
        "--hume-api-key",
        required=False,
        default=None,
        help="Hume AI API Key",
    )
    parser.add_argument(
        "--hume-secret-key", 
        required=False,
        default=None,
        help="Hume AI Secret Key",
    )
    parser.add_argument(
        "--default-voice-id",
        default="eaa917f9-fb8e-4b44-9e8d-b3b78c9c1ebc",  # Scar voice
        help="Default Hume AI voice ID",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=22050,
        help="Audio sample rate (PCM output)",
    )
    parser.add_argument(
        "--format",
        default="mp3",
        help="Hume output format to request (mp3 or wav)",
    )
    parser.add_argument(
        "--instant-mode",
        action="store_true",
        help="Enable Hume instant_mode (lower latency, potentially lower fidelity)",
    )
    parser.add_argument(
        "--samples-per-chunk",
        type=int,
        default=512,
        help="PCM samples per chunk emitted to Wyoming (lower for lower latency)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--no-debug",
        action="store_true",
        help="Disable debug logging (overrides env)",
    )
    parser.add_argument(
        "--log-file",
        default="/tmp/hume-tts.log",
        help="Path to write server logs (also logs to stdout)",
    )
    parser.add_argument(
        "--log-max-bytes",
        type=int,
        default=5_000_000,
        help="Max size in bytes for rotating log file",
    )
    parser.add_argument(
        "--log-backup-count",
        type=int,
        default=3,
        help="Number of rotated log files to keep",
    )
    parser.add_argument(
        "--voices-file",
        default=None,
        help=(
            "Path to voices file. Supports JSON list or CSV lines: 'name, id_or_name[, provider]'. "
            "Provider is optional and can be 'custom' (default) or 'hume' for Hume Voice Library. "
            "If not set, will try 'voices.txt' then 'voices.json' next to server.py."
        ),
    )
    
    args = parser.parse_args()
    
    # Apply environment variable overrides BEFORE logging setup so log file can be redirected.
    # Precedence: CLI > env > defaults (only override when arg still equals parser default)
    def _env_str(name: str):
        v = os.getenv(name)
        return v if v is not None else None
    def _env_int(name: str):
        v = os.getenv(name)
        if v is None:
            return None
        try:
            return int(str(v).strip())
        except Exception:
            return None
    def _env_bool(name: str):
        v = os.getenv(name)
        if v is None:
            return None
        return str(v).strip().lower() in {"1","true","yes","on"}

    _defaults = {
        "uri": parser.get_default("uri"),
        "format": parser.get_default("format"),
        "instant_mode": parser.get_default("instant_mode"),
        "samples_per_chunk": parser.get_default("samples_per_chunk"),
        "sample_rate": parser.get_default("sample_rate"),
        "log_file": parser.get_default("log_file"),
        "voices_file": parser.get_default("voices_file"),
        "default_voice_id": parser.get_default("default_voice_id"),
    }

    v = _env_str("HUME_TTS_URI")
    if (v is not None) and (args.uri == _defaults["uri"]):
        args.uri = v
    v = _env_str("HUME_TTS_FORMAT")
    if (v is not None) and (args.format == _defaults["format"]):
        args.format = v
    v = _env_bool("HUME_TTS_INSTANT_MODE")
    if (v is not None) and (args.instant_mode == _defaults["instant_mode"]):
        args.instant_mode = v
    v = _env_int("HUME_TTS_SAMPLES_PER_CHUNK")
    if (v is not None) and (args.samples_per_chunk == _defaults["samples_per_chunk"]):
        args.samples_per_chunk = v
    v = _env_int("HUME_TTS_SAMPLE_RATE")
    if (v is not None) and (args.sample_rate == _defaults["sample_rate"]):
        args.sample_rate = v
    v = _env_str("HUME_TTS_LOG_FILE")
    if (v is not None) and (args.log_file == _defaults["log_file"]):
        args.log_file = v
    v = _env_str("HUME_TTS_VOICES_FILE")
    if (v is not None) and (args.voices_file == _defaults["voices_file"]):
        args.voices_file = v
    v = _env_str("HUME_TTS_DEFAULT_VOICE_ID")
    if (v is not None) and (args.default_voice_id == _defaults["default_voice_id"]):
        args.default_voice_id = v

    # Configure logging with env + flags. Default: DEBUG unless explicitly disabled.
    env_debug = os.getenv("HUME_TTS_DEBUG")
    env_debug_bool = None
    if env_debug is not None:
        env_debug_bool = env_debug.strip().lower() in {"1", "true", "yes", "on"}
    if args.debug:
        debug_enabled = True
    elif getattr(args, "no_debug", False):
        debug_enabled = False
    elif env_debug_bool is not None:
        debug_enabled = env_debug_bool
    else:
        debug_enabled = True  # default to debug on
    # Configure logging to stdout + optional rotating file
    log_level = logging.DEBUG if debug_enabled else logging.INFO
    root_logger = logging.getLogger()
    # Clear existing handlers
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    root_logger.setLevel(log_level)
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    # Console handler (stdout)
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(fmt)
    root_logger.addHandler(ch)
    installed_dests = ["stdout"]
    # File handler (rotating) for configured path
    if args.log_file:
        try:
            os.makedirs(os.path.dirname(args.log_file), exist_ok=True)
            fh = RotatingFileHandler(args.log_file, maxBytes=args.log_max_bytes, backupCount=args.log_backup_count, encoding="utf-8")
            fh.setLevel(log_level)
            fh.setFormatter(fmt)
            root_logger.addHandler(fh)
            installed_dests.append(args.log_file)
        except Exception as e:
            _LOGGER.warning(f"Failed to set up file logging at {args.log_file}: {e}")
    # Always try a fallback file handler inside the container for reliability
    try:
        fallback_log = "/tmp/hume-tts.log"
        if not args.log_file or os.path.abspath(args.log_file) != fallback_log:
            os.makedirs(os.path.dirname(fallback_log), exist_ok=True)
            fh2 = RotatingFileHandler(fallback_log, maxBytes=args.log_max_bytes, backupCount=args.log_backup_count, encoding="utf-8")
            fh2.setLevel(log_level)
            fh2.setFormatter(fmt)
            root_logger.addHandler(fh2)
            installed_dests.append(fallback_log)
    except Exception as e:
        _LOGGER.warning(f"Failed to set up fallback file logging at /tmp/hume-tts.log: {e}")
    _LOGGER.info(f"Log destinations active: {', '.join(installed_dests)}")
    _LOGGER.info(f"Debug logging {'ENABLED' if debug_enabled else 'DISABLED'} (flag --debug={args.debug}, --no-debug={getattr(args,'no_debug', False)}, env HUME_TTS_DEBUG={env_debug})")
    # Resolve Hume credentials from CLI or environment
    if not args.hume_api_key:
        args.hume_api_key = os.getenv("HUME_API_KEY")
    if not args.hume_secret_key:
        args.hume_secret_key = os.getenv("HUME_SECRET_KEY")
    if not args.hume_api_key or not args.hume_secret_key:
        _LOGGER.error("Missing Hume credentials. Provide --hume-api-key/--hume-secret-key or set HUME_API_KEY/HUME_SECRET_KEY env vars.")
        sys.exit(2)
    
    _LOGGER.info(f"Starting Hume AI TTS server on {args.uri}")
    _LOGGER.info(f"Default voice ID: {args.default_voice_id}")
    
    # Load voices from file (JSON or CSV "name, id" per line). Fallback to built-in 'scar' if missing.
    voice_entries: List[Dict[str, str]] = []
    voice_map: Dict[str, Dict[str, str]] = {}
    voices_file = getattr(args, "voices_file", None)

    def _parse_json_voices(text: str) -> Optional[List[Dict[str, str]]]:
        try:
            obj = json.loads(text)
        except Exception:
            return None
        out: List[Dict[str, str]] = []
        if isinstance(obj, list):
            for item in obj:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                vid = str(item.get("id", "")).strip()
                desc = str(item.get("description", "")).strip() or f"{name} - Custom Hume voice"
                if name and vid:
                    out.append({"name": name, "id": vid, "description": desc})
        return out or None

    def _parse_csv_voices(text: str) -> Optional[List[Dict[str, str]]]:
        out: List[Dict[str, str]] = []
        try:
            from uuid import UUID as _UUID

            def _is_uuid(s: str) -> bool:
                try:
                    _UUID(str(s))
                    return True
                except Exception:
                    return False

            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith('#') or line.startswith('//'):
                    continue
                parts = [p.strip() for p in line.split(',')]
                if len(parts) < 2:
                    continue
                name = parts[0]
                value = parts[1]
                provider = parts[2] if len(parts) >= 3 else None
                provider_norm = (provider or '').strip().upper()
                if provider_norm in {"HUME", "HUME_AI", "HUMEAI"}:
                    provider_norm = "HUME_AI"
                elif provider_norm in {"CUSTOM", "CUSTOM_VOICE", "CUSTOMVOICE", "CV"}:
                    provider_norm = "CUSTOM_VOICE"
                else:
                    provider_norm = None

                # Default to CUSTOM_VOICE if unspecified
                if not provider_norm:
                    provider_norm = "CUSTOM_VOICE"

                # Determine key field for Hume voice: id vs name
                key_type = "id"
                if provider_norm == "HUME_AI" and not _is_uuid(value):
                    key_type = "name"

                if name and value:
                    out.append({
                        "name": name,
                        "value": value,
                        "provider": provider_norm,
                        "key_type": key_type,
                        "description": f"{name} - {'Hume voice' if provider_norm=='HUME_AI' else 'Custom Hume voice'}",
                    })
        except Exception:
            return None
        return out or None

    # Determine candidate paths when not explicitly provided
    candidates: List[str] = []
    if voices_file:
        candidates.append(voices_file)
    else:
        base_dir = os.path.dirname(__file__)
        candidates.append(os.path.join(base_dir, "voices.txt"))
        candidates.append(os.path.join(base_dir, "voices.json"))

    for path in candidates:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as vf:
                text = vf.read()
            entries = _parse_json_voices(text) or _parse_csv_voices(text)
            if entries:
                voice_entries = entries
                # Build name->spec and id->provider maps
                voice_map = {}
                id_provider_map: Dict[str, str] = {}
                for e in entries:
                    # Map friendly name -> voice spec
                    voice_map[e["name"].lower()] = {
                        "provider": e.get("provider", "CUSTOM_VOICE"),
                        "key_type": e.get("key_type", "id"),
                        "value": e.get("value") or e.get("id"),
                    }
                    # If the entry uses an ID, map ID -> provider for override lookups
                    val = voice_map[e["name"].lower()]["value"]
                    if voice_map[e["name"].lower()]["key_type"] == "id" and isinstance(val, str):
                        id_provider_map[val] = voice_map[e["name"].lower()]["provider"]
                _LOGGER.info(f"Loaded voices from {path}: {[v['name'] for v in voice_entries]}")
                break
        except Exception as e:
            _LOGGER.warning(f"Failed to load voices file {path}: {e}")

    if not voice_entries:
        # Fallback voice
        voice_entries = [{
            "name": "scar",
            "value": args.default_voice_id,
            "provider": "CUSTOM_VOICE",
            "key_type": "id",
            "description": "Scar - Custom trained voice",
        }]
        voice_map = {"scar": {"provider": "CUSTOM_VOICE", "key_type": "id", "value": args.default_voice_id}}
        id_provider_map = {args.default_voice_id: "CUSTOM_VOICE"}
        _LOGGER.info("Using built-in voices: ['scar'] (create voices.txt or voices.json to add more)")

    # Create server info with dynamic voices
    tts_program = TtsProgram(
        name="hume-ai-octave",
        description="Hume AI Octave TTS with custom voices",
        attribution=Attribution(
            name="Hume AI",
            url="https://www.hume.ai/",
        ),
        installed=True,
        version="1.0.0",
        voices=[
            TtsVoice(
                name=v["name"],
                description=v.get("description") or f"{v['name']} - Custom Hume voice",
                attribution=Attribution(
                    name="Hume AI",
                    url="https://www.hume.ai/",
                ),
                installed=True,
                version="1.0.0",
                languages=["en"],
            ) for v in voice_entries
        ],
        supports_synthesize_streaming=True,
    )
    
    wyoming_info = Info(tts=[tts_program])
    
    # Create event handler
    def event_handler_factory(reader, writer):
        return HumeTtsEventHandler(
            reader,
            writer,
            hume_api_key=args.hume_api_key,
            hume_secret_key=args.hume_secret_key,
            default_voice_id=args.default_voice_id,
            sample_rate=args.sample_rate,
            wyoming_info=wyoming_info,
            stream_format=args.format,
            instant_mode=args.instant_mode,
            samples_per_chunk=args.samples_per_chunk,
            voice_map=voice_map,
            id_provider_map=id_provider_map if 'id_provider_map' in locals() else {},
        )
    
    # Start server
    server = AsyncServer.from_uri(args.uri)
    
    _LOGGER.info("Server ready")
    await server.run(event_handler_factory)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _LOGGER.info("Server stopped")
