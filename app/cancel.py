"""Cooperative cancellation and mutual exclusion of GPU work.

A cancelled job (the UI's "Cancel" button) sets a `threading.Event`. Long
operations check it at their checkpoints (between two inference blocks, between
two transcription chunks, on every ffmpeg progress line) and raise
`JobCancelled` to unwind cleanly.

`GPU_LOCK` serialises the phases that load a model onto the card. Per-model
locks (`LavaEnhancer._lock`, `ParakeetTranscriber._lock`) are not enough:
without a global lock one job can enhance audio while another transcribes, and
both models are then resident on the 8 GB card at once. The lock is taken
around the processing of a single *file* (enhancement then transcription); the
ffmpeg phases stay outside it. `main.py`'s single worker already serializes
jobs, so the lock is never contended in practice — it stays as a safety net for
a direct call to `process_file`.
"""

import threading

GPU_LOCK = threading.Lock()


class JobCancelled(Exception):
    """Raised to interrupt processing at the client's request."""


def raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise JobCancelled("Processing cancelled by the client.")
