"""Transcribe every video on a YouTube channel to one JSON file per video.

Idempotent: every finished video is recorded in a per-machine ledger
($XDG_STATE_HOME/yt_transcribe/processed.jsonl, default ~/.local/state/...),
and videos in the ledger or with an existing output file are skipped, so the
command can be re-run after interruptions or failures from any directory.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mlx_whisper
import yt_dlp

SCHEMA_VERSION = 1
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
PAID_AVAILABILITY = {"subscriber_only", "premium_only"}
LEDGER_PATH = (
    Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    / "yt_transcribe"
    / "processed.jsonl"
)

log = logging.getLogger("transcribe_channel")


# --- CLI -------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", help="Channel URL, e.g. https://www.youtube.com/@handle")
    parser.add_argument("--out", type=Path, default=Path("transcripts"), help="Output directory")
    parser.add_argument(
        "--tabs",
        default="videos",
        help="Comma-separated channel tabs to include: videos,shorts,streams (default: videos)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="MLX Whisper model repo")
    parser.add_argument("--language", default=None, help="Force language code (e.g. en); default auto-detect")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N videos (for testing)")
    parser.add_argument(
        "--cookies-from-browser",
        default=None,
        help="Pass browser cookies to yt-dlp (e.g. chrome, safari) if YouTube demands sign-in",
    )
    parser.add_argument(
        "--include-members",
        action="store_true",
        help="Also transcribe members-only/premium videos (requires --cookies-from-browser "
        "for a browser signed in to an account with access)",
    )
    args = parser.parse_args(argv)
    if args.include_members and not args.cookies_from_browser:
        parser.error("--include-members requires --cookies-from-browser")
    return args


# --- Listing ---------------------------------------------------------------

def base_ydl_opts(cookies_from_browser: str | None) -> dict[str, Any]:
    opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "noprogress": True}
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    return opts


def list_channel_videos(
    channel: str, tabs: list[str], cookies_from_browser: str | None, include_members: bool = False
) -> list[dict[str, str]]:
    """Return [{id, title}] for every video on the requested channel tabs, de-duplicated."""
    root = channel.rstrip("/")
    for tab in ("videos", "shorts", "streams"):
        root = root.removesuffix(f"/{tab}")

    opts = base_ydl_opts(cookies_from_browser) | {"extract_flat": "in_playlist"}
    seen: dict[str, dict[str, str]] = {}
    skipped_paid = 0
    with yt_dlp.YoutubeDL(opts) as ydl:
        for tab in tabs:
            url = f"{root}/{tab}"
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as err:
                log.warning("could not list %s: %s", url, err)
                continue
            for entry in info.get("entries") or []:
                if not entry or not entry.get("id") or entry["id"] in seen:
                    continue
                # Channel-membership and YouTube Premium videos can't be downloaded without paying.
                if not include_members and entry.get("availability") in PAID_AVAILABILITY:
                    skipped_paid += 1
                    continue
                seen[entry["id"]] = {"id": entry["id"], "title": entry.get("title") or ""}
            log.info("listed %s: %d unique videos so far", url, len(seen))
    if skipped_paid:
        log.info("skipped %d members-only/premium videos", skipped_paid)
    return list(seen.values())


# --- Per-video work --------------------------------------------------------

def download_audio(video_id: str, workdir: Path, cookies_from_browser: str | None) -> tuple[dict[str, Any], Path]:
    """Download the best audio-only stream; return (yt-dlp info dict, audio path)."""
    opts = base_ydl_opts(cookies_from_browser) | {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        # Be polite: yt-dlp sleeps a random 2-5s before each download.
        "sleep_interval": 2,
        "max_sleep_interval": 5,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    return info, Path(info["requested_downloads"][0]["filepath"])


def transcribe(audio: Path, model: str, language: str | None) -> dict[str, Any]:
    return mlx_whisper.transcribe(
        str(audio),
        path_or_hf_repo=model,
        language=language,
        # Stops one bad segment from snowballing into repeated-phrase loops.
        condition_on_previous_text=False,
        # False shows a progress bar over the audio; None would be silent.
        verbose=False,
    )


def iso_date(yyyymmdd: str | None) -> str | None:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return None
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def build_document(info: dict[str, Any], result: dict[str, Any], model: str) -> dict[str, Any]:
    """Shape the output so a search indexer can use it without re-reading yt-dlp output."""
    video_id = info["id"]
    segments = [
        {
            "start": round(seg["start"], 2),
            "end": round(seg["end"], 2),
            "text": seg["text"].strip(),
            # Deep link that starts playback at this segment.
            "url": f"https://www.youtube.com/watch?v={video_id}&t={int(seg['start'])}s",
        }
        for seg in result.get("segments", [])
        if seg["text"].strip()
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "video": {
            "id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": info.get("title"),
            "description": info.get("description"),
            "channel": info.get("channel"),
            "channel_id": info.get("channel_id"),
            "upload_date": iso_date(info.get("upload_date")),
            "duration_seconds": info.get("duration"),
            "tags": info.get("tags") or [],
            "categories": info.get("categories") or [],
            "thumbnail": info.get("thumbnail"),
            "view_count": info.get("view_count"),
        },
        "transcript": {
            "model": model,
            "language": result.get("language"),
            "transcribed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "text": " ".join(s["text"] for s in segments),
            "segments": segments,
        },
    }


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write via temp file + rename so a crash never leaves a half-written JSON behind."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_ledger() -> dict[str, str]:
    """Return {video_id: output_path} for every video already transcribed on this machine."""
    if not LEDGER_PATH.exists():
        return {}
    done: dict[str, str] = {}
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a line truncated by a crash mid-append
        done[entry["id"]] = entry["path"]
    return done


def append_ledger(video_id: str, output: Path) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": video_id, "path": str(output.resolve()), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def record_failure(out: Path, video: dict[str, str], err: Exception) -> None:
    entry = {
        "id": video["id"],
        "title": video["title"],
        "error": f"{type(err).__name__}: {err}",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with (out / "failures.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def process_video(video: dict[str, str], args: argparse.Namespace, videos_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="yt-audio-") as tmp:
        log.info("downloading audio")
        info, audio = download_audio(video["id"], Path(tmp), args.cookies_from_browser)
        seconds = int(info.get("duration") or 0)
        log.info("transcribing %dm%02ds of audio", seconds // 60, seconds % 60)
        started = time.monotonic()
        result = transcribe(audio, args.model, args.language)
        elapsed = time.monotonic() - started
    doc = build_document(info, result, args.model)
    output = videos_dir / f"{video['id']}.json"
    write_json_atomic(output, doc)
    append_ledger(video["id"], output)
    duration = info.get("duration") or 0
    speed = f", {duration / elapsed:.1f}x realtime" if duration and elapsed else ""
    log.info("done %s (%ss audio in %.0fs%s)", video["id"], duration, elapsed, speed)


# --- Entry point -----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    # Libraries stay at WARNING; only this script logs progress at INFO.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    args = parse_args(argv)
    videos_dir = args.out / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)

    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
    videos = list_channel_videos(args.channel, tabs, args.cookies_from_browser, args.include_members)
    if not videos:
        log.error("no videos found for %s (tabs: %s)", args.channel, ",".join(tabs))
        return 1
    if args.limit:
        videos = videos[: args.limit]

    done = load_ledger()
    # Register outputs written before the ledger existed so later runs from any directory skip them.
    for v in videos:
        output = videos_dir / f"{v['id']}.json"
        if v["id"] not in done and output.exists():
            append_ledger(v["id"], output)
            done[v["id"]] = str(output.resolve())

    todo = [v for v in videos if v["id"] not in done]
    log.info("%d videos total, %d already done, %d to process", len(videos), len(videos) - len(todo), len(todo))
    log.info("ledger: %s", LEDGER_PATH)

    failed = 0
    for n, video in enumerate(todo, 1):
        log.info("[%d/%d] %s  %s", n, len(todo), video["id"], video["title"])
        try:
            process_video(video, args, videos_dir)
        except KeyboardInterrupt:
            log.warning("interrupted; re-run the same command to resume")
            return 130
        except Exception as err:  # keep going; one bad video shouldn't stop a channel run
            failed += 1
            log.error("failed %s: %s", video["id"], err)
            record_failure(args.out, video, err)

    log.info("finished: %d ok, %d failed (see %s)", len(todo) - failed, failed, args.out / "failures.jsonl")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
