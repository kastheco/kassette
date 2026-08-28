#!/usr/bin/env python3
"""Generate a listenable four-way TTS comparison from one text sample."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import time
import wave
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import ormsgpack
from websockets.asyncio.client import connect

from kassette.settings import load_settings

SAMPLE_RATE = 24_000
DEFAULT_TEXT = (
    "Hey Kas, I checked the voice pipeline. The build finished in two minutes and "
    "seventeen seconds, and the estimated total is forty-two dollars and eighty cents. "
    "Honestly, that went better than I expected. Want me to run the final test?"
)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    provider: str
    model: str
    filename: str
    characters: int
    audio_seconds: float
    first_audio_ms: float
    total_ms: float
    estimated_cost_usd: float


def write_pcm_wav(path: Path, pcm: bytes) -> float:
    """Wrap mono 16-bit PCM in a WAV container and return its duration."""
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    return len(pcm) / (SAMPLE_RATE * 2)


async def generate_elevenlabs(
    *, api_key: str, voice_id: str, model: str, text: str, output: Path
) -> GenerationResult:
    """Generate raw PCM through ElevenLabs' streaming HTTP endpoint."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    params = {"output_format": "pcm_24000"}
    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/pcm",
        "Content-Type": "application/json",
    }
    payload = {"text": text, "model_id": model}
    timeout = aiohttp.ClientTimeout(total=120)
    started = time.perf_counter()
    first_audio_at: float | None = None
    chunks: list[bytes] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, params=params, headers=headers, json=payload) as response:
            if response.status != 200:
                detail = (await response.text())[:1_000]
                raise RuntimeError(f"ElevenLabs {model} returned HTTP {response.status}: {detail}")
            async for chunk in response.content.iter_chunked(16_384):
                if chunk and first_audio_at is None:
                    first_audio_at = time.perf_counter()
                chunks.append(chunk)
    finished = time.perf_counter()
    pcm = b"".join(chunks)
    if not pcm or first_audio_at is None:
        raise RuntimeError(f"ElevenLabs {model} returned no audio")
    duration = write_pcm_wav(output, pcm)
    price_per_character = 0.00005 if "flash" in model else 0.0001
    return GenerationResult(
        provider="ElevenLabs",
        model=model,
        filename=output.name,
        characters=len(text),
        audio_seconds=duration,
        first_audio_ms=(first_audio_at - started) * 1_000,
        total_ms=(finished - started) * 1_000,
        estimated_cost_usd=len(text) * price_per_character,
    )


async def generate_fish(
    *, api_key: str, voice_id: str | None, model: str, text: str, output: Path
) -> GenerationResult:
    """Generate raw PCM through Fish Audio's live WebSocket endpoint."""
    headers = {"Authorization": f"Bearer {api_key}", "model": model}
    started = time.perf_counter()
    first_audio_at: float | None = None
    chunks: list[bytes] = []
    async with connect(
        "wss://api.fish.audio/v1/tts/live",
        additional_headers=headers,
        open_timeout=30,
        close_timeout=10,
        max_size=None,
    ) as websocket:
        start_message = {
            "event": "start",
            "request": {
                "text": "",
                "sample_rate": SAMPLE_RATE,
                "latency": "balanced",
                "format": "pcm",
                "normalize": True,
                "prosody": {"speed": 1.0, "volume": 0},
                "reference_id": voice_id,
            },
        }
        await websocket.send(ormsgpack.packb(start_message))
        await websocket.send(ormsgpack.packb({"event": "text", "text": text}))
        await websocket.send(ormsgpack.packb({"event": "flush"}))
        await websocket.send(ormsgpack.packb({"event": "stop"}))

        async with asyncio.timeout(120):
            async for raw_message in websocket:
                if not isinstance(raw_message, bytes):
                    continue
                message: Any = ormsgpack.unpackb(raw_message)
                if not isinstance(message, dict):
                    continue
                event = message.get("event")
                if event == "audio":
                    audio = message.get("audio")
                    if isinstance(audio, bytes) and audio:
                        if first_audio_at is None:
                            first_audio_at = time.perf_counter()
                        chunks.append(audio)
                elif event == "finish":
                    if message.get("reason") == "error":
                        raise RuntimeError(f"Fish Audio generation failed: {message}")
                    break
    finished = time.perf_counter()
    pcm = b"".join(chunks)
    if not pcm or first_audio_at is None:
        raise RuntimeError("Fish Audio returned no audio")
    duration = write_pcm_wav(output, pcm)
    return GenerationResult(
        provider="Fish Audio",
        model=model,
        filename=output.name,
        characters=len(text),
        audio_seconds=duration,
        first_audio_ms=(first_audio_at - started) * 1_000,
        total_ms=(finished - started) * 1_000,
        estimated_cost_usd=(
            0.0 if model.endswith("-free") else len(text.encode("utf-8")) * 0.000015
        ),
    )


