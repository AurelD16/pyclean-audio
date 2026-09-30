import argparse
import sys
from pathlib import Path

from .processor import iter_media_files, process_file


def _cb(stage, prog):
    print(f"\r    {stage:<45s} {int(prog * 100):3d} %", end="", flush=True)


def _process_one(src: Path, outdir: Path, a, seen: dict):
    """Traite un fichier ; évite les collisions de noms dans le même dossier."""
    key = (str(outdir), src.stem.lower())
    n = seen.get(key, 0) + 1
    seen[key] = n
    if n > 1:
        outdir = outdir / f"{src.stem}_{n}"
    return process_file(src, outdir, a.denoise, a.input_sr, a.cutoff, _cb,
                        output_format=a.format, transcribe=a.transcribe)


def _run_folder(root: Path, a) -> int:
    files = iter_media_files(root)
    if not files:
        print(f"Aucun fichier audio/vidéo pris en charge trouvé dans : {root}")
        return 1
    outdir = Path(a.outdir).resolve() if a.outdir else root.parent / (root.name + "_pyclean-audio")
    print(f"pyclean-audio — dossier : {root}")
    print(f"  {len(files)} fichier(s) trouvé(s), récursif")
    print(f"  sortie : {outdir}\n")

    seen = {}
    ok, failed = 0, []
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root)
        print(f"[{i}/{len(files)}] {rel}")
        try:
            res = _process_one(f, outdir / rel.parent, a, seen)
            ok += 1
            print()
            if a.format == "mp3" and res.get("enhanced_mp3"):
                print(f"    → {res['enhanced_mp3']}")
            else:
                print(f"    → {res['enhanced_wav']}")
            if res["output"]:
                print(f"      {res['output']}")
            if res.get("transcript"):
                print(f"      {res['transcript']}")
            if res.get("transcript_srt"):
                print(f"      {res['transcript_srt']}")
        except Exception as e:
            print(f"\n    ÉCHEC : {e}")
            failed.append(str(rel))
    print(f"\nTerminé : {ok}/{len(files)} fichier(s) traité(s).")
    if failed:
        print("  En échec :")
        for rel in failed:
            print(f"   - {rel}")
        return 1
    return 0


def main():
    p = argparse.ArgumentParser(
        prog="pyclean-audio",
        description="Améliorer l'audio d'un fichier audio ou vidéo "
                    "(ou d'un dossier entier, de façon récursive) avec pyclean-audio.",
    )
    p.add_argument("file", help="fichier audio/vidéo d'entrée, ou dossier (traitement récursif)")
    p.add_argument("-o", "--outdir", default=None,
help="dossier de sortie (défaut : <fichier>_pyclean-audio à côté du fichier, "
                         "ou <dossier>_pyclean-audio à côté du dossier)")
    p.add_argument("--denoise", action="store_true",
                   help="activer la réduction de bruit (UL-UNAS)")
    p.add_argument("--input-sr", type=int, default=16000, choices=[8000, 16000, 24000],
                   help="résolution d'entrée simulée (défaut : 16000)")
    p.add_argument("--cutoff", type=int, default=None,
                   help="cutoff en Hz de l'étage de raffinement (défaut : auto)")
    p.add_argument("--format", dest="format", choices=["wav", "mp3"], default="wav",
                   help="format de sortie audio (défaut : wav ; mp3 : 192 kbit/s)")
    p.add_argument("--transcribe", action="store_true",
                   help="transcrire l'audio nettoyé avec Parakeet TDT "
                        "(fichiers <nom>_transcript.txt et <nom>_pyclean-audio.srt, "
                        "désactivé par défaut)")
    a = p.parse_args()

    src = Path(a.file).expanduser().resolve()
    if not src.exists():
        sys.exit(f"Introuvable : {src}")

    if src.is_dir():
        sys.exit(_run_folder(src, a))

    outdir = Path(a.outdir).resolve() if a.outdir else src.parent / (src.stem + "_pyclean-audio")

    print(f"pyclean-audio — traitement de : {src}")
    res = _process_one(src, outdir, a, {})
    print()
    print("  Résultat :")
    print(f"    audio d'origine  : {res['original_wav']}")
    print(f"    audio amélioré   : {res['enhanced_wav']}")
    if res.get("enhanced_mp3"):
        print(f"    audio amélioré   : {res['enhanced_mp3']}")
    if res.get("transcript"):
        print(f"    transcription    : {res['transcript']}")
    if res.get("transcript_srt"):
        print(f"    sous-titres      : {res['transcript_srt']}")
    if res["output"]:
        print(f"    vidéo finale     : {res['output']}")


if __name__ == "__main__":
    main()
