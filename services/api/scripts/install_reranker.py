"""Install and verify the pinned local Cross-Encoder assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.retrieval.cross_encoder import (  # noqa: E402
    MODEL_FILE,
    MODEL_ID,
    MODEL_REPO,
    MODEL_SHA256,
    MODEL_SIZE,
    TOKENIZER_FILE,
    TOKENIZER_SHA256,
    TOKENIZER_SIZE,
    model_dir,
)


ASSETS = (
    (MODEL_FILE, "onnx/model_int8.onnx", MODEL_SIZE, MODEL_SHA256),
    (TOKENIZER_FILE, "tokenizer.json", TOKENIZER_SIZE, TOKENIZER_SHA256),
)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def install_asset(root: Path, name: str, remote: str, size: int, sha256: str) -> None:
    destination = root / name
    if destination.is_file() and destination.stat().st_size == size:
        if digest(destination) == sha256:
            print(f"verified {name}", flush=True)
            return
    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.unlink(missing_ok=True)
    url = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{remote}?download=true"
    print(f"downloading {name} ({size / 1024 / 1024:.1f} MiB)", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as output:
        copied = 0
        while block := response.read(8 * 1024 * 1024):
            output.write(block)
            copied += len(block)
            print(f"  {copied * 100 / size:5.1f}%", flush=True)
    if partial.stat().st_size != size or digest(partial) != sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"asset verification failed: {name}")
    os.replace(partial, destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, default=model_dir())
    args = parser.parse_args()
    root = args.dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for asset in ASSETS:
        install_asset(root, *asset)
    manifest = {
        "model_id": MODEL_ID,
        "repository": MODEL_REPO,
        "assets": [
            {"name": name, "size": size, "sha256": sha256}
            for name, _remote, size, sha256 in ASSETS
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"installed": True, "model_id": MODEL_ID, "path": str(root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

