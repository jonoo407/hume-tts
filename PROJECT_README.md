# Hume AI TTS Wyoming Integration Project

> Note: For public installation and usage, see `README.md`. This document serves as developer/deep-dive notes.
> Public releases prefer `voices.txt` (CSV) for voice configuration. JSON examples below are legacy and optional.

## 🎯 Project Overview

This project integrates Hume AI's Octave TTS service with Home Assistant using the Wyoming protocol, providing streaming text-to-speech with custom voice support ("Scar" voice model).

**Goal:** Enable low-latency, high-quality streaming TTS in Home Assistant using custom-trained voices from Hume AI.

## 📊 Current Status: ✅ Operational and optimized (WAV + instant mode)

- **Docker Container:** Running successfully on port 10201
- **Wyoming Server:** Active and ready for Home Assistant integration
- **Voice Models:** Multiple custom voices supported via simple CSV (`voices.txt`) or JSON
- **Protocol:** Wyoming TTS protocol fully implemented
- **Audio Format:** Requests WAV from Hume and streams PCM 22050 Hz, 16-bit, mono to Home Assistant

## ✅ Notes & Recommendations (as of September 3, 2025)

- **Use WAV + instant_mode for best results.** MP3 can introduce decoder priming and clipping/static. WAV avoids both and still streams at low latency.
- **Typical first audio:** ~0.98 s. Latest measurements: `t_first_sse≈932 ms`, `t_first_pcm≈983 ms`, first chunk sent at the same time.
- **Chunk size:** `--samples-per-chunk 512` is a good default. Testing `256` did not improve first audio (~1.05 s in a trial).
- **Streaming input:** Implements `SynthesizeStart/Chunk/Stop` and emits `SynthesizeStopped` after audio completes. `supports_synthesize_streaming=True` is advertised in Info.

## 🏗️ Architecture

```
Home Assistant
    ↓ (Wyoming Protocol)
Wyoming TTS Server (Port 10201)
    ↓ (HTTP/Streaming)
Hume AI Octave TTS API
    ↓ (WAV Chunks, instant_mode)
FFmpeg Audio Processing
    ↓ (PCM Audio)
Home Assistant Audio Output
```

## 🔧 Configuration

### Environment Variables Required
- `HUME_API_KEY`: Your Hume AI API key
- `HUME_SECRET_KEY`: Your Hume AI secret key

### Default Settings
- **Server URI:** `tcp://0.0.0.0:10201`
- **Default Voice ID:** set by `--default-voice-id` (used when requested voice is unknown)
- **Sample Rate:** 22050 Hz
- **Audio Request Format:** WAV from Hume (output to HA is PCM 16-bit mono)
- **Chunk Size:** 512 samples

## 📁 File Structure

```
/home/jon/smart-home-setup/hume-tts/
├── server.py             # Main Wyoming TTS server
├── requirements.txt      # Python dependencies
├── Dockerfile            # Container build instructions
├── secrets.env           # Environment variables (Hume keys)
├── voices.sample.json    # Sample voices JSON (copy to voices.json and edit)
├── voices.json           # Optional: JSON voices list (mounted in container)
├── voices.txt            # Optional: CSV voices file (name, id per line)
├── logs/                 # Optional log directory
└── PROJECT_README.md     # This documentation
```

## 🐳 Docker Configuration

### Recommended (docker run)
```bash
docker run -d --name hume-tts \
  --restart unless-stopped \
  --env-file /home/jon/smart-home-setup/hume-tts/secrets.env \
  -v /home/jon/smart-home-setup/hume-tts/voices.txt:/app/voices.txt:ro \
  -p 10201:10201 hume-tts:latest \
  --uri tcp://0.0.0.0:10201 \
  --instant-mode \
  --samples-per-chunk 512 \
  --format wav \
  --voices-file /app/voices.txt \
  --debug --log-file /tmp/hume-tts.log
```

Note: If you prefer JSON, mount `/app/voices.json` and use `--voices-file /app/voices.json` instead.

### Build Status
- **Image:** `hume-tts:latest`
- **Base:** `python:3.11-slim`
- **Size:** ~760MB (includes FFmpeg for audio processing)

## 🎛️ Server Implementation Details

