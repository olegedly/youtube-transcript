# youtube-transcript

An agent skill for getting what a YouTube video actually says: pull the caption track, turn
machine-formatted captions into readable text, then summarise it honestly.

The hard part isn't downloading captions — it's that YouTube returns them as `json3`/`vtt` with
rolling duplication and no sentence structure, that caption tracks throttle individually with
HTTP 429, and that it's easy to summarise the wrong track without noticing. This skill handles
those failure modes explicitly and tells you when it has substituted something.

## Contents

- `SKILL.md` — the skill definition and instructions for the agent.
- `scripts/fetch_transcript.py` — the fetcher/parser. `yt-dlp` is the only reliable route to
  caption tracks; there is no working API substitute without a key.

## Requirements

- Python 3 (standard library only — no `pip install` step)
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) on `PATH`

No YouTube cookies are used, so age-restricted and login-gated videos cannot be fetched.

## Usage

```bash
python3 scripts/fetch_transcript.py "<url>" --outdir ./.yt/<id>
```

`<id>` is the 11-character video id from the URL. Default output directory is
`./yt-transcript-<id>`.

| Flag | Use |
| --- | --- |
| `--list-langs` | Show original audio language and every available track, without downloading |
| `--meta-only` | Title/channel/duration/date/views only |
| `--lang en-orig` | Force one published track key (see exit code 4 below) |
| `--text-lang en` | Ask for YouTube's machine-translated track |
| `--keep-raw` | Keep the raw caption file alongside the transcript |
| `--stdout` | Print instead of writing files (short videos only) |

With no `--lang`, the script picks the track itself — human-written tracks first, then the
automatic track in the original audio language — and falls back through a short candidate list
if the first one is throttled, warning on stderr whenever it substitutes. Check
`caption_language` in `meta.txt` before writing anything that depends on which track you got.

## Outputs

- `transcript.txt` — `[m:ss] text`, one line per caption block. The file to read.
- `transcript.flat.txt` — single line, for word counts and grep.
- `meta.txt` — id, title, channel, duration, upload date, views, likes, original language, and
  which caption track was used. Written even when the caption download fails.

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Fine | Read `transcript.txt` |
| 2 | URL or tooling is broken (private, removed, no `yt-dlp`) | Do not retry; report the failure |
| 3 | The video genuinely has no caption track | Say so; never summarise from the title |
| 4 | Captions exist but every candidate track was throttled | Wait ~60s, retry once; `--list-langs` then a specific `--lang` often works |

## Installing as an agent skill

The directory is self-contained. Place or symlink it into your agent's skills directory, e.g.:

```bash
ln -s "$PWD" ~/.agents/skills/youtube-transcript
```

## License

No license granted. This repository is all-rights-reserved by default — add a license if you
intend others to reuse it.
