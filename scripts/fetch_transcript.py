#!/usr/bin/env python3
"""Fetch a YouTube video's captions with yt-dlp and emit clean, readable text.

Why this exists: YouTube caption tracks come back as machine-formatted json3/vtt
with rolling duplication and no sentence structure. This normalises that into a
transcript that is cheap to read, and records the metadata needed to report on a
video honestly (including which language the captions are actually in).

Usage:
    fetch_transcript.py <url> [--outdir DIR] [--lang CODE] [--text-lang CODE]
                        [--meta-only] [--list-langs] [--stdout] [--keep-raw]

Outputs written to --outdir (default ./yt-transcript-<id>):
    transcript.txt        timestamped, one line per caption block
    transcript.flat.txt   single-line plain text, for word counts / grep
    meta.txt              title, channel, duration, dates, view/like counts, language
    captions.<lang>.json3 raw track, only with --keep-raw

Exit codes:
    0 ok | 2 URL/tooling failure | 3 no caption track exists
    4 captions exist but every track failed to download (usually HTTP 429 throttling)

On exit 4 the metadata is still written to <outdir>/meta.txt, so a throttled run
yields the title/channel/date rather than nothing at all.

Every yt-dlp call has a hard timeout and the caption phase runs against a wall-clock
budget, so a throttled or stalled video fails in about 90 seconds instead of hanging
indefinitely.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# YouTube rate-limits caption downloads hard (HTTP 429), especially when several
# yt-dlp calls run close together. Backoff is deliberately generous, but every wait
# is also bounded by a phase budget so a dead track cannot stall the whole run.
RETRY_ATTEMPTS = 5
RETRY_BACKOFF_S = 8.0
RETRY_BACKOFF_CAP_S = 45.0

# Hard ceiling on a single yt-dlp invocation, and on the whole run. Without the
# first, a stalled socket blocks forever: no exit code is produced and the caller just
# hangs. Without the second, a throttled video grinds through every candidate and
# every backoff far past the point of usefulness. A 43-minute video's caption track
# downloads in ~3s and its metadata in ~2s, so both ceilings are generous.
#
# A single run is bounded by PHASE_BUDGET_S plus whatever is left of it, because each
# call is given min(SUBPROCESS_TIMEOUT_S, budget remaining) rather than a fresh
# timeout — so the two do not stack.
SUBPROCESS_TIMEOUT_S = 60.0
PHASE_BUDGET_S = 75.0

# The biggest lever on a throttled video is *switching* tracks, so the default path
# sweeps `<lang>-orig` keys when the audio language has no usable track of its own.
# Capped: each candidate costs a subprocess, and a long tail of them is what invites
# the 429 in the first place.
MAX_ORIG_FALLBACKS = 6

# After the no-backoff sweep, only the leading candidates are worth retrying. If the
# whole list failed, the block is per-video or per-IP and hammering the tail helps
# nobody; this keeps the worst case near a dozen requests rather than dozens.
MAX_RETRY_PASS_CANDIDATES = 3


def die(message: str, code: int) -> None:
    """Fail loudly on stderr with a documented exit code."""
    print(message, file=sys.stderr)
    sys.exit(code)


def backoff_delay(attempt: int) -> float:
    """Seconds to wait before the next attempt: exponential, capped, jittered."""
    return min(RETRY_BACKOFF_S * (2 ** (attempt - 1)), RETRY_BACKOFF_CAP_S) + random.uniform(
        0, 3.0
    )


def sleep_within_budget(delay: float, deadline: float) -> bool:
    """Sleep `delay` seconds unless that would overrun `deadline`.

    Returns False when the wait was skipped, which tells the caller to stop rather
    than keep paying for a track that is not answering. The check is against the
    clock, so a long tail of retries cannot add up past the phase budget.
    """
    if time.monotonic() + delay > deadline:
        return False
    time.sleep(delay)
    return True


def remaining_budget(deadline: float) -> float:
    """Seconds left before `deadline`, never below 1s, to bound a single call."""
    return max(1.0, deadline - time.monotonic())


def run_yt_dlp(cmd: list[str], timeout: float = SUBPROCESS_TIMEOUT_S) -> tuple[int, str, str]:
    """Run yt-dlp under a hard timeout, returning (returncode, stdout, stderr).

    A timeout comes back as returncode -1 with an explanatory stderr rather than an
    exception, so every caller treats it as one more transient failure. Without a
    timeout here a stalled connection hangs the script forever instead of failing.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "", f"yt-dlp exceeded {timeout:.0f}s without finishing and was killed"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# Retrying these wastes minutes: they do not resolve on their own, and the
