---
name: youtube-transcript
description: Fetch and summarise the transcript of a YouTube video. Use when the user shares a youtube.com or youtu.be link, or asks for a video's transcript, captions, subtitles, or a summary/gist of what a video says.
---

# YouTube transcripts

Get what a video actually says, then summarise it. The work is: pull the caption track, turn machine-formatted captions into readable text, read it, and report on it honestly.

## Quick start

```bash
python3 scripts/fetch_transcript.py "<url>" --outdir ./.yt/<id>
```

`<id>` is the 11-character video id from the URL. One run gives you metadata, the transcript, and a warning if anything was substituted. Useful flags:

| Flag | Use |
| --- | --- |
| `--list-langs` | Show original audio language and every available track, without downloading |
| `--meta-only` | Title/channel/duration/date/views only |
| `--lang en-orig` | Force one published track key (see "When a fetch fails") |
| `--text-lang en` | Ask for YouTube's machine-translated track |
| `--keep-raw` | Keep the raw caption file alongside the transcript |
| `--stdout` | Print instead of writing files (short videos only) |

With no `--lang` the script chooses the track itself — human-written tracks first, then the automatic track in the original audio language — and it tries a short list of fallback tracks if the first one is throttled, warning on stderr whenever it substitutes. Check `caption_language` in `meta.txt` before you write anything that depends on which track you got.

## What it writes

- `transcript.txt` — `[m:ss] text`, one line per caption block. Read this with the read tool.
- `transcript.flat.txt` — single line, for word counts and grep.
- `meta.txt` — id, title, channel, duration, upload date, views, likes, original language, and which caption track was used. Written even when the caption download fails, so a throttled run still gives you the metadata.

Then read `transcript.txt` with the read tool. Do **not** dump the transcript through a shell pipe: long outputs get truncated to their tail, which silently loses the beginning of the video.

## Clean up after yourself

Write into a scratch directory (e.g. `./.yt/<id>`), and **delete it once you have delivered the summary** unless the user asked to keep the transcript or is likely to want it again. A summary in the conversation is the deliverable; the files are intermediates. Never leave a transcript directory behind just because it was convenient.

## Summarising rules

- **Lead with the metadata**: title, channel, length, upload date, views. The user cannot see the video from your message.
- **Attribute claims to the speaker.** A transcript is one person talking. Write "he argues", "she claims" — never restate what the speaker says as established fact. Attribution is neutral: it marks whose view it is and stops there.
- **Report; do not adjudicate.** Your job is what the video says, not whether it is right. Do not evaluate the speaker's evidence, sample, rhetoric, motives, or expertise, and do not rebut, "balance" or caveat their claims — no verdict sections ("How to read it", "Assessment", "Caveats"), no noting what they left out, no stating what the science "really" says. This holds for opinions you find objectionable and for ones you agree with; both are simply reported as the speaker's position. The user can judge the content themselves.
- **Judge the artifact, never the speaker.** Comment on caption reliability when it affects the words — a track is auto-generated so wording is approximate, or a name is visibly garbled — because that is about the transcript you produced, not about the person in it. Keep the two clearly separate.
- **Structure by the speaker's own argument**, not by chronology. A gist that walks through the video minute by minute is not a gist.
- **Prefer the original language.** Read the original-language transcript yourself rather than using `--text-lang`; YouTube's auto-translation mangles names, political vocabulary, and idioms badly. Summarise in the user's language.

## When a fetch fails

Read the exit code before deciding what to do. Retrying the wrong failure wastes minutes and makes the throttling worse.

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Fine | Read `transcript.txt` |
| 2 | URL or tooling is broken (private, removed, no `yt-dlp`) | Do not retry. Report the failure |
| 3 | The video genuinely has no caption track | Say so. Never summarise from the title or description |
| 4 | Captions exist but every candidate track was throttled | Wait a minute, then retry once |

Escalation ladder for a throttled flake (exit 4), in order — stop as soon as one works:

1. Wait ~60 seconds and rerun the same command. The candidate list usually finds a track that answers.
2. `--list-langs` to see the exact keys, then `--lang <key>` with a specific one. The `-orig` key is often the one that is not throttled.
3. Re-run once more. If it still fails, tell the user the video has captions but YouTube is refusing to serve them right now, and report the title/channel/date from the `meta.txt` that was written anyway.

Do not fire off several fetches in parallel to "try harder" — parallel `yt-dlp` caption requests are the usual cause of the 429 in the first place.

## Gotchas

- **`/tmp` does not persist between shell calls in this harness.** It is a private per-invocation namespace: a file written to `/tmp` in one call is gone in the next. Never split download and parse across two calls, and write transcripts to a real workspace path. The script downloads and parses inside a single process run for exactly this reason.
- **HTTP 429 on captions is normal — and it throttles tracks *individually*.** On a video whose audio language was reported as `en-US`, the bare `en` automatic track returned 429 on every attempt while `en-orig` served fine in the same minute. So the script builds an ordered candidate list (human-written tracks, then `<base>-orig`, then `<base>`, then the original language for a requested translation) and moves to the next candidate instead of retrying one track to death. It uses fewer retries per track when fallbacks exist, and warns on stderr if it substitutes anything.
- **A regional audio language is not a track key.** YouTube reports `en-US`, `de-DE`, `pt-BR`, but publishes tracks as `en`, `de`, `pt`. The script strips the region and the `-orig` suffix before matching, so never pass `--lang en-US`; pass `en`, or leave `--lang` off entirely.
- **Translated tracks are labelled honestly.** `caption_language` reports the track actually used and `caption_note` appears only when that track is genuinely a translation. Same-language tracks are never called translations: `en` or `en-orig` against an audio language of `en-US` is the original wording, and a `-orig` suffix always means the source track.
- **Human-written captions beat auto-generated ones**, and the script prefers them per language. A track list that shows only `automatic_captions` means no human transcript exists for that video — worth telling the user if they need exact quotes.
- **Permanent failures are not retried**: private, members-only, age-restricted, and removed videos fail fast. This environment has **no YouTube cookies**, so age-restricted and login-gated videos cannot be fetched at all — report that rather than retrying.
- **No caption track at all** is a legitimate outcome (exit code 3). Say the video has no captions. Do not invent a summary from the title and thumbnail. `--meta-only` and the video description are the honest fallback.
- **Playlists**: the script passes `--no-playlist`, so it fetches only the linked video. If the user wants a whole playlist, loop over video ids deliberately and expect rate limiting.

## If yt-dlp is unavailable

`yt-dlp` is the only reliable route to caption tracks; there is no working API substitute without a key. Fall back to `web_fetch` on the watch URL to get at least the title, channel, description, and chapters, then tell the user you could not retrieve the spoken content. Do not present a description-derived summary as a transcript summary.

Transcribing audio locally (downloading with `yt-dlp -x` plus a speech-to-text model) is possible in principle but needs a model that is not installed here — offer it as a setup task, not as something you can do immediately.
