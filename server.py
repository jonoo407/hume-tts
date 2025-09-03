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
        # Streaming input session state (for SynthesizeStart/Chunk/Stop)
        self._streaming_in: bool = False
        self._streaming_text_parts: List[str] = []
        self._streaming_voice: Optional[SynthesizeVoice] = None
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
            
            # Try streaming TTS first
            async for pcm_chunk in self._stream_hume_tts(synthesize.text, voice_spec):
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
        _LOGGER.debug("SynthesizeStart received: initialized streaming input session")

    async def handle_synthesize_chunk(self, chunk: SynthesizeChunk) -> None:
        """Accumulate a chunk of streaming text input."""
        if not self._streaming_in:
            _LOGGER.debug("SynthesizeChunk received outside of streaming session; starting implicit session")
            self._streaming_in = True
            self._streaming_text_parts = []
            self._streaming_voice = None
        self._streaming_text_parts.append(chunk.text)
        _LOGGER.debug(f"SynthesizeChunk received: +{len(chunk.text)} chars (total={sum(len(p) for p in self._streaming_text_parts)})")

    async def handle_synthesize_stop(self, _stop: SynthesizeStop) -> None:
        """Finalize streaming input and perform synthesis, then signal stopped."""
        try:
            total_text = "".join(self._streaming_text_parts) if self._streaming_text_parts else ""
            _LOGGER.debug(f"SynthesizeStop received: finalizing streaming input (chars={len(total_text)})")
            # Reuse existing synthesis path
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

    async def _stream_hume_tts(self, text: str, voice_spec: Dict[str, str]) -> bytes:
        """Stream TTS audio from Hume AI API and convert to PCM."""
        try:
            start_ts = time.monotonic()
            # Prepare Hume API request
            url = "https://api.hume.ai/v0/tts/stream/json"
            
            headers = {
                "X-Hume-Api-Key": self.hume_api_key,
                "X-Hume-Secret-Key": self.hume_secret_key,
                "Content-Type": "application/json",
                # Accept multiple streaming encodings; server will choose one
                "Accept": "text/event-stream, application/x-ndjson, application/json"
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
            voice_payload: Dict[str, str] = {"provider": provider}
            if key_type == "name":
                voice_payload["name"] = value
            else:
                voice_payload["id"] = value

            payload = {
                "utterances": [{
                    "text": text,
                    "voice": voice_payload,
                }],
                "instant_mode": self.instant_mode,
                "format": fmt
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
                    async def start_transcoder(input_format: Optional[str]):
                        args = [
                            'ffmpeg', '-hide_banner', '-loglevel', 'error',
                        ]
                        if input_format:
                            args += ['-f', input_format]
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
                    wav_per_chunk = False  # decode each WAV chunk independently via one-shot ffmpeg
                    # Diagnostics
                    sse_events = 0
                    audio_events = 0
                    bytes_in_total = 0
                    pcm_out_total = 0
                    emitted_chunks = 0

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
                                    _LOGGER.debug(f"Hume stream event: keys={keys} type={etype} data.keys={dkeys} chunk.keys={ckeys}")
                                except Exception:
                                    pass
                                sse_events += 1

                            # Find audio base64 in common locations
                            audio_b64 = None
                            if isinstance(chunk_data, dict):
                                if isinstance(chunk_data.get('audio'), str):
                                    audio_b64 = chunk_data['audio']
                                elif isinstance(chunk_data.get('data'), dict) and isinstance(chunk_data['data'].get('audio'), str):
                                    audio_b64 = chunk_data['data']['audio']
                                elif isinstance(chunk_data.get('chunk'), dict) and isinstance(chunk_data['chunk'].get('audio'), str):
                                    audio_b64 = chunk_data['chunk']['audio']
                                elif chunk_data.get('type') in {'audio', 'audio_chunk'} and isinstance(chunk_data.get('payload', {}).get('audio'), str):
                                    audio_b64 = chunk_data['payload']['audio']

                            if audio_b64:
                                audio_events += 1
                                # Strip data URL prefix if present
                                if audio_b64.startswith('data:'):
                                    idx = audio_b64.find('base64,')
                                    if idx != -1:
                                        audio_b64 = audio_b64[idx + 7:]
                                import base64
                                try:
                                    encoded = base64.b64decode(audio_b64)
                                except Exception as e:
                                    _LOGGER.warning(f"Failed to b64 decode audio chunk: {e}")
                                    continue

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
                                        # Use per-chunk decode for WAV to avoid multiple RIFF headers in a single stream
                                        wav_per_chunk = True
                                        _LOGGER.debug("Using per-chunk WAV decode (one ffmpeg per event)")
                                    else:
                                        transcoder = await start_transcoder(chosen_input_fmt)
                                        _LOGGER.debug(f"Started transcoder input_fmt={chosen_input_fmt}")
                                        stdout_task = asyncio.create_task(stdout_reader(transcoder, pcm_queue))
                                        stderr_task = asyncio.create_task(stderr_reader(transcoder))

                                # Handle current audio block
                                if wav_per_chunk:
                                    # Convert this WAV chunk independently, prefer fast-path
                                    pcm = None
                                    fast_pcm = self._extract_wav_pcm_fast(encoded)
                                    if fast_pcm is not None:
                                        _LOGGER.debug(f"WAV fast-path used: out_bytes={len(fast_pcm)}")
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
                                            _LOGGER.debug(f"Emitting PCM chunk of {len(part_pcm)} bytes")
                                            pcm_out_total += len(part_pcm)
                                            emitted_chunks += 1
                                            yield part_pcm
                                    else:
                                        _LOGGER.warning("WAV per-chunk conversion returned no PCM")
                                else:
                                    # Persistent transcoder (mp3)
                                    try:
                                        transcoder.stdin.write(encoded)
                                        if hasattr(transcoder.stdin, 'drain'):
                                            await transcoder.stdin.drain()
                                    except Exception as e:
                                        _LOGGER.warning(f"Failed writing to ffmpeg stdin: {e}")
                                        continue

                                # Drain any PCM currently available without blocking long
                                if not wav_per_chunk:
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

                    # After HTTP stream ends, note any residual text that didn't hit a delimiter
                    if text_buffer.strip():
                        _LOGGER.debug(f"Residual text_buffer length after stream end: {len(text_buffer)}")

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
            voice_payload: Dict[str, str] = {"provider": provider}
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
    # File handler (rotating)
    if args.log_file:
        try:
            os.makedirs(os.path.dirname(args.log_file), exist_ok=True)
            fh = RotatingFileHandler(args.log_file, maxBytes=args.log_max_bytes, backupCount=args.log_backup_count)
            fh.setLevel(log_level)
            fh.setFormatter(fmt)
            root_logger.addHandler(fh)
        except Exception as e:
            _LOGGER.warning(f"Failed to set up file logging at {args.log_file}: {e}")
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
