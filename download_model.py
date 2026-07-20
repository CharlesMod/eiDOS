#!/usr/bin/env python3
"""Fetch the GGUF models eiDOS needs, from public (non-gated) Hugging Face repos.

For a naive/fresh system: this pulls the house-mind and the semantic-recall embedder so a clone is
one command from a running brain. RESUMABLE (HTTP Range) — a 12.7 GB pull survives an interrupted
connection, unlike a from-scratch retry. Pure stdlib: no `huggingface_hub`, no `curl`/`wget`
dependency, works the same on Linux/macOS/Windows.

Models (both from PUBLIC, non-gated repos — no HF token or license click needed; Google's own
`google/` gemma repos ARE gated, which is why we point at the community re-uploads):

  mind   gemma-4-12b-it Q8_0 (~12.7 GB) — the house-mind. Source: `ggml-org` (the canonical
         llama.cpp org). Runs under the 32k / f16-KV+`-fa on` serving default (RUNTIME_SPRINTER §32k).
  embed  nomic-embed-text-v1.5 Q8_0 (~146 MB) — the embeddings the llama-server :8082 path serves
         (config `[knowledge] embedding_endpoint`). This is the PRIMARY embedder; `setup_embedding.py`
         (MiniLM ONNX) is the in-process FALLBACK tier below it (HTTP → ONNX → mock → BM25).

Usage:
    python3 download_model.py all                       # both, into ./models
    python3 download_model.py mind                      # just the mind
    python3 download_model.py embed
    python3 download_model.py all --models-dir /home/cmod/llm/models   # Sprinter's model dir
    python3 download_model.py mind --repo lmstudio-community/gemma-4-12B-it-GGUF   # a mirror
    python3 download_model.py --check                   # report what's present, download nothing

Then point the llama.cpp / llama-swap `-m` path at `<models-dir>/<dest filename>`.
"""

import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HF = "https://huggingface.co"

# The model registry. `filename` is the file's name IN the HF repo; `dest` is the local name we
# write (the mind keeps the lowercase-`b` name the existing llama-swap config references, though HF
# spells it with a capital B). `mirrors` are drop-in alternate repos (same file, same quant).
MODELS = {
    "mind": {
        "repo": "ggml-org/gemma-4-12B-it-GGUF",
        "filename": "gemma-4-12B-it-Q8_0.gguf",
        "dest": "gemma-4-12b-it-Q8_0.gguf",
        "min_bytes": 12_000_000_000,   # ~12.67 GB expected; floor guards a truncated pull
        "mirrors": ["lmstudio-community/gemma-4-12B-it-GGUF", "bartowski/gemma-4-12B-it-GGUF"],
        "desc": "house-mind — gemma-4-12b-it Q8_0 (~12.7 GB)",
    },
    "embed": {
        "repo": "nomic-ai/nomic-embed-text-v1.5-GGUF",
        "filename": "nomic-embed-text-v1.5.Q8_0.gguf",
        "dest": "nomic-embed-text-v1.5.Q8_0.gguf",
        "min_bytes": 130_000_000,      # ~146 MB expected
        "mirrors": [],
        "desc": "semantic-recall embeddings — nomic-embed-text-v1.5 Q8_0 (~146 MB)",
    },
}

CHUNK = 4 * 1024 * 1024  # 4 MiB
_UA = {"User-Agent": "eiDOS-download_model/1.0"}


