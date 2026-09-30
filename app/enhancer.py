import math
import threading
import time
from functools import lru_cache

import numpy as np
import soundfile as sf
import torch
import torchaudio

SAMPLES_PER_SEC = 16000  # the model's input sample rate
OUTPUT_RATE = 48000
CHUNK_SEC = 60           # block size for long recordings
OVERLAP_SEC = 2          # overlap, crossfaded at the joins
READ_MARGIN_SEC = 2.0    # read margin, absorbs the support of the sinc kernel


def plan_ranges(n, chunk, step):
    """Split [0, n) into successive (start, end) blocks overlapping by chunk-step.

    Blocks after the first and before the last overlap by `chunk - step` samples:
    that overlap is the crossfade material. The last block is truncated to n. Pure
    function (tested without a model).
    """
    ranges = []
    s = 0
    while s < n:
        e = min(s + chunk, n)
        ranges.append((s, e))
        if e >= n:
            break
        s += step
    return ranges


def _resample_stride(src, dst):
    """Output grid step of a torchaudio resampling.

    torchaudio runs a convolution with stride `orig_freq / gcd(orig, new)`: output
    sample j reads the input around j × that stride. Aligning the windows on a
    multiple of it is what makes the result identical to a global resampling.
    """
    return src // math.gcd(src, dst)


