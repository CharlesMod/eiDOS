#!/usr/bin/env python3
"""Download and set up the all-MiniLM-L6-v2 ONNX model for eiDOS embedding.

Usage:
    python3 setup_embedding.py [--model-dir models/all-MiniLM-L6-v2]

Downloads the ONNX model and tokenizer from Hugging Face, validates file
sizes, and writes a ready marker.
"""

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

# HuggingFace model files for all-MiniLM-L6-v2 ONNX
BASE_URL = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx"
FILES = {
    "model.onnx": {
        "url": f"{BASE_URL}/model.onnx",
        "min_bytes": 80_000_000,  # ~90MB expected
    },
}

# Tokenizer files (from main repo, not onnx subdir)
TOKENIZER_BASE = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main"
TOKENIZER_FILES = {
    "tokenizer.json": f"{TOKENIZER_BASE}/tokenizer.json",
    "tokenizer_config.json": f"{TOKENIZER_BASE}/tokenizer_config.json",
    "special_tokens_map.json": f"{TOKENIZER_BASE}/special_tokens_map.json",
    "vocab.txt": f"{TOKENIZER_BASE}/vocab.txt",
}


def download_file(url: str, dest: Path, min_bytes: int = 0) -> None:
    """Download a file with progress indication."""
    if dest.exists() and dest.stat().st_size >= min_bytes:
        print(f"  Already exists: {dest} ({dest.stat().st_size:,} bytes)")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    print(f"  Downloading {url}")
    print(f"  → {dest}")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "eiDOS-Setup/1.0"})
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        print(f"\r  Progress: {pct}% ({downloaded:,}/{total:,} bytes)", end="", flush=True)
            print()  # newline after progress

        if min_bytes and tmp.stat().st_size < min_bytes:
            tmp.unlink()
            raise RuntimeError(
                f"Downloaded file too small: {tmp.stat().st_size:,} bytes "
                f"(expected >= {min_bytes:,})"
            )

        os.rename(str(tmp), str(dest))
        print(f"  Saved: {dest.stat().st_size:,} bytes")
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def main():
    parser = argparse.ArgumentParser(description="Set up MiniLM ONNX model for eiDOS")
    parser.add_argument(
        "--model-dir",
        default="models/all-MiniLM-L6-v2",
        help="Directory to store model files (default: models/all-MiniLM-L6-v2)",
    )
    args = parser.parse_args()

    model_dir = Path(args.model_dir)
    print(f"Setting up embedding model in: {model_dir}")

    # Download ONNX model
    print("\n1. Downloading ONNX model...")
    for filename, info in FILES.items():
        download_file(info["url"], model_dir / filename, info["min_bytes"])

    # Download tokenizer files
    print("\n2. Downloading tokenizer files...")
    for filename, url in TOKENIZER_FILES.items():
        download_file(url, model_dir / filename)

    # Verify
    print("\n3. Verifying installation...")
    model_path = model_dir / "model.onnx"
    if not model_path.exists():
        print("ERROR: model.onnx not found after download!")
        sys.exit(1)

    print(f"  model.onnx: {model_path.stat().st_size:,} bytes")

    # Write a ready marker
    (model_dir / ".ready").write_text("ok\n")
    print(f"\nSetup complete! Model ready at {model_dir}/")
    print("Enable in config.toml:")
    print("  [knowledge]")
    print("  embedding_enabled = true")


if __name__ == "__main__":
    main()
