import argparse
import sys
from pathlib import Path

from .processor import iter_media_files, process_file


def _cb(stage, prog, key=None, args=None):
    """Print a stage. The CLI translates nothing: it prints the wording
    `process_file` joins to the key (see app/messages.py)."""
    print(f"\r    {stage:<45s} {int(prog * 100):3d} %", end="", flush=True)


def _process_one(src: Path, outdir: Path, a, seen: dict):
    """Process one file; avoids name collisions inside the same folder."""
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
        print(f"No supported audio/video file found in: {root}")
        return 1
    outdir = Path(a.outdir).resolve() if a.outdir else root.parent / (root.name + "_pyclean-audio")
    print(f"pyclean-audio — folder: {root}")
    print(f"  {len(files)} file(s) found, recursive")
    print(f"  output: {outdir}\n")

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
            print(f"\n    FAILED: {e}")
            failed.append(str(rel))
    print(f"\nDone: {ok}/{len(files)} file(s) processed.")
    if failed:
        print("  Failed:")
        for rel in failed:
            print(f"   - {rel}")
        return 1
    return 0


def main():
    p = argparse.ArgumentParser(
        prog="pyclean-audio",
        description="Restore the audio of an audio or video file "
                    "(or of a whole folder, recursively) with pyclean-audio.",
    )
    p.add_argument("file", help="input audio/video file, or folder (recursive processing)")
    p.add_argument("-o", "--outdir", default=None,
help="output folder (default: <file>_pyclean-audio next to the input, "
                         "or <folder>_pyclean-audio next to the folder)")
    p.add_argument("--denoise", action="store_true",
                   help="run the denoiser (UL-UNAS) first")
    p.add_argument("--input-sr", type=int, default=16000, choices=[8000, 16000, 24000],
                   help="simulated input bandwidth (default: 16000)")
    p.add_argument("--cutoff", type=int, default=None,
                   help="cutoff in Hz of the refinement stage (default: auto)")
    p.add_argument("--format", dest="format", choices=["wav", "mp3"], default="wav",
                   help="audio output format (default: wav ; mp3: 192 kbit/s)")
    p.add_argument("--transcribe", action="store_true",
                   help="transcribe the cleaned audio with Parakeet TDT "
                        "(writes <name>_transcript.txt and <name>_pyclean-audio.srt, "
                        "off by default)")
    a = p.parse_args()

    src = Path(a.file).expanduser().resolve()
    if not src.exists():
        sys.exit(f"Not found: {src}")

    if src.is_dir():
        sys.exit(_run_folder(src, a))

    outdir = Path(a.outdir).resolve() if a.outdir else src.parent / (src.stem + "_pyclean-audio")

    print(f"pyclean-audio — processing: {src}")
    res = _process_one(src, outdir, a, {})
    print()
    print("  Result:")
    print(f"    original audio  : {res['original_wav']}")
    print(f"    enhanced audio  : {res['enhanced_wav']}")
    if res.get("enhanced_mp3"):
        print(f"    enhanced audio  : {res['enhanced_mp3']}")
    if res.get("transcript"):
        print(f"    transcript      : {res['transcript']}")
    if res.get("transcript_srt"):
        print(f"    subtitles       : {res['transcript_srt']}")
    if res["output"]:
        print(f"    final video     : {res['output']}")


if __name__ == "__main__":
    main()