### Wyoming Protocol Support
- **Service Discovery:** Responds to `describe` events with TTS capability info
- **Voice Information:** Advertises all voices from `voices.txt` (CSV) or `voices.json` (friendly names). HA shows these in the voice dropdown.
- **Streaming:** Supports single-shot and streaming synthesis (SSE/NDJSON). Implements `SynthesizeStart/Chunk/Stop` and emits `SynthesizeStopped`.
- **Info Flags:** `supports_synthesize_streaming=True`.
- **Audio Format:** Requests WAV from Hume; emits PCM 22050 Hz mono. MP3 is supported but not recommended due to priming/clipping.

### Audio Processing Pipeline
1. **Text Input:** Received via Wyoming `synthesize` event
2. **Hume API:** Streaming request to Octave TTS with voice_id
3. **Audio Chunks:** Receive WAV chunks (~1 second each) with `instant_mode` enabled
4. **Conversion:** Fast-path direct PCM extraction from WAV; ffmpeg fallback when needed
5. **Streaming Output:** Send PCM chunks via Wyoming audio events

### Error Handling
- Connection retry logic for Hume API failures
- Audio format validation and conversion fallbacks
- Wyoming protocol compliance error responses
- Container restart policy for service reliability

## 🏠 Home Assistant Integration

### Automatic Discovery
The Wyoming service should be auto-discovered by Home Assistant through:
- **Protocol:** Wyoming TTS service advertisement
- **Network:** Zeroconf/mDNS discovery on local network
- **Port:** 10201 TCP

### Manual Integration Steps
If auto-discovery fails:
1. Go to **Settings → Devices & Services**
2. Click **"Add Integration"**
3. Search for **"Wyoming Protocol"**
4. Configure:
   - **Host:** `localhost` or your server IP
   - **Port:** `10201`

### Voice Configuration
- **Service Name:** "Hume AI Octave TTS"
- **Available Voices:** Populated from `voices.txt` (CSV) or `voices.json`. Each entry's `name` appears in HA's voice dropdown.
- **Language Support:** English (en)
- **Features:** Streaming synthesis support

## 🎙️ Multiple Voices via voices.txt (CSV)

Create a simple CSV file with your voice names and provider hint. This file is read at startup to populate the voice list in Home Assistant and to map friendly names → Hume specs at runtime.

1. Create/edit `voices.txt`:
   ```text
   # Custom voices (2 columns; provider defaults to custom)
   Scarlett, eaa917f9-fb8e-4b44-9e8d-b3b78c9c1abc
   Jon, 1f9f033f-c890-402b-83f3-74affa44dabc

   # Hume Voice Library by name or id (3rd column 'hume')
   Olivia, Olivia, hume
   Kora, 59cfc7ab-e945-43de-ad1a-471daa379c67, hume
   ```
   - Format: `name, id_or_name[, provider]` (comma-separated). Lines starting with `#` or blank lines are ignored.
   - Provider: `custom` (default) or `hume` (Hume Voice Library). Synonyms like `hume_ai` are accepted.
   - If provider is `hume` and the second field is not a UUID, it is treated as a Hume Library voice name.
   - Names are case-insensitive when selected in Home Assistant.

2. Mount it into the container and pass `--voices-file /app/voices.txt` (see Docker example above).

Notes:
- If `--voices-file` is not set, the server will try `voices.txt` first, then `voices.json`.
- You can also request a voice by UUID directly in HA; the server accepts both friendly names and UUIDs.


## 🚀 Deployment Commands

### Start the Service
```bash
cd /home/jon/smart-home-setup
docker-compose up hume-tts -d
```

### Check Status
```bash
docker-compose logs hume-tts
docker ps | grep hume
```

### Rebuild After Changes
```bash
docker-compose build hume-tts
docker-compose up hume-tts --no-deps -d
```

### Stop the Service
```bash
docker-compose stop hume-tts
```

## 🔍 Troubleshooting

### Common Issues and Solutions

#### Container Restart Loop
- **Symptoms:** Container continuously restarting
- **Check:** `docker logs hume-tts` for error messages
- **Common Causes:** 
  - Missing/invalid Hume API credentials
  - Wyoming protocol compatibility issues
  - Port conflicts on 10201

#### No Audio Output
- **Check:** Audio format compatibility (22050Hz PCM)
- **Verify:** FFmpeg is working in container
- **Test:** Wyoming protocol connectivity