def write_report(
    output_dir: Path,
    text: str,
    results: list[GenerationResult],
    errors: list[str],
) -> None:
    """Write machine-readable results and a small blind-friendly listening page."""
    manifest = {
        "text": text,
        "generated_at": datetime.now(UTC).isoformat(),
        "results": [asdict(result) for result in results],
        "errors": errors,
    }
    (output_dir / "results.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    cards = "\n".join(
        f"""
        <article>
          <h2>{html.escape(result.provider)} · {html.escape(result.model)}</h2>
          <audio controls preload="metadata" src="{html.escape(result.filename)}"></audio>
          <dl>
            <div><dt>First audio</dt><dd>{result.first_audio_ms:.0f} ms</dd></div>
            <div><dt>Total request</dt><dd>{result.total_ms:.0f} ms</dd></div>
            <div><dt>Audio duration</dt><dd>{result.audio_seconds:.2f} s</dd></div>
            <div><dt>Marginal list cost</dt><dd>${result.estimated_cost_usd:.4f}</dd></div>
          </dl>
        </article>"""
        for result in results
    )
    error_block = (
        '<section class="errors"><h2>Generation errors</h2><ul>'
        + "".join(f"<li>{html.escape(error)}</li>" for error in errors)
        + "</ul></section>"
        if errors
        else ""
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TTS comparison</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: ui-sans-serif, system-ui, sans-serif;
      background: #191724;
      color: #e0def4;
    }}
    body {{ max-width: 920px; margin: 0 auto; padding: 32px 20px 64px; }}
    p {{ color: #908caa; line-height: 1.6; }}
    .errors {{
      margin: 16px 0;
      padding: 12px 18px;
      border: 1px solid #eb6f92;
      border-radius: 10px;
    }}
    main {{ display: grid; gap: 16px; }}
    article {{
      padding: 20px;
      border: 1px solid #403d52;
      border-radius: 14px;
      background: #1f1d2e;
    }}
    audio {{ width: 100%; }}
    dl {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }}
    dl div {{ padding: 10px; border-radius: 9px; background: #26233a; }}
    dt {{ color: #908caa; font-size: 12px; }}
    dd {{ margin: 4px 0 0; font-variant-numeric: tabular-nums; }}
  </style>
</head>
<body>
  <h1>Same text, four TTS models</h1>
  <p>{html.escape(text)}</p>
  {error_block}
  <main>{cards}</main>
</body>
</html>
"""
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="Text spoken by all three models")
    source.add_argument("--text-file", type=Path, help="UTF-8 text file spoken by all models")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory (default: artifacts/tts-comparison/<timestamp>)",
    )
    return parser.parse_args()


async def run() -> tuple[Path, list[str]]:
    args = parse_args()
    text = (
        args.text_file.read_text(encoding="utf-8").strip()
        if args.text_file
        else (args.text or DEFAULT_TEXT).strip()
    )
    if not text:
        raise SystemExit("Comparison text cannot be empty")
    output_dir = args.output or Path("artifacts/tts-comparison") / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    settings = load_settings()
    eleven_key, eleven_voice, fish_key = settings.comparison_credentials()
    generators = [
        (
            "ElevenLabs Flash v2.5",
            generate_elevenlabs(
                api_key=eleven_key,
                voice_id=eleven_voice,
                model="eleven_flash_v2_5",
                text=text,
                output=output_dir / "01-eleven-flash-v2.5.wav",
            ),
        ),
        (
            "ElevenLabs v3 Conversational",
            generate_elevenlabs(
                api_key=eleven_key,
                voice_id=eleven_voice,
                model="eleven_v3_conversational",
                text=text,
                output=output_dir / "02-eleven-v3-conversational.wav",
            ),
        ),
        (
            "Fish Audio S2.1 Pro Free",
            generate_fish(
                api_key=fish_key,
                voice_id=settings.fish_voice_id,
                model="s2.1-pro-free",
                text=text,
                output=output_dir / "03-fish-s2.1-pro-free.wav",
            ),
        ),
        (
            "Fish Audio S2.1 Pro",
            generate_fish(
                api_key=fish_key,
                voice_id=settings.fish_voice_id,
                model="s2.1-pro",
                text=text,
                output=output_dir / "04-fish-s2.1-pro.wav",
            ),
        ),
    ]
    results: list[GenerationResult] = []
    errors: list[str] = []
    for label, generator in generators:
        try:
            results.append(await generator)
        except Exception as error:
            errors.append(f"{label}: {error}")
    write_report(output_dir, text, results, errors)
    return output_dir, errors


def main() -> None:
    output_dir, errors = asyncio.run(run())
    print(f"Comparison ready: {output_dir / 'index.html'}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
