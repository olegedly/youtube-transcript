#!/usr/bin/env python3
"""Regression tests for the caption-retry and track-provenance logic.

These are fast and offline: yt-dlp is never invoked and nothing sleeps. Each test
pins a bug that actually shipped and cost minutes of dead time or produced a
dishonest summary.

Run with:
    python3 -m unittest discover -s scripts -v
    python3 scripts/test_fetch_transcript.py
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_transcript as ft  # noqa: E402


def caption_meta(tracks: dict[str, list[str]]) -> dict:
    """Build minimal yt-dlp metadata: {track_key: [caption_url, ...]} -> meta."""
    return {
        "language": "en-US",
        "automatic_captions": {
            key: [{"ext": "json3", "url": url} for url in urls]
            for key, urls in tracks.items()
        },
    }


class RetryBudgetTests(unittest.TestCase):
    """The retry loop must not sleep after its last attempt, and must honour a budget."""

    def setUp(self) -> None:
        self.original_run = ft.run_yt_dlp
        self.original_sleep = ft.time.sleep
        self.calls = 0
        self.slept: list[float] = []

        def failing_run(cmd, timeout=ft.SUBPROCESS_TIMEOUT_S):
            self.calls += 1
            return -1, "", "ERROR: Unable to download video subtitles: HTTP Error 429"

        ft.run_yt_dlp = failing_run
        ft.time.sleep = lambda seconds: self.slept.append(seconds)

    def tearDown(self) -> None:
        ft.run_yt_dlp = self.original_run
        ft.time.sleep = self.original_sleep

    def _download(self, attempts: int, deadline: float | None = None):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            return ft.download_captions(
                "https://example.invalid",
                "en",
                Path(tmp),
                attempts=attempts,
                deadline=deadline,
            )

    def test_no_backoff_after_final_attempt(self):
        # The shipped bug compared `attempt < RETRY_ATTEMPTS` (5) instead of the
        # caller's `attempts` (3), so a 3-attempt track still slept backoff(3) = 32s
        # after its final failure — pure dead time on a track that never answered.
        self._download(attempts=3)
        self.assertEqual(self.calls, 3, "should make exactly `attempts` calls")
        self.assertEqual(len(self.slept), 2, "3 attempts need only 2 waits")

    def test_single_attempt_never_waits(self):
        self._download(attempts=1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.slept, [], "a single attempt must not sleep at all")

    def test_exhausted_budget_stops_retrying(self):
        # A dead thread of retries must not outlive the phase budget.
        self._download(attempts=5, deadline=time.monotonic() - 1.0)
        self.assertEqual(self.calls, 1, "budget already gone: no second attempt")
        self.assertEqual(self.slept, [])


class TimeoutTests(unittest.TestCase):
    """A stalled yt-dlp must surface as a failure, not hang the process."""

    def test_timeout_becomes_a_transient_failure(self):
        original = ft.subprocess.run

        def timing_out(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="yt-dlp", timeout=kwargs.get("timeout", 0))

        ft.subprocess.run = timing_out
        try:
            code, out, err = ft.run_yt_dlp(["yt-dlp"], timeout=1.0)
        finally:
            ft.subprocess.run = original

        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("exceeded", err)

    def test_run_yt_dlp_always_passes_a_timeout(self):
        seen: dict = {}
        original = ft.subprocess.run

        class Done:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def capture(cmd, **kwargs):
            seen.update(kwargs)
            return Done()

        ft.subprocess.run = capture
        try:
            ft.run_yt_dlp(["yt-dlp"])
        finally:
            ft.subprocess.run = original
        self.assertIn("timeout", seen, "subprocess.run must be called with a timeout")


class ProvenanceTests(unittest.TestCase):
    """caption_note must describe the track honestly, including tlang translations."""

    def test_translation_source_reads_tlang(self):
        meta = caption_meta({"en": ["https://x/api/timedtext?v=abc&lang=ar&tlang=en&fmt=json3"]})
        self.assertEqual(ft.translation_source(meta, "en", True), "ar")

    def test_same_language_tlang_is_not_a_translation(self):
        meta = caption_meta({"en": ["https://x/api/timedtext?v=abc&lang=en&tlang=en&fmt=json3"]})
        self.assertIsNone(ft.translation_source(meta, "en", True))

    def test_plain_original_track_has_no_note(self):
        meta = caption_meta({"en": ["https://x/api/timedtext?v=abc&lang=en&kind=asr&fmt=json3"]})
        self.assertIsNone(ft.caption_provenance(meta, "en", True, "en-US"))

    def test_translation_of_a_dub_is_not_called_original_wording(self):
        # The shipped bug: `en` against an audio language of `en-US` passed the
        # language-code check, so YouTube's Arabic-to-English round-trip was reported
        # as the original wording with no note at all.
        meta = caption_meta({"en": ["https://x/api/timedtext?v=abc&lang=ar&tlang=en&fmt=json3"]})
        note = ft.caption_provenance(meta, "en", True, "en-US")
        self.assertIsNotNone(note)
        self.assertIn("machine-translated from 'ar'", note)

    def test_dub_source_track_is_labelled_as_dubbed_audio(self):
        meta = caption_meta({"ar-orig": ["https://x/api/timedtext?v=abc&lang=ar&kind=asr&fmt=json3"]})
        note = ft.caption_provenance(meta, "ar-orig", True, "en-US")
        self.assertIsNotNone(note)
        self.assertIn("auto-dubbed", note)

    def test_explicit_other_language_is_flagged(self):
        meta = caption_meta({"de": ["https://x/api/timedtext?v=abc&lang=de&fmt=json3"]})
        note = ft.caption_provenance(meta, "de", True, "en-US")
        self.assertIsNotNone(note)


class CandidateTests(unittest.TestCase):
    """The default path must reach a source track when the audio language has none."""

    def test_orig_fallback_is_appended_last(self):
        meta = caption_meta(
            {
                "en": ["https://x/api/timedtext?v=abc&lang=ar&tlang=en&fmt=json3"],
                "ar-orig": ["https://x/api/timedtext?v=abc&lang=ar&kind=asr&fmt=json3"],
            }
        )
        candidates = ft.track_candidates(meta, None)
        self.assertEqual(candidates[0], ("en", True), "language-matched track still wins")
        self.assertIn(("ar-orig", True), candidates)

    def test_orig_fallback_is_capped(self):
        tracks = {"en": ["https://x/api/timedtext?v=abc&lang=en&fmt=json3"]}
        for index in range(ft.MAX_ORIG_FALLBACKS + 5):
            tracks[f"z{index:02d}-orig"] = ["https://x/api/timedtext?v=abc&lang=z&fmt=json3"]
        candidates = ft.track_candidates(caption_meta(tracks), None)
        appended = [lang for lang, _auto in candidates if lang.endswith("-orig")]
        self.assertLessEqual(len(appended), ft.MAX_ORIG_FALLBACKS)

    def test_explicit_lang_never_silently_returns_another_language(self):
        # Asking for a language YouTube does not publish must fail loudly (exit 3)
        # rather than quietly returning a different language's source track.
        meta = caption_meta({"ar-orig": ["https://x/api/timedtext?v=abc&lang=ar&fmt=json3"]})
        with self.assertRaises(SystemExit) as caught:
            ft.track_candidates(meta, "de")
        self.assertEqual(caught.exception.code, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
