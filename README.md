# Hume AI TTS (Wyoming) for Home Assistant

Low-latency, high-quality Text-to-Speech for Home Assistant using Hume AI Octave TTS over the Wyoming protocol. Runs in Docker and streams PCM audio to HA. Voice selection is driven by a simple `voices.txt` file.

## Features
- Streaming TTS with low latency (WAV + instant mode recommended)
- Voices defined in `voices.txt` (CSV): `name, id_or_name[, provider]`
- Supports Hume Voice Library by name or UUID via `provider=hume`
- Works with Home Assistant Wyoming integration
- Minimal dependencies; small, hardened Docker image (non-root + healthcheck)

## Quick start

1) Clone and set up env
```bash
git clone https://github.com/<your-org>/hume-tts.git
cd hume-tts
cp .env.example .env
# Edit .env with your Hume API credentials
```

2) Create your voices list
```bash
cp voices.sample.txt voices.txt
# Edit voices.txt
# Example:
# Scarlett, eaa917f9-fb8e-4b44-9e8d-b3b78c9c1abc
# Jon, 1f9f033f-c890-402b-83f3-74affa44dabc
# Olivia, Olivia, hume
# Kora, 59cfc7ab-e945-43de-ad1a-471daa379c67, hume
```

3) Launch with Docker Compose (recommended)
```bash
docker compose up -d --build
```
This starts the Wyoming TTS service on `tcp://0.0.0.0:10201`.

Alternatively, docker run:
```bash
docker build -t hume-tts:latest .
docker run -d --name hume-tts \
  --restart unless-stopped \
  --env-file ./.env \
  -v $(pwd)/voices.txt:/app/voices.txt:ro \
  -p 10201:10201 hume-tts:latest
```

## Home Assistant integration
- In HA, add or reload the Wyoming integration to discover the service (port 10201)
- The voice dropdown is populated from `voices.txt` (names are case-insensitive)
- Send a short test message via `tts.speak`

## voices.txt format
Text file, one entry per line:
```
name, id_or_name[, provider]
```
- Provider is optional. Default is `custom` (your own/custom voice UUID)
- Use `hume` for Hume Voice Library voices. If the second field is not a UUID, it is treated as a library voice name
- Lines starting with `#` or blank lines are ignored

Examples:
```
# Custom voice by UUID
Scarlett, eaa917f9-fb8e-4b44-9e8d-b3b78c9c1abc

# Hume Voice Library by name and by UUID
Olivia, Olivia, hume
Kora, 59cfc7ab-e945-43de-ad1a-471daa379c67, hume
```

## Configuration
Set via CLI flags or environment variables (CLI > env > defaults). `.env` is used by docker compose.

Important env vars:
- `HUME_API_KEY` and `HUME_SECRET_KEY` (required)
- `HUME_TTS_URI` (default: `tcp://0.0.0.0:10201`)
- `HUME_TTS_FORMAT` (default: `wav`)
- `HUME_TTS_INSTANT_MODE` (default: `true`)
- `HUME_TTS_SAMPLE_RATE` (default: `22050`)
- `HUME_TTS_SAMPLES_PER_CHUNK` (default: `512`)
- `HUME_TTS_LOG_FILE` (default: `/tmp/hume-tts.log`)
- `HUME_TTS_VOICES_FILE` (default: `/app/voices.txt` if present)
- `HUME_TTS_DEFAULT_VOICE_ID` (optional)
- `HUME_TTS_DEBUG` (`1/true/yes/on` to enable debug logs)

## Recommended settings
- Hume output: WAV; `instant_mode=true`
- PCM: 22050 Hz, 16-bit, mono
- `samples_per_chunk=512`

## Security
- Do not commit `.env` or `secrets.env` (they are gitignored)
- Provide API keys via `.env` or `--env-file` to Docker
- Container runs as a non-root user and exposes only TCP 10201

## Troubleshooting
- Check logs: `docker logs -f hume-tts` or inside container `/tmp/hume-tts.log`
- Port busy? Adjust `HUME_TTS_URI` or host port mapping
- No voices? Ensure `voices.txt` is mounted and readable

## License
MIT. See `LICENSE`.

## Credits
- Hume AI Octave TTS: https://dev.hume.ai/docs
- Wyoming protocol: https://github.com/rhasspy/wyoming
