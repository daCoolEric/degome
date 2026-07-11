#!/usr/bin/env python3
"""
Pre-download Whisper models while you have internet, so Degome can
transcribe fully offline afterwards.

Usage:
    python predownload.py                # downloads small (the default model)
    python predownload.py small medium   # download several
    python predownload.py all            # everything (several GB)
"""

import sys

SIZES = ["tiny", "base", "small", "medium", "large-v3"]


def main() -> None:
    args = sys.argv[1:] or ["small"]
    wanted = SIZES if args == ["all"] else args
    bad = [w for w in wanted if w not in SIZES]
    if bad:
        sys.exit(f"Unknown model(s): {', '.join(bad)}. Choose from: {', '.join(SIZES)} or 'all'.")

    from faster_whisper import WhisperModel

    for size in wanted:
        print(f"-> downloading/verifying '{size}' ...")
        WhisperModel(size, compute_type="int8")
        print(f"   '{size}' is ready for offline use.")

    print("\nDone. Transcription now works with no internet connection.")


if __name__ == "__main__":
    main()
