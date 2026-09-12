"""
The local mlx-whisper engine.

Moved out of app.py unchanged in behaviour. Every call into MLX is serialised
through one lock: the push-to-talk worker and the meeting worker can both want
the GPU at once, and MLX's global model cache is not built for that.
"""

from __future__ import annotations

import sys
import threading

from whisperlocal.config import Settings


class Transcriber:
    """Manages the mlx-whisper model."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model_path = settings.model_path
        self._mlx_whisper = None
        self._loaded = False
        self._release_timer: threading.Timer | None = None
        # One MLX call at a time. Push-to-talk and meeting transcription share
        # the model, and mlx_whisper's global model cache is not thread-safe.
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        """Import mlx_whisper on first use — it is slow to load."""
        if self._loaded:
            return
        print("Loading mlx-whisper...")
        try:
            import mlx_whisper
        except ImportError:
            print("Error: mlx-whisper is not installed. Run: pip install mlx-whisper")
            sys.exit(1)
        self._mlx_whisper = mlx_whisper
        self._loaded = True
        self._apply_cache_limit()
        print(f"mlx-whisper loaded. Model: {self.model_path}")
        print("   (Weights download on first use — the first run takes longer.)")

    # ── memory ───────────────────────────────────────────────────────────────

    def _apply_cache_limit(self) -> None:
        """
        Cap MLX's buffer cache.

        MLX holds freed GPU buffers for reuse and, unbounded, that reaches about
        a gigabyte after a few dictations and stays there. The model itself is
        only ~140 MB of it. Capping the cache costs tens of milliseconds per
        dictation and roughly halves the memory this process reports.
        """
        limit = self.settings.mlx_cache_mb
        if limit < 0:
            return
        try:
            import mlx.core as mx

            mx.set_cache_limit(limit * 2**20)
        except Exception as exc:
            print(f"Warning: could not set the MLX cache limit: {exc}")

    def release_memory(self) -> None:
        """Drop the buffer cache entirely. Costs the next dictation a little."""
        try:
            import mlx.core as mx

            mx.clear_cache()
        except Exception:
            pass

    def _schedule_release(self) -> None:
        """
        Drop the cache once dictation has stopped for a while.

        Dictation is bursty: a flurry of sentences, then nothing for an hour.
        Keeping the cache during the burst keeps it fast, and releasing it
        afterwards keeps the idle app from parking hundreds of megabytes it is
        not using.
        """
        seconds = self.settings.idle_release_seconds
        if seconds <= 0:
            return

        if self._release_timer:
            self._release_timer.cancel()

        self._release_timer = threading.Timer(seconds, self.release_memory)
        self._release_timer.daemon = True
        self._release_timer.start()

    def memory_report(self) -> dict:
        """Current MLX memory use, in MB. Empty if MLX is not loaded."""
        try:
            import mlx.core as mx

            return {
                "active": mx.get_active_memory() // 2**20,
                "cache": mx.get_cache_memory() // 2**20,
                "peak": mx.get_peak_memory() // 2**20,
            }
        except Exception:
            return {}

    def apply_settings(self, settings: Settings) -> None:
        """Pick up new settings without a restart.

        A model change only swaps the path: mlx_whisper loads the new weights
        on the next call and drops the old ones from its single-slot cache. A
        prewarm thread makes that next call not the slow one.
        """
        previous = self.model_path
        self.settings = settings
        self.model_path = settings.model_path
        if not self._loaded:
            return
        self._apply_cache_limit()
        if self.model_path != previous:
            print(f"Model changed: {previous} → {self.model_path} (loading in the background)")
            threading.Thread(target=self.warm, daemon=True).start()

    def warm(self) -> None:
        """Load the model and run one inference on it, in this thread.

        Loading alone is not enough — it is actively harmful. MLX binds
        whatever the first real forward pass creates to the thread that ran
        it, and a model that was only *loaded* in one thread aborts the whole
        process with "There is no Stream(gpu, 1) in current thread" the moment
        another thread transcribes with it. A model that has been run once is
        fine from any later thread. So warming means a short silent clip
        through the full pipeline, never just get_model().
        """
        self._ensure_loaded()
        try:
            import numpy as np

            silence = np.zeros(16000, dtype="float32")  # one second at 16 kHz
            with self._lock:
                self._mlx_whisper.transcribe(
                    silence,
                    path_or_hf_repo=self.model_path,
                    language=self.settings.whisper_language,
                    fp16=self.settings.fp16,
                )
        except Exception as exc:
            print(f"Warning: could not pre-load {self.model_path}: {exc}")

    def transcribe(self, audio_path) -> str | None:
        """Transcribe an audio file. None if anything went wrong."""
        self._ensure_loaded()
        try:
            with self._lock:
                result = self._mlx_whisper.transcribe(
                    str(audio_path),
                    path_or_hf_repo=self.model_path,
                    language=self.settings.whisper_language,
                    fp16=self.settings.fp16,
                )
            return result.get("text", "").strip()
        except Exception as exc:
            print(f"Error: transcription failed: {exc}")
            return None
        finally:
            self._schedule_release()

    def transcribe_segments(self, audio_path, *, language: str | None = None) -> dict | None:
        """Full whisper result with segment and word timestamps, or None.

        For long-form (meeting) audio. No prompt and no conditioning on previous
        text, for the reasons documented on transcribe_words.
        """
        self._ensure_loaded()
        try:
            with self._lock:
                return self._mlx_whisper.transcribe(
                    str(audio_path),
                    path_or_hf_repo=self.model_path,
                    language=language if language is not None else self.settings.whisper_language,
                    fp16=self.settings.fp16,
                    word_timestamps=True,
                    condition_on_previous_text=False,
                )
        except Exception as exc:
            print(f"Error: transcription failed: {exc}")
            return None
        finally:
            self._schedule_release()

    def transcribe_words(self, audio, model_path: str | None = None):
        """Transcribe an audio array and return [(word, start, end), ...].

        Takes no prompt, deliberately. Passing previously transcribed text back
        in as `initial_prompt` creates a positive feedback loop: on near-silence
        the model simply continues the prompt, so one bad chunk primes the next
        and the session degenerates into a single token repeated forever
        ("ARP ARP ARP..."). Reproduced exactly — 2.5s of room tone with such a
        prompt yields 112 words of "ARP" in 1650ms; the same audio with no
        prompt yields nothing in 97ms.

        condition_on_previous_text is off for the same reason, guarding against
        a spiral within a single chunk.
        """
        self._ensure_loaded()
        try:
            with self._lock:
                result = self._mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=model_path or self.model_path,
                    language=self.settings.whisper_language,
                    fp16=self.settings.fp16,
                    word_timestamps=True,
                    condition_on_previous_text=False,
                )
        except Exception as exc:
            print(f"Error: transcription failed: {exc}")
            return None

        return [
            (w["word"], w["start"], w["end"])
            for segment in result.get("segments", [])
            for w in (segment.get("words") or [])
        ]

