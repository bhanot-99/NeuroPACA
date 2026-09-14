#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>
#
# A6.3 · installs the `wake_word` extra (openwakeword) for real.
#
# `pip install -e ".[wake_word]"` alone FAILS: openwakeword==0.6.0 declares a
# hard dependency on tflite-runtime<3,>=2.8.0, which has no published wheel
# for Python 3.11+ (Google stopped publishing them — a known, widely-hit
# upstream packaging break, not something this project can fix in its own
# pyproject.toml). openwakeword's own code already supports running on
# onnxruntime alone (inference_framework="onnx", which
# sensing/wake_word.py's OpenWakeWordDetector already requests) — the
# tflite-runtime dependency just isn't actually needed for that path, but
# pip's resolver has no way to know that from the package metadata alone.
#
# Verified working (2026-09-14): install openwakeword with --no-deps, then
# its real onnx-path dependencies explicitly (the ones pip's resolver was
# about to install when the tflite-runtime failure aborted the whole thing).
#
# Usage: ./scripts/install_wake_word_extra.sh
set -euo pipefail

cd "$(dirname "$0")/.."

pip install --no-deps openwakeword==0.6.0
pip install "scipy>=1.3,<2" "scikit-learn>=1,<2"
# onnxruntime, tqdm, requests are already covered by this project's own
# `stt` extra (faster-whisper depends on all three) — installed separately
# here too in case `wake_word` is ever installed on its own.
pip install "onnxruntime>=1.10.0,<2" "tqdm>=4.0,<5.0" "requests>=2.0,<3"

echo
echo "openwakeword installed (--no-deps + real onnx-path dependencies)."
echo "Known gotcha: openwakeword.utils.download_models()'s incremental cache is"
echo "keyed on each model's .tflite file only — if a previous download was"
echo "interrupted after the .tflite completed but before its paired .onnx file,"
echo "later calls skip the pair entirely and Model(..., inference_framework="
echo "'onnx') will fail with NO_SUCHFILE. If you hit that: delete the affected"
echo ".tflite from <site-packages>/openwakeword/resources/models/ and re-run."