class Stream16k:
    """Provides the input signal as 16 kHz mono, window by window.

    Loading the whole file used to cost ~2.6 GB of RAM at the maximum duration
    (float32 48 kHz plus the 16 kHz resampled signal). Memory here is O(window):
    only the current window is read, converted and dropped.

    Inside each window the result is **identical** to a global resampling:
    torchaudio's sinc kernel is a finite-support convolution (19 samples for
    48 k -> 16 k, entirely absorbed by READ_MARGIN_SEC) and the window edges are
    aligned on its grid. Without that alignment a one-sample offset becomes
    audible at the block joins.
    """

    def __init__(self, path, input_sr=SAMPLES_PER_SEC):
        self._f = sf.SoundFile(str(path))
        self.src_sr = self._f.samplerate
        self.input_sr = input_sr
        self.n_src = self._f.frames
        # steps of the two successive conversions (sr -> input_sr -> 16 kHz)
        self._s1 = _resample_stride(self.src_sr, input_sr) if self.src_sr != input_sr else 1
        self._s2 = _resample_stride(input_sr, SAMPLES_PER_SEC) if input_sr != SAMPLES_PER_SEC else 1
        # read windows are aligned on a multiple common to both grids, otherwise
        # the second conversion ends up shifted by one sample (only visible for
        # input_sr != 16 kHz)
        self._align = math.lcm(self._s1, self._s2)
        self.n16 = self._to16(self.n_src)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._f.close()

    def _to16(self, n_src):
        """Position/length at 16 kHz of a source index — torchaudio's formulas
        (target_length = ceil(new_freq * length / orig_freq))."""
        n1 = n_src
        if self.src_sr != self.input_sr:
            n1 = -(-n_src * self.input_sr // self.src_sr)
        if self.input_sr != SAMPLES_PER_SEC:
            n1 = -(-n1 * SAMPLES_PER_SEC // self.input_sr)
        return n1

    def read(self, start, stop):
        """16 kHz samples [start, stop) — always exactly stop-start."""
        need = stop - start
        if need <= 0:
            return torch.zeros(0)
        # Source window widened by a margin on **both** sides, then aligned on the
        # grid: the left margin is essential, the first ~6 convolution samples of
        # a block read the input that precedes it (kernel support) and would be
        # wrong with a padding zero.
        margin = int(READ_MARGIN_SEC * self.src_sr)
        s_pad = max(0, (start * self.src_sr) // SAMPLES_PER_SEC - margin)
        s_pad -= s_pad % self._align
        e_pad = min(self.n_src, -(-stop * self.src_sr // SAMPLES_PER_SEC) + margin)

        self._f.seek(s_pad)
        block = self._f.read(e_pad - s_pad, dtype="float32", always_2d=True)
        t = torch.from_numpy(np.ascontiguousarray(block.mean(axis=1))).unsqueeze(0)
        del block
        if self.src_sr != self.input_sr:
            t = torchaudio.functional.resample(t, self.src_sr, self.input_sr)
        if self.input_sr != SAMPLES_PER_SEC:
            t = torchaudio.functional.resample(t, self.input_sr, SAMPLES_PER_SEC)

        # index, within the block, of the first requested sample: the window read
        # starts earlier, so the margin is consumed here
        lo = start - self._to16(s_pad)
        out = t[0, lo:lo + need]
        if out.numel() < need:  # window truncated by the end of the file
            out = torch.nn.functional.pad(out, (0, need - out.numel()))
        return out


@lru_cache(maxsize=8)
def _crossfade_cached(n):
    """Equal-power cosine windows (w_prev² + w_out² == 1).

    Cached: the length only depends on the overlap, which is constant. The
    returned tensors are shared, never modify them.
    """
    t = torch.linspace(0.0, 1.0, n)
    return torch.cos(t * math.pi / 2), torch.sin(t * math.pi / 2)


class LavaEnhancer:
    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._status = "idle"
        self._error = None
        self._device = None
        self._loaded_at = None

    def _load(self):
        import torch
        from LavaSR.model import LavaEnhance2

        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = device
        t0 = time.time()
        model = LavaEnhance2("YatharthS/LavaSR", device)
        self._loaded_at = time.time() - t0
        return model

    def _ensure_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._status = "loading"
                    try:
                        self._model = self._load()
                        self._status = "ready"
                        self._error = None
                    except Exception as e:
                        self._status = "error"
                        self._error = str(e)
                        raise
        return self._model

    def preload(self):
        try:
            self._ensure_model()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def enhance_wav(self, in_path, out_path, denoise=False, input_sr=16000,
                    cutoff=None, on_progress=None):
        """Enhance a mono WAV (any sample rate) to 48 kHz with LavaSR v2.

        Signals longer than CHUNK_SEC are processed block by block with an
        overlap and an equal-power crossfade, so no jump is audible at the
        joins. The input is read window by window: memory stays proportional to
        one block, not to the file length.
        """
        from LavaSR.enhancer.linkwitz_merge import FastLRMerge

        model = self._ensure_model()
        with self._lock, Stream16k(in_path, input_sr) as reader:
            if cutoff is None:
                cutoff = input_sr // 2
            model.bwe_model.lr_refiner = FastLRMerge(
                device=model.device, cutoff=cutoff, transition_bins=1024)

            chunk = CHUNK_SEC * SAMPLES_PER_SEC
            ov = OVERLAP_SEC * SAMPLES_PER_SEC
            step = chunk - ov
            ov48 = 3 * ov  # factor 3 between 16 kHz and 48 kHz

            ranges = plan_ranges(reader.n16, chunk, step)

            writer = sf.SoundFile(str(out_path), "w",
                                  samplerate=OUTPUT_RATE, channels=1, subtype="PCM_16")
            carry = None  # (tail: the last ov48 samples of the previous block)
            for i, (s, e) in enumerate(ranges):
                # unsqueeze: vocos expects [batch, time], not a 1-D vector
                out_i = self._enhance_chunk(model, reader.read(s, e).unsqueeze(0), denoise)
                expected = 3 * (e - s)
                if out_i.numel() < expected:
                    out_i = torch.cat([
                        out_i,
                        torch.zeros(expected - out_i.numel(), dtype=out_i.dtype),
                    ])
                elif out_i.numel() > expected:
                    out_i = out_i[:expected]

                is_last = i == len(ranges) - 1
                if carry is None:
                    if is_last:
                        to_write = out_i
                    else:
                        to_write = out_i[:expected - ov48]
                        carry = out_i[expected - ov48:]
                else:
                    w_prev, w_out = self._crossfade_weights(ov48)
                    blended = carry * w_prev + out_i[:ov48] * w_out
                    if is_last:
                        to_write = torch.cat([blended, out_i[ov48:]])
                    else:
                        to_write = torch.cat([blended, out_i[ov48:expected - ov48]])
                        carry = out_i[expected - ov48:]
                    carry = None if is_last else carry
                writer.write(self._to_int16(to_write))
                if on_progress:
                    on_progress((i + 1) / len(ranges))
            writer.close()
        return str(out_path)

    def _enhance_chunk(self, model, chunk_16k, denoise):
        c = chunk_16k.to(model.device)
        with torch.inference_mode():
            out = model.enhance(c, denoise=denoise, batch=False)
        # a single transfer to the CPU (the numpy round-trip cost two)
        return out.reshape(-1).float().cpu()

    @staticmethod
    def _crossfade_weights(n):
        return _crossfade_cached(n)

    @staticmethod
    def _to_int16(x):
        # Explicit copy: np.clip writes in place and the crossfade block is
        # reused by the next block — the caller's tensor must not be mutated.
        a = np.array(x.cpu().numpy(), dtype=np.float32, copy=True)
        np.clip(a, -1.0, 1.0, out=a)
        a *= 32767.0
        return a.astype(np.int16)

    def status(self):
        return {
            "status": self._status,
            "device": self._device,
            "error": self._error,
            "load_time": round(self._loaded_at, 1) if self._loaded_at else None,
        }


_enhancer = None
_enhancer_init = threading.Lock()


def get_enhancer() -> LavaEnhancer:
    global _enhancer
    if _enhancer is None:
        with _enhancer_init:
            if _enhancer is None:
                _enhancer = LavaEnhancer()
    return _enhancer