def _resolve_url(repo: str, filename: str) -> str:
    return f"{HF}/{repo}/resolve/main/{filename}"


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def download(url: str, dest: Path, min_bytes: int = 0) -> None:
    """Resumable download to `dest`. Reuses a `<dest>.part` if present (HTTP Range), verifies the
    final size against `min_bytes`, then atomically renames into place. Idempotent: a complete
    `dest` is left untouched."""
    if dest.exists() and dest.stat().st_size >= min_bytes:
        print(f"  ✓ already present: {dest} ({_human(dest.stat().st_size)})")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0

    headers = dict(_UA)
    if have:
        headers["Range"] = f"bytes={have}-"   # preserved across HF's 302 → CDN (urllib keeps it)
        print(f"  ↻ resuming {dest.name} from {_human(have)}")
    else:
        print(f"  ↓ downloading {dest.name}")
    print(f"    {url}")

    resp = None
    try:
        try:
            resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:      # range past EOF → the .part is already complete
                resp = None
            else:
                raise

        if resp is not None:
            partial = getattr(resp, "status", resp.getcode()) == 206
            if have and not partial:         # server ignored our Range → start clean
                print("    (server ignored resume; restarting from 0)")
                have = 0
            cr = resp.headers.get("Content-Range", "")
            if cr and "/" in cr:
                total = int(cr.rsplit("/", 1)[1])
            else:
                clen = int(resp.headers.get("Content-Length", 0) or 0)
                total = (have + clen) if partial else clen
            mode = "ab" if (partial and have) else "wb"
            done = have
            with open(part, mode) as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        print(f"\r    {pct:3d}%  {_human(done)} / {_human(total)}", end="", flush=True)
            print()
    finally:
        if resp is not None:
            resp.close()

    size = part.stat().st_size
    if min_bytes and size < min_bytes:
        raise RuntimeError(f"{dest.name}: got {size:,} bytes, expected >= {min_bytes:,} "
                           f"(truncated download — rerun to resume from the .part)")
    os.replace(str(part), str(dest))
    print(f"  ✓ saved {dest} ({_human(size)})")


def fetch(key: str, models_dir: Path, repo_override: str = "") -> Path:
    spec = MODELS[key]
    dest = models_dir / spec["dest"]
    repos = [repo_override] if repo_override else [spec["repo"], *spec["mirrors"]]
    last = None
    for repo in repos:
        url = _resolve_url(repo, spec["filename"])
        try:
            download(url, dest, spec["min_bytes"])
            return dest
        except Exception as e:  # noqa: BLE001 — try the next mirror, report all failures
            last = e
            print(f"  ! {repo} failed: {e}")
            if repo != repos[-1]:
                print("  … trying a mirror")
    raise RuntimeError(f"all sources failed for '{key}': {last}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Download eiDOS's GGUF models from Hugging Face.")
    ap.add_argument("which", nargs="?", default="all", choices=[*MODELS, "all"],
                    help="which model(s) to fetch (default: all)")
    ap.add_argument("--models-dir", default="models",
                    help="directory to write into (default: ./models). Point llama.cpp -m here.")
    ap.add_argument("--repo", default="",
                    help="override the source repo (e.g. a specific mirror)")
    ap.add_argument("--check", action="store_true",
                    help="report what's already present; download nothing")
    args = ap.parse_args()

    models_dir = Path(args.models_dir)
    keys = list(MODELS) if args.which == "all" else [args.which]

    if args.check:
        print(f"models dir: {models_dir.resolve()}")
        for k in keys:
            dest = models_dir / MODELS[k]["dest"]
            if dest.exists() and dest.stat().st_size >= MODELS[k]["min_bytes"]:
                print(f"  ✓ {k:6} {MODELS[k]['dest']}  ({_human(dest.stat().st_size)})")
            else:
                part = dest.with_name(dest.name + ".part")
                partial = f" (partial: {_human(part.stat().st_size)})" if part.exists() else ""
                print(f"  ✗ {k:6} {MODELS[k]['dest']}  MISSING{partial} — {MODELS[k]['desc']}")
        return 0

    print(f"Fetching {', '.join(keys)} into {models_dir.resolve()}\n")
    for k in keys:
        print(f"[{k}] {MODELS[k]['desc']}")
        fetch(k, models_dir, args.repo)
        print()

    print("Done. Point the llama.cpp / llama-swap `-m` path at the file(s) above.")
    print("Mind serving default (16 GB card, 32k): -c 32768 --parallel 1 -fa on "
          "--cache-type-k f16 --cache-type-v f16   (omit -ngl; see RUNTIME_SPRINTER.md).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted — rerun to resume from the .part file")
        sys.exit(130)
