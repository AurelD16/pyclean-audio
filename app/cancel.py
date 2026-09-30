"""Annulation coopérative et exclusion mutuelle des traitements GPU.

Un job annulé (bouton « Annuler » de l'interface) pose un `threading.Event`.
Les longs traitements le vérifient à leurs points de contrôle (entre deux
blocs d'inférence, entre deux tranches de transcription, à chaque ligne de
progression ffmpeg) et lèvent `JobCancelled` pour unwinder proprement.

`GPU_LOCK` sérialise les phases qui chargent un modèle sur la carte. Les
verrous par modèle (`LavaEnhancer._lock`, `ParakeetTranscriber._lock`) ne
suffisent pas : sans verrou global, un job peut améliorer l'audio pendant qu'un
autre transcrit, et les deux modèles résident en même temps sur les 8 Go.
Le verrou est pris au niveau du traitement d'un *fichier* (amélioration puis
transcription) ; les phases ffmpeg restent hors verrou. Le worker unique de
`main.py` sérialise déjà les jobs, donc le verrou n'est en pratique jamais
disputé — il reste le filet de sécurité pour un appel direct à `process_file`.
"""

import threading

GPU_LOCK = threading.Lock()


class JobCancelled(Exception):
    """Levée pour interrompre un traitement à la demande du client."""


def raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise JobCancelled("Traitement annulé par le client.")