#### Home Assistant Discovery Issues
- **Manual Setup:** Add integration manually with host:port
- **Network:** Ensure port 10201 is accessible
- **Protocol:** Check Wyoming integration is enabled in HA

### Logs and Debugging
- **Container Logs:** `docker logs hume-tts`
- **Real-time Logs:** `docker logs -f hume-tts`
- **Debug Level:** Add `--debug` flag to server.py command

## 📋 Dependencies

### Python Packages (requirements.txt)
```
wyoming>=1.7.2    # Wyoming protocol implementation (streaming events/flags)
aiohttp>=3.8.0    # Async HTTP client for Hume API
```

### System Dependencies
- **FFmpeg:** Audio format conversion
- **Python 3.11:** Runtime environment

## 🔐 Security Considerations

- **API Keys:** Stored as environment variables (not in code)
- **Network:** Service bound to 0.0.0.0 (consider firewall rules)
- **Container:** Runs as non-root user for security
- **Dependencies:** Regular updates recommended for security patches

## 🎵 Audio Specifications

### Input (from Hume AI)
- **Format:** WAV (streaming chunks, instant_mode)
- **Quality:** High-quality neural TTS
- **Voice:** Custom trained "Scar" model
- **Latency:** ~0.95–1.1 s to first audio typical. Latest: `t_first_sse≈932 ms`, `t_first_pcm≈983 ms`.

### Output (to Home Assistant)
- **Format:** PCM (raw audio)
- **Sample Rate:** 22050 Hz
- **Bit Depth:** 16-bit
- **Channels:** Mono
- **Latency:** PCM chunks are forwarded immediately when ready; minimal additional processing.

## 🔄 Version History

### v1.0.0 (Current - September 2024)
- ✅ Initial Wyoming protocol implementation
- ✅ Hume AI Octave TTS integration
- ✅ Docker containerization
- ✅ Custom voice support ("Scar")
- ✅ Streaming audio pipeline
- ✅ Home Assistant compatibility

### Future Enhancements
- Multiple voice support
- Voice emotion/style parameters
- Advanced audio processing options
- Performance monitoring and metrics
- Caching for frequently used phrases

## 📞 Support and Resources

### Documentation Links
- **Wyoming Protocol:** https://github.com/rhasspy/wyoming
- **Hume AI Docs:** https://dev.hume.ai/docs
- **Home Assistant Voice:** https://www.home-assistant.io/voice_control/

### Project Context
- **Created:** September 2024
- **Purpose:** Custom voice integration for Home Assistant
- **Environment:** Smart home setup with Docker deployment
- **Integration:** Part of larger Home Assistant automation system

---

## 📝 Quick Start Commands

```bash
# Build image (from repo root)
docker build -t hume-tts:latest /home/jon/smart-home-setup/hume-tts

# Run service (WAV + instant mode)
docker run -d --name hume-tts \
  --restart unless-stopped \
  --env-file /home/jon/smart-home-setup/hume-tts/secrets.env \
  -p 10201:10201 hume-tts:latest \
  --uri tcp://0.0.0.0:10201 \
  --instant-mode \
  --samples-per-chunk 512 \
  --format wav \
  --debug --log-file /tmp/hume-tts.log

# Check logs/timings
docker logs hume-tts | tail -n 200

# Describe (Wyoming) – Python (safest)
python3 - << 'PY'
import socket, json
host, port = "127.0.0.1", 10201
msg = b'{"type":"describe"}'
hdr = b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (len(msg),)
s = socket.create_connection((host, port), timeout=3)
s.sendall(hdr + msg)
s.settimeout(3)

# Read headers
def read_line(sock):
    data = b""
    while not data.endswith(b"\r\n"):
        ch = sock.recv(1)
        if not ch:
            break
        data += ch
    return data

headers = {}
while True:
    line = read_line(s)
    if not line or line == b"\r\n":
        break
    k, v = line.decode().rstrip("\r\n").split(":", 1)
    headers[k.strip().lower()] = v.strip()

length = int(headers.get("content-length", "0"))
body = b""
while len(body) < length:
    chunk = s.recv(length - len(body))
    if not chunk:
        break
    body += chunk
s.close()
print(body.decode("utf-8", "ignore"))
PY
```

**Last Updated:** September 3, 2025  
**Status:** Operational and optimized ✅
