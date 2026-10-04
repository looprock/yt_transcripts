# yt_transcribe

Transcribes every video on a YouTube channel to one JSON file per video, using
yt-dlp for audio and mlx-whisper (`whisper-large-v3-turbo`) on Apple Silicon.

## Usage

```bash
uv sync
uv run transcribe-channel https://www.youtube.com/@handle --out transcripts
```

| Flag | Default | Purpose |
|---|---|---|
| `--tabs` | `videos` | Channel tabs to include: `videos,shorts,streams` |
| `--language` | auto | Force a language code (e.g. `en`) |
| `--limit N` | all | Only the first N videos (testing) |
| `--model` | `mlx-community/whisper-large-v3-turbo` | Any MLX Whisper repo |
| `--cookies-from-browser` | none | `chrome`/`safari` if YouTube asks you to sign in |
| `--include-members` | off | Also transcribe members-only/premium videos; requires `--cookies-from-browser` for a browser signed in to an account with access |

Members-only and premium videos are skipped by default.

The script is idempotent per machine. Every finished video is recorded in
`~/.local/state/yt_transcribe/processed.jsonl` (or `$XDG_STATE_HOME/...`), and
any video in that ledger is skipped regardless of working directory or `--out`.
Existing output files from earlier runs are added to the ledger automatically.
To force a video to be redone, delete its line from the ledger.

Failures (removed, private, upcoming premieres) go to `<out>/failures.jsonl`
and are retried on the next run.

## Output

```
transcripts/
  videos/<video_id>.json
  failures.jsonl
```

Each file (`schema_version: 1`):

```json
{
  "schema_version": 1,
  "video": {
    "id", "url", "title", "description", "channel", "channel_id",
    "upload_date": "YYYY-MM-DD", "duration_seconds", "tags", "categories",
    "thumbnail", "view_count"
  },
  "transcript": {
    "model", "language", "transcribed_at", "text",
    "segments": [{ "start", "end", "text", "url": "...&t=<start>s" }]
  }
}
```

For search, index `transcript.segments` (or windows of several consecutive
segments) as documents that carry the parent `video` fields. Each hit then
links straight to the timestamp.
