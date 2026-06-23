{
  description = "Reproducible Nix runtime for eiDOS";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs, ... }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = lib.genAttrs systems;
      allowedUnfreePackages = [
        "claude-code"
      ];
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfreePredicate =
            pkg: builtins.elem (lib.getName pkg) allowedUnfreePackages;
        };
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python312;
          eidosPython = python.withPackages (
            ps: with ps; [
              bleak
              numpy
              pydantic
              pydantic-settings
              pytest
              pytest-timeout
              pyyaml
              pyzmq
              rank-bm25
            ]
          );
          dashboard = pkgs.writeShellApplication {
            name = "eidos-dashboard";
            runtimeInputs = [
              eidosPython
              pkgs.ffmpeg
            ];
            text = ''
              if [ ! -f dashboard.py ]; then
                echo "eidos-dashboard: run this from the eiDOS repository root" >&2
                exit 1
              fi

              export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
              export PYTHONUTF8=1
              exec python dashboard.py --config "''${EIDOS_CONFIG:-config.toml}" "$@"
            '';
          };
        in
        {
          default = dashboard;
          claudeCode = pkgs.claude-code;
          inherit dashboard eidosPython;
        }
      );

      apps = forAllSystems (
        system:
        let
          dashboard = "${nixpkgs.lib.getExe self.packages.${system}.dashboard}";
        in
        {
          default = {
            type = "app";
            program = dashboard;
            meta.description = "Run the eiDOS dashboard supervisor from the repository root";
          };
          dashboard = {
            type = "app";
            program = dashboard;
            meta.description = "Run the eiDOS dashboard supervisor from the repository root";
          };
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python312;
          claudeCode = self.packages.${system}.claudeCode;
          eidosPython = self.packages.${system}.eidosPython;
          eidosPythonWithEmbeddings = python.withPackages (
            ps: with ps; [
              bleak
              numpy
              onnxruntime
              pydantic
              pydantic-settings
              pytest
              pytest-timeout
              pyyaml
              pyzmq
              rank-bm25
              tokenizers
            ]
          );
          mkShell =
            shellPython: extraMessage:
            pkgs.mkShell {
              packages = [
                shellPython
                claudeCode
                pkgs.ffmpeg
                pkgs.git
                pkgs.uv
              ];

              env = {
                PYTHONUTF8 = "1";
                UV_PYTHON = "${shellPython}/bin/python";
                UV_PYTHON_DOWNLOADS = "never";
                UV_NO_MANAGED_PYTHON = "1";
              };

              shellHook = ''
                repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
                export PYTHONPATH="$repo_root''${PYTHONPATH:+:$PYTHONPATH}"
                echo "eiDOS Nix shell: Python $(python --version | cut -d' ' -f2), repo=$repo_root${extraMessage}"
              '';
            };
        in
        {
          default = mkShell eidosPython "";
          embeddings = mkShell eidosPythonWithEmbeddings " (embeddings enabled)";
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          claudeCode = self.packages.${system}.claudeCode;
          python = self.packages.${system}.eidosPython;
          src = lib.cleanSource ./.;
          testEnv = ''
            export HOME="$TMPDIR/home"
            export PYTHONPATH="$PWD"
            export PYTHONUTF8=1
            mkdir -p "$HOME"
          '';
        in
        {
          claude-smoke = pkgs.runCommand "eidos-claude-smoke" { nativeBuildInputs = [ claudeCode ]; } ''
            claude --version | grep -F "Claude Code"
            touch "$out"
          '';

          import-smoke = pkgs.runCommand "eidos-import-smoke" { nativeBuildInputs = [ python ]; } ''
            cp -R ${src} source
            chmod -R u+w source
            cd source
            ${testEnv}
            python - <<'PY'
import bleak
import numpy
import pydantic
import pydantic_settings
import rank_bm25
import yaml
import zmq

import config
import dashboard
import eidos
import embedding
import nervous
PY
            touch "$out"
          '';

          offline-tests = pkgs.runCommand "eidos-offline-tests" {
            nativeBuildInputs = [
              python
              pkgs.git
            ];
          } ''
            cp -R ${src} source
            chmod -R u+w source
            cd source
            ${testEnv}
            python -m pytest -q -m "not slow and not live"
            touch "$out"
          '';
        }
      );
    };
}
