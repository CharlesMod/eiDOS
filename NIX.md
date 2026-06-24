# Nix Runtime

eiDOS is still a flat Python application. The Nix flake wraps that shape instead
of turning the repo into an installable Python package.

## Commands

```bash
nix develop
nix run .#dashboard
nix flake check
```

The development shell provides Python 3.12 and the current Python dependencies
from Nixpkgs. It puts the repository root on `PYTHONPATH` so existing imports
continue to work without a `.venv`. It also provides Claude Code as `claude`,
pinned by `flake.lock` through Nixpkgs.

Typed boundary helpers are included in the default shell:

- `pydantic` for validating external data into typed Python models
- `pydantic-settings` for future config/env/TOML settings models

The default shell intentionally excludes the optional embedding stack. Use the
heavier shell when `knowledge.embedding_enabled = true` or when working on the
ONNX/vector path:

```bash
nix develop .#embeddings
```

`nix run .#dashboard` must be run from the repository root. It starts the same
`dashboard.py` supervisor path used by the existing installer:

```bash
nix run .#dashboard
```

Use `EIDOS_CONFIG` to point at a different config file:

```bash
EIDOS_CONFIG=config.local.toml nix run .#dashboard
```

## What The Flake Proves

`nix flake check` runs:

- `claude --version` to prove the pinned Claude Code CLI is available
- an import smoke for the core Python packages and eiDOS modules
- imports for `pydantic` and `pydantic_settings`
- `docs/boundary-schemas.json` matches the Pydantic boundary models generated
  by `scripts/check_boundary_schemas.py`
- the existing offline test selector:

```bash
python -m pytest -q -m "not slow and not live"
```

Regenerate the checked schema artifact after changing typed boundary models:

```bash
nix develop --command python scripts/check_boundary_schemas.py --write
```

## What The Flake Does Not Prove

The flake does not prove host-specific runtime capabilities:

- an OpenAI-compatible local LLM server
- Bluetooth/DBus access for Renogy BLE sensing
- a Chatterbox TTS service for the GLaDOS voice
- downloaded embedding model files under `models/`
- the optional ONNX/tokenizers embedding shell unless entered explicitly
- Claude Code login, subscription, network access, or model availability
- Windows PowerShell execution paths

Those remain runtime/operator concerns. The flake only makes the Python
interpreter, Python packages, ffmpeg executable, dev shell, dashboard wrapper,
and offline tests reproducible.

## Dependency Discipline

Do not run `pip install`, `uv sync`, curl installers, or other network-mutating
setup from `nix develop` hooks. Do not install Claude Code with npm from the
shell hook; add or update it through Nixpkgs. Add runtime dependencies through
Nix first. `uv` is present in the shell for dependency exploration or future
lock maintenance, but it is not the authority for the working runtime. The shell
points `uv` at the Nix Python and disables uv-managed Python downloads.