# login/age ones need cookies that this environment has none of.
PERMANENT_ERRORS = (
    "this video is unavailable",
    "video unavailable",
    "this video is not available",
    "private video",
    "members-only",
    "sign in to confirm your age",
    "incomplete youtube id",
    "is not a valid url",
    "unsupported url",
    "no video formats found",
    "has been terminated",
    "account has been terminated",
)


def is_permanent_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in PERMANENT_ERRORS)


def yt_dlp_json(url: str, attempts: int = RETRY_ATTEMPTS, deadline: float | None = None) -> dict:
    """Return yt-dlp's single-video metadata JSON, retrying on transient 429s."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--no-warnings",
        "--socket-timeout",
        "15",
        "--dump-single-json",
        url,
    ]
    if deadline is None:
        deadline = time.monotonic() + PHASE_BUDGET_S
    last = ""
    for attempt in range(1, attempts + 1):
        code, out, err = run_yt_dlp(cmd, timeout=min(SUBPROCESS_TIMEOUT_S, remaining_budget(deadline)))
        if code == 0 and out.strip():
            return json.loads(out)
        last = (err or out).strip()
        if is_permanent_error(last):
            die(f"yt-dlp cannot read this URL (not retryable):\n{last}", 2)
        if attempt < attempts and not sleep_within_budget(backoff_delay(attempt), deadline):
            last += "\nstopped retrying: run budget exhausted"
            break
    die(f"yt-dlp could not read this URL after {attempts} attempts:\n{last}", 2)


def fmt_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "unknown"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_upload_date(raw: str | None) -> str:
    if not raw or len(raw) != 8:
        return raw or "unknown"
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


# YouTube language codes carry a region ("en-US") or a script ("zh-Hans") on top of
# the base language ("en"). Caption tracks are keyed by the base language in almost
# every case, so the region has to be stripped before a track can be matched against
# the reported audio language.
LANGUAGE_ALIASES = {
    # legacy YouTube codes it still advertises under the old key
    "iw": "he",
    "in": "id",
    "ji": "yi",
    "jw": "jv",
    "mo": "ro",
    "tl": "fil",
    "nb": "no",
}


def base_language(code: str) -> str:
    """Reduce a YouTube language code to its base: 'en-US' -> 'en', 'zh-Hans' -> 'zh'."""
    base = code.split("-")[0].strip().lower()
    return LANGUAGE_ALIASES.get(base, base)


def same_language(a: str, b: str) -> bool:
    """True when two YouTube codes name the same spoken language."""
    return base_language(a) == base_language(b)


def is_original_track(lang: str, orig: str | None) -> bool:
    """True when `lang` carries the original audio rather than a translation.

    A '<lang>-orig' key is the source track by definition, and a track whose base
    language matches the audio language is the original wording too. The original
    check here was a plain `lang != orig`, which compared 'en' against 'en-US' and
    so branded the original English transcript "machine-translated from en-US".
    """
    if lang.endswith("-orig"):
        return True
    if not orig:
        return False
    return same_language(lang, orig)


def original_keys(orig: str) -> list[str]:
    """Track keys that carry the audio language's original tracks, best first.

    'en-US' -> ['en-US', 'en-orig', 'en']. The regional code is first because it is
    what the metadata reports, but it is usually not a track key, so the '-orig' and
    bare base forms do the real work.
    """
    base = base_language(orig)
    return [orig, f"{base}-orig", base]


def track_url_params(meta: dict, lang: str, is_auto: bool) -> dict[str, str]:
    """Query parameters of the caption URL YouTube published for one track."""
    pool = (meta.get("automatic_captions") if is_auto else meta.get("subtitles")) or {}
    for entry in pool.get(lang) or []:
        url = entry.get("url") or ""
        if "?" in url:
            return {k: v[0] for k, v in parse_qs(urlparse(url).query).items() if v}
    return {}


def translation_source(meta: dict, lang: str, is_auto: bool) -> str | None:
    """The language a track is machine-translated *from*, or None if it is not one.

    YouTube marks a translation with a `tlang` parameter, and that is the only
    reliable signal. A track requested as `en` on a video whose only ASR source is
    Arabic is published as `lang=ar&tlang=en`. Language codes alone cannot see this:
    `en` against an audio language of `en-US` looks exactly like the original
    wording, which is how YouTube's Arabic-to-English round-trip came to be reported
    as the original wording.
    """
    params = track_url_params(meta, lang, is_auto)
    source, target = params.get("lang"), params.get("tlang")
    if source and target and not same_language(source, target):
        return source
    return None


def caption_provenance(meta: dict, lang: str, is_auto: bool, orig: str | None) -> str | None:
    """One honest line about what the chosen track actually is, or None if plain.

    Three ways a track can be something other than the video's own wording:

    * a machine translation, which only the `tlang` URL parameter reveals;
    * the source track of an auto-dub (`<lang>-orig`) whose language is not the
      video's audio language — genuine ASR, but of the dubbed audio;
    * simply a different language from the audio.
    """
    source = translation_source(meta, lang, is_auto)
    if source:
        return f"machine-translated from {source!r} by YouTube, not the original wording"
    if not is_original_track(lang, orig):
        return f"not the video's original audio language ({orig!r})"
    base = base_language(lang)
    if orig and not same_language(base, orig):
        return (
            f"transcribes YouTube's auto-dubbed {base!r} audio, "
            f"not the video's original {orig!r} audio"
        )
    return None


def track_candidates(meta: dict, wanted: str | None) -> list[tuple[str, bool]]:
    """Caption tracks to try, best first, as (language_code, is_automatic).

    Preference order deliberately favours the *original* audio language: YouTube's
    auto-translated tracks are machine-translated from it and lose nuance badly,
    especially for names, political vocabulary and idioms.

    Several candidates are returned rather than one, for two reasons found the hard
    way on a video whose audio language was reported as 'en-US':

    * YouTube advertises the source track of an English video under both 'en' and
      'en-orig', and will 429 one while serving the other fine in the same minute.
    * The reported audio language ('en-US') is often not a track key at all, so a
      failed track used to end the run with "Language 'en-US' has no caption track"
      even though a perfectly good 'en-orig' was listed right there.

    Hence: an ordered list, with the '<lang>-orig' key ahead of the bare one for
    automatic tracks, since '-orig' names the source rather than a variant. A track
    in a *different* language is only ever appended for an explicit request that is
    itself a translation, so asking for 'de' never quietly returns English.

    One more case, and the reason for the sweep at the end of the default path: when
    YouTube has auto-dubbed a video, the audio language can have no published track of
    its own. The original-language caption may then be a *translation of the dub*
    (`lang=ar&tlang=en` behind an `en` key), while the only genuine ASR lives under
    another language's `<lang>-orig`. Those are appended as a last resort so the run
    still yields the video's content, with meta.txt recording what was really used.
    An explicit --lang stays strict, so asking for 'de' never returns Arabic.
    """
    manual = meta.get("subtitles") or {}
    auto = meta.get("automatic_captions") or {}
    orig = meta.get("language")
    pools = ((manual, False), (auto, True))

    if wanted:
        base = base_language(wanted)
        candidates: list[tuple[str, bool]] = []
        for pool, is_auto in pools:
            if wanted in pool:
                candidates.append((wanted, is_auto))
        published = bool(candidates)

        # Same-language siblings are always worth a try: they carry the same audio,
        # and YouTube throttles the bare '<lang>' key and '<lang>-orig' independently.
        for pool, is_auto in pools:
            for candidate in (f"{wanted}-orig", f"{base}-orig", base):
                if candidate in pool and (candidate, is_auto) not in candidates:
                    candidates.append((candidate, is_auto))

        if not candidates:
            die(
                f"Language {wanted!r} has no caption track. Available: "
                f"manual={sorted(manual) or 'none'} auto={sorted(auto) or 'none'}",
                3,
            )
        if not published:
            print(
                f"warning: track {wanted!r} is not published for this video; "
                f"using the same-language track {candidates[0][0]!r}",
                file=sys.stderr,
            )
        if orig and not same_language(wanted, orig):
            # Last resort for a throttled translation: the original audio language,
            # so the run still yields the video's content. meta.txt reports the
            # substitution honestly via caption_language / caption_note.
            for pool, is_auto in pools:
                for candidate in original_keys(orig):
                    if candidate in pool and (candidate, is_auto) not in candidates:
                        candidates.append((candidate, is_auto))
        return candidates

    ordered: list[str] = []
    if orig:
        ordered += original_keys(orig)
    ordered += ["en-orig", "en"]

    candidates = []
    for pool, is_auto in pools:
        for lang in ordered:
            if lang in pool and (lang, is_auto) not in candidates:
                candidates.append((lang, is_auto))

    # Last resort on the default path: the ASR source tracks, whatever language they
    # are in. On an auto-dubbed video these are the only real transcripts YouTube
    # publishes. Capped and appended last, so a language-matched track always wins.
    fallbacks = 0
    for pool, is_auto in pools:
        for lang in sorted(pool):
            if fallbacks >= MAX_ORIG_FALLBACKS:
                break
            if lang.endswith("-orig") and (lang, is_auto) not in candidates:
                candidates.append((lang, is_auto))
                fallbacks += 1

    if candidates:
        return candidates
    for pool, is_auto in pools:
        if pool:
            return [(next(iter(pool)), is_auto)]
    die("This video has no caption tracks at all (manual or automatic).", 3)


def download_captions(
    url: str,
    lang: str,
    dest_dir: Path,
    attempts: int = RETRY_ATTEMPTS,
    deadline: float | None = None,
) -> tuple[Path | None, str]:
    """Try to download one caption track.

    Returns (path or None, last error text). Returning rather than exiting lets the
    caller fall back to another track: YouTube 429s some tracks (the bare '<lang>'
    auto track and translated ones in particular) while serving a sibling fine in the
    same minute. `attempts` is lowered by the caller when further candidates exist, so
    a throttled track does not burn minutes of backoff before a working one is tried.

    Every retry wait is checked against `deadline` (defaulting to a fresh phase
    budget), so this cannot add up past it however many attempts are requested.
    """
    if deadline is None:
        deadline = time.monotonic() + PHASE_BUDGET_S
    dest_dir.mkdir(parents=True, exist_ok=True)
    template = str(dest_dir / "cap.%(ext)s")
    base = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--no-warnings",
        "--socket-timeout",
        "15",
        "--sleep-subtitles",
        "2",
        "--sub-langs",
        lang,
        "--sub-format",
        "json3/vtt/best",
        # Both flags: human-written and auto-generated tracks are requested the
        # same way, and requesting both avoids having to branch on which exists.
        "--write-subs",
        "--write-auto-subs",
        # Note: --convert-subs cannot target json3 (only srt/vtt/ass/lrc), so the
        # format is requested above and parsed as-is below.
        "-o",
        template,
        url,
    ]
    last = ""
    for attempt in range(1, attempts + 1):
        code, out, err = run_yt_dlp(base, timeout=min(SUBPROCESS_TIMEOUT_S, remaining_budget(deadline)))
        produced = sorted(dest_dir.glob("cap*"))
        if produced:
            return produced[0], ""
        last = (err or out).strip()
        for stale in dest_dir.glob("cap*"):
            stale.unlink()  # a partial/zero-byte track must not satisfy the next check
        if is_permanent_error(last):
            return None, last
        # Compare against `attempts`, NOT the module constant: the old code tested
        # `attempt < RETRY_ATTEMPTS`, so a track whose budget had already been
        # lowered to 3 attempts still slept backoff(3) = 32s *after* its final
        # failure — pure dead time on a track that was never going to answer.
        if attempt < attempts:
            if not sleep_within_budget(backoff_delay(attempt), deadline):
                last += "\nstopped retrying: caption budget exhausted"
                break
    return None, last


def strip_vtt_markup(line: str) -> str:
    line = re.sub(r"<[^>]+>", "", line)
    line = re.sub(r"&nbsp;?", " ", line)
    line = line.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    return line.strip()


def parse_json3(raw: str) -> list[tuple[int, str]]:
    data = json.loads(raw)
    segments: list[tuple[int, str]] = []
    for event in data.get("events", []):
        if "segs" not in event:
            continue  # window/style events carry no text
        text = "".join(seg.get("utf8", "") for seg in event["segs"])
        text = text.replace("\n", " ").strip()
        if text:
            segments.append((int(event.get("tStartMs", 0)), text))
    return segments


def parse_vtt(raw: str) -> list[tuple[int, str]]:
    segments: list[tuple[int, str]] = []
    stamp = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->")
    current_ms: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_ms is not None and buffer:
            text = " ".join(buffer).strip()
            if text:
                segments.append((current_ms, text))

    for line in raw.splitlines():
        match = stamp.match(line.strip())
        if match:
            flush()
            h, m, s, _ms = (int(g) for g in match.groups())
            current_ms = (h * 3600 + m * 60 + s) * 1000
            buffer = []
        elif line.strip() and not line.strip().isdigit() and not line.startswith(("WEBVTT", "Kind:", "Language:")):
            buffer.append(strip_vtt_markup(line))
    flush()
    return segments


def dedupe_rolling(segments: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Collapse rolling-caption artefacts.

    Auto-captions re-emit the previous line while appending the next few words.
    json3 tracks are usually already discrete, but vtt and some tracks are not, so
    drop exact repeats and let a superset replace its own prefix.
    """
    out: list[tuple[int, str]] = []
    for ms, text in segments:
        if out and out[-1][1] == text:
            continue
        if out and text.startswith(out[-1][1]) and len(text) > len(out[-1][1]):
            out[-1] = (out[-1][0], text)
            continue
        out.append((ms, text))
    return out


