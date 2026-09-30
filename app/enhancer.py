import math
import threading
import time
from functools import lru_cache

import numpy as np
import soundfile as sf
import torch
import torchaudio

SAMPLES_PER_SEC = 16000  # taux d'échantillonnage d'entrée du modèle
OUTPUT_RATE = 48000
CHUNK_SEC = 60           # taille des morceaux pour les enregistrements longs
OVERLAP_SEC = 2          # chevauchement pour le fondu croisé aux jonctions
READ_MARGIN_SEC = 2.0    # marge de lecture, absorbe le support du noyau sinc


def plan_ranges(n, chunk, step):
    """Découpe [0, n) en blocs (début, fin) successifs chevauchés de chunk-step.

    Les blocs au-delà du premier et avant le dernier se recouvrent de `chunk - step`
    échantillons : c'est la matière du fondu croisé. Le dernier bloc est tronqué
    à n. Fonction pure (testée sans modèle).
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
    """Pas de la grille de sortie d'un rééchantillonnage torchaudio.

    torchaudio applique une convolution à pas `orig_freq / gcd(orig, new)` :
    l'échantillon de sortie j lit l'entrée autour de j × ce pas. Caler les
    fenêtres sur un multiple de ce pas est ce qui rend le résultat identique au
    rééchantillonnage global.
    """
    return src // math.gcd(src, dst)


class Stream16k:
    """Fournit le signal d'entrée en 16 kHz mono, fenêtre par fenêtre.

    Charger le fichier entier coûtait ~2,6 Go de RAM pour la durée maximale
    (float32 48 kHz + le rééchantillonné 16 kHz). Ici la mémoire est O(fenêtre) :
    seule la fenêtre courante est lue, convertie puis jetée.

    Sur l'intérieur de chaque fenêtre le résultat est **identique** au
    rééchantillonnement global : le noyau sinc de torchaudio est une
    convolution de support fini (19 échantillons pour 48 k -> 16 k, entièrement
    absorbé par READ_MARGIN_SEC) et les bords de fenêtre sont calés sur sa
    grille. Sans ce calage, un décalage d'un échantillon se ferait audible aux
    jonctions de blocs.
    """

    def __init__(self, path, input_sr=SAMPLES_PER_SEC):
        self._f = sf.SoundFile(str(path))
        self.src_sr = self._f.samplerate
        self.input_sr = input_sr
        self.n_src = self._f.frames
        # pas des deux conversions successives (sr -> input_sr -> 16 kHz)
        self._s1 = _resample_stride(self.src_sr, input_sr) if self.src_sr != input_sr else 1
        self._s2 = _resample_stride(input_sr, SAMPLES_PER_SEC) if input_sr != SAMPLES_PER_SEC else 1
        # les fenêtres de lecture sont calées sur un multiple commun aux deux
        # grilles, sinon la seconde conversion se retrouve décalée d'un
        # échantillon (visible uniquement pour input_sr != 16 kHz)
        self._align = math.lcm(self._s1, self._s2)
        self.n16 = self._to16(self.n_src)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._f.close()

    def _to16(self, n_src):
        """Position/taille en 16 kHz d'un indice source — formules de torchaudio
        (target_length = ceil(new_freq * length / orig_freq))."""
        n1 = n_src
        if self.src_sr != self.input_sr:
            n1 = -(-n_src * self.input_sr // self.src_sr)
        if self.input_sr != SAMPLES_PER_SEC:
            n1 = -(-n1 * SAMPLES_PER_SEC // self.input_sr)
        return n1

    def read(self, start, stop):
        """Échantillons 16 kHz [start, stop) — toujours exactement stop-start."""
        need = stop - start
        if need <= 0:
            return torch.zeros(0)
        # Fenêtre source élargie d'une marge **des deux côtés** puis calée sur la
        # grille : la marge de gauche est indispensable, les ~6 premiers
        # échantillons de convolution d'un bloc lisent l'entrée qui le précède
        # (support du noyau) et seraient faux avec un zéro de remplissage.
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

        # indice, dans le bloc, du premier échantillon demandé : la fenêtre lue
        # commence en amont, la marge est donc consommée ici
        lo = start - self._to16(s_pad)
        out = t[0, lo:lo + need]
        if out.numel() < need:  # fenêtre tronquée par la fin du fichier
            out = torch.nn.functional.pad(out, (0, need - out.numel()))
        return out


@lru_cache(maxsize=8)
def _crossfade_cached(n):
    """Fenêtres cosinus d'égalité de puissance (w_prev² + w_out² == 1).

    Mise en cache : la taille ne dépend que du chevauchement, qui est constant.
    Les tenseurs retournés sont partagés, ne jamais les modifier.
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

        Les signaux plus longs que CHUNK_SEC sont traités par morceaux avec
        chevauchement et fondu croisé (equal-power) pour éviter tout saut
        audible aux jonctions. L'entrée est lue par fenêtres : la mémoire reste
        proportionnelle à un bloc, pas à la durée du fichier.
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
            ov48 = 3 * ov  # facteur 3 entre 16 kHz et 48 kHz

            ranges = plan_ranges(reader.n16, chunk, step)

            writer = sf.SoundFile(str(out_path), "w",
                                  samplerate=OUTPUT_RATE, channels=1, subtype="PCM_16")
            carry = None  # (tail: dernier ov48 échantillons du bloc précédent)
            for i, (s, e) in enumerate(ranges):
                # unsqueeze : le vocos attend [batch, temps], pas un vecteur
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
        # un seul transfert vers le CPU (le numpy aller-retour coûtait deux)
        return out.reshape(-1).float().cpu()

    @staticmethod
    def _crossfade_weights(n):
        return _crossfade_cached(n)

    @staticmethod
    def _to_int16(x):
        # Copie explicite : np.clip écrit en place et le bloc de fondu est
        # réutilisé au bloc suivant — on ne doit pas muter le tenseur appelant.
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
