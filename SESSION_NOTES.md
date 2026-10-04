# yt_transcribe: session handoff (2026-09-30)

Context for picking this project up in a new Claude Code session.

## Goal

One-time transcription of every video on a YouTube channel into per-video JSON
files, to feed a web-based search service later. First target channel:
`https://www.youtube.com/@twogirlsoneghost`.

## Machines

- **Mac mini (M4, 16 GB, `Douglass-Mac-mini.local`)**: runs the transcription.
  The project lives at `~/agents/yt_transcribe/` there. ffmpeg is installed via
  Homebrew; `uv` was installed by the user.
- **dsl-air (laptop)**: where the previous Claude session ran, and where the
  code was written and tested. The files in this folder are copies of that code.
  The previous session could only write files to dsl-air, not to the mini, which
  caused confusion. Start the next session on the machine whose files you want
  changed.

## Stack and decisions

| Decision | Choice | Why |
|---|---|---|
| Download | yt-dlp Python API, audio only (`bestaudio/best`) | No video bytes needed |
| Speech-to-text | `mlx-whisper` + `mlx-community/whisper-large-v3-turbo` | Native on Apple Silicon; ~2–3 GB memory at runtime, fits 16 GB |
| Decoding | `condition_on_previous_text=False` | Prevents repeated-phrase hallucination loops |
| Packaging | `uv` project, entry point `transcribe-channel` | User standard |
| Output | One JSON per video, written atomically (temp file + rename) | A crash never leaves half-written JSON |
| Politeness | yt-dlp sleeps a random 2–5 s before each download | Avoid YouTube throttling |

## Current behaviour

- Lists the channel's `videos` tab (`--tabs videos,shorts,streams` to add more),
  de-duplicated by video ID.
- **Members-only / premium videos are skipped by default.** The flat listing
  marks them `availability: subscriber_only`. On @twogirlsoneghost that's 476
  free and 77 members-only (553 total) as of 2026-09-30.
- `--include-members` includes them. It requires `--cookies-from-browser
  <chrome|firefox|safari>` (enforced by argparse). Borrowing browser cookies is
  the only YouTube login yt-dlp supports; it has no username/password option.
- **Idempotent per machine:** finished videos are appended to the ledger
  `~/.local/state/yt_transcribe/processed.jsonl` (`$XDG_STATE_HOME` respected).
  Anything in the ledger is skipped regardless of cwd or `--out`. Existing
  output JSONs are backfilled into the ledger on the next run. Delete a
  ledger line to force a redo.
- Failures go to `<out>/failures.jsonl` and are retried next run.
- Audio is downloaded to a `TemporaryDirectory` (`$TMPDIR/yt-audio-*`) and
  deleted after each video. The model is cached in `~/.cache/huggingface/hub/`.
- Progress logging: "downloading audio" → "transcribing XmYYs of audio" →
  mlx-whisper progress bar → "done … Nx realtime". Only this script logs at
  INFO; library loggers (httpx, huggingface_hub) are held at WARNING.

## JSON schema (`schema_version: 1`)

```
video:      id, url, title, description, channel, channel_id, upload_date (YYYY-MM-DD),
            duration_seconds, tags, categories, thumbnail, view_count
transcript: model, language, transcribed_at, text,
            segments[]: start, end, text, url (watch?v=ID&t=<start>s deep link)
```

Plan for search: index segments (or windows of consecutive segments) as documents
that carry the parent `video` fields, so each hit links straight to the timestamp.

## Commands (on the mini)

```bash
cd ~/agents/yt_transcribe && uv sync
# free videos, whole channel; keep Mac awake; no Hugging Face network calls once the model is cached
HF_HUB_OFFLINE=1 caffeinate -i uv run transcribe-channel https://www.youtube.com/@twogirlsoneghost
# add members-only videos using a signed-in browser's cookies
HF_HUB_OFFLINE=1 caffeinate -i uv run transcribe-channel https://www.youtube.com/@twogirlsoneghost --include-members --cookies-from-browser chrome
```

## Measured performance

- On dsl-air, with the model warm: 9–17x realtime on short videos. Not yet
  measured on the M4 mini; the script logs speed per video.
- Episodes on this channel are about 70 min (about 68 MB of audio). At 8–15x
  realtime, expect 5–10 min per episode, so roughly 40–80 h for 476 episodes.
  That estimate is extrapolated, not measured.
- First run on the mini: `--limit 3` was in progress when this session ended.

## Known gotchas

- The Hugging Face "unauthenticated requests" warning is harmless (public model).
- Browser cookies: Chrome needs Keychain "Always Allow" for "Chrome Safe
  Storage"; Safari needs Full Disk Access for the terminal app; Firefox needs
  nothing.
- yt-dlp warns that heavy logged-in downloading can get an account flagged.
- A "PO token" error on logged-in downloads means YouTube wants an extra check:
  see https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
- "Members One Week Early" videos become public later (re-run to pick them up).
  "Members Ad Free" videos duplicate free episodes.

## Open items / next steps

1. Confirm the `--limit 3` test run on the mini finished and check the speed it logged.
2. Optional: when `--include-members` is set, skip titles containing
   "Members Ad Free" to avoid duplicate transcripts (offered, not decided).
3. Run the full channel.
4. Later: build the search service over `transcripts/videos/*.json`.