def timestamp(ms: int) -> str:
    """Render a caption start time as [m:ss], or [h:mm:ss] past the hour."""
    total = ms // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch a YouTube transcript as clean text.")
    ap.add_argument("url")
    ap.add_argument("--outdir", help="Where to write transcripts (default ./yt-transcript-<id>)")
    ap.add_argument("--lang", help="Caption language code to use, e.g. 'ru'. Default: original audio language.")
    ap.add_argument("--text-lang", help="Shorthand to request YouTube's auto-translated track in this language.")
    ap.add_argument("--meta-only", action="store_true", help="Print metadata only, no transcript.")
    ap.add_argument("--list-langs", action="store_true", help="List available caption tracks and exit.")
    ap.add_argument("--stdout", action="store_true", help="Print transcript instead of writing files.")
    ap.add_argument("--keep-raw", action="store_true", help="Keep the raw caption track in the output dir.")
    args = ap.parse_args()

    if not shutil.which("yt-dlp"):
        die("yt-dlp is not on PATH; install it or use the web_fetch fallback described in SKILL.md", 2)

    # One deadline for the whole run, shared by the metadata fetch and the caption
    # loop, so the two budgets cannot stack into a multi-minute worst case.
    deadline = time.monotonic() + PHASE_BUDGET_S

    meta = yt_dlp_json(args.url, deadline=deadline)
    vid = meta.get("id", "unknown")
    manual = sorted((meta.get("subtitles") or {}).keys())
    auto = sorted((meta.get("automatic_captions") or {}).keys())

    if args.list_langs:
        print(f"video id: {vid}")
        print(f"original audio language: {meta.get('language') or 'unknown'}")
        print(f"human-written tracks: {manual or 'none'}")
        print(f"auto-generated tracks: {len(auto)} languages"
              + (f" -> {auto}" if len(auto) <= 12 else f" -> {auto[:12]} ..."))
        return 0

    summary = {
        "id": vid,
        "title": meta.get("title"),
        "channel": meta.get("channel") or meta.get("uploader"),
        "duration": fmt_duration(meta.get("duration")),
        "upload_date": fmt_upload_date(meta.get("upload_date")),
        "view_count": meta.get("view_count"),
        "like_count": meta.get("like_count"),
        "original_language": meta.get("language"),
        "webpage_url": meta.get("webpage_url") or args.url,
    }
    if args.meta_only:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    orig = meta.get("language")
    candidates = track_candidates(meta, args.text_lang or args.lang)
    # Fewer per-track retries when there are fallbacks to try: spending minutes of
    # backoff on a throttled 'en' before reaching the 'en-orig' that answers at once
    # is worse than moving straight on to it.
    retry_attempts = RETRY_ATTEMPTS if len(candidates) == 1 else 3

    outdir = Path(args.outdir) if args.outdir else Path(f"yt-transcript-{vid}")
    # Download and parse inside ONE process run. In this harness /tmp is a private
    # namespace that does not survive between separate shell invocations, so a
    # two-step "download now, parse later" workflow silently loses the captions.
    errors: dict[str, str] = {}
    raw_path: Path | None = None
    lang, is_auto = candidates[0]

    with tempfile.TemporaryDirectory() as tmp:
        # Two passes over the candidate list, because switching tracks beats retrying
        # a dead one. Measured: one video's 'en' track returned 429 across five fresh
        # signatures over four minutes, while its 'ar-orig' answered in the same
        # second. Pass 1 therefore touches every candidate exactly once with no
        # backoff, so a working fallback is reached in seconds. Pass 2 adds backoff,
        # which only helps genuinely transient flakes; it is limited to the leading
        # candidates so a fully blocked video does not turn into a request storm.
        passes = ((1, candidates), (retry_attempts, candidates[:MAX_RETRY_PASS_CANDIDATES]))
        for per_track_attempts, pass_candidates in passes:
            for index, (cand_lang, cand_auto) in enumerate(pass_candidates):
                if time.monotonic() >= deadline:
                    break
                raw_path, err = download_captions(
                    args.url,
                    cand_lang,
                    Path(tmp),
                    attempts=per_track_attempts,
                    deadline=deadline,
                )
                if raw_path is not None:
                    lang, is_auto = cand_lang, cand_auto
                    if index and not same_language(cand_lang, candidates[0][0]):
                        # A different language than the one asked for: say so plainly.
                        # meta.txt records exactly what was substituted (see
                        # caption_provenance), which matters most on auto-dubbed videos
                        # where the only real ASR is of the dub.
                        print(
                            f"warning: could not fetch {candidates[0][0]!r}; falling back to "
                            f"the {cand_lang!r} track, which is NOT the video's original "
                            f"audio language (meta.txt records what it is)",
                            file=sys.stderr,
                        )
                    elif index:
                        print(
                            f"warning: {candidates[0][0]!r} was unusable; "
                            f"used the {cand_lang!r} track instead",
                            file=sys.stderr,
                        )
                    break
                if err:
                    errors[cand_lang] = err.splitlines()[-1]
            if raw_path is not None:
                break

        if raw_path is None:
            # Captions exist but none downloaded — almost always throttling. Keep the
            # metadata so the run still yields title/channel/date rather than nothing.
            tried = [f"{k}: {v}" for k, v in errors.items()]
            summary["caption_language"] = "none"
            summary["caption_error"] = "; ".join(tried)
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "meta.txt").write_text(
                "\n".join(f"{k}: {v}" for k, v in summary.items()) + "\n", encoding="utf-8"
            )
            die(
                "Captions exist but no candidate track downloaded "
                "(HTTP 429 is the usual cause). Check whether one video is throttled "
                "while another fetches fine, then retry once, or pass --lang with a "
                "track key from --list-langs.\nTried:\n  "
                + "\n  ".join(tried)
                + f"\nMetadata written to {outdir}/meta.txt",
                4,
            )

        raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
        segments = parse_json3(raw_text) if raw_path.suffix == ".json3" else parse_vtt(raw_text)
        if args.keep_raw:
            outdir.mkdir(parents=True, exist_ok=True)
            shutil.copy(raw_path, outdir / f"captions.{lang}{raw_path.suffix}")

    summary["caption_language"] = lang
    summary["caption_kind"] = "auto-generated" if is_auto else "human-written"
    # Say what the track actually is. Language codes alone are not enough: a
    # translation can carry the same code as the audio language (see
    # caption_provenance), which is how a machine translation once got reported as
    # the original wording.
    note = caption_provenance(meta, lang, is_auto, orig)
    if note:
        summary["caption_note"] = note

    segments = dedupe_rolling(segments)
    if not segments:
        die(f"The {lang!r} caption track downloaded but contained no readable text.", 3)

    lines = [f"[{timestamp(ms)}] {text}" for ms, text in segments]
    flat = " ".join(text for _ms, text in segments)

    if args.stdout:
        print("\n".join(lines))
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (outdir / "transcript.flat.txt").write_text(flat + "\n", encoding="utf-8")
    (outdir / "meta.txt").write_text(
        "\n".join(f"{k}: {v}" for k, v in summary.items()) + "\n", encoding="utf-8"
    )

    print(json.dumps(summary, ensure_ascii=False))
    print(f"caption blocks: {len(segments)} | words: {len(flat.split())} | chars: {len(flat)}")
    print(f"wrote: {outdir}/transcript.txt")
    print(f"       {outdir}/transcript.flat.txt")
    print(f"       {outdir}/meta.txt")
    print(f"preview: {lines[0][:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
