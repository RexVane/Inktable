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

from app.db.database import APP_DIR  # noqa: E402
from app.retrieval.cross_encoder import (  # noqa: E402
    MODELS,
    active_spec,
)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def install_asset(root: Path, name: str, remote: str, size: int, sha256: str,
                  *, repo: str, endpoint: str) -> None:
    destination = root / name
    if destination.is_file() and destination.stat().st_size == size:
        if digest(destination) == sha256:
            print(f"verified {name}", flush=True)
            return
    partial = destination.with_suffix(destination.suffix + ".partial")
    partial.unlink(missing_ok=True)
    url = f"{endpoint.rstrip('/')}/{repo}/resolve/main/{remote}?download=true"
    print(f"downloading {name} ({size / 1024 / 1024:.1f} MiB) from {endpoint}",
          flush=True)
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
    parser.add_argument("--dir", type=Path)
    parser.add_argument(
        "--model", default="", choices=["", *sorted(MODELS)],
        help="要安装哪个模型；留空 = 当前生效的那个（见 active_spec）",
    )
    parser.add_argument(
        "--endpoint", default=os.environ.get("HF_ENDPOINT", "").strip()
        or "https://huggingface.co",
        help="镜像地址。huggingface.co 不通时用 https://hf-mirror.com",
    )
    args = parser.parse_args()
    spec = MODELS[args.model] if args.model else active_spec()
    root = (args.dir or (Path(APP_DIR) / "models" / spec.model_id)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    assets = (
        (spec.model_file, spec.model_remote, spec.model_size, spec.model_sha256),
        (spec.tokenizer_file, spec.tokenizer_remote, spec.tokenizer_size,
         spec.tokenizer_sha256),
    )
    for asset in assets:
        install_asset(root, *asset, repo=spec.repo, endpoint=args.endpoint)
    manifest = {
        "model_id": spec.model_id,
        "repository": spec.repo,
        "endpoint": args.endpoint,
        "assets": [
            {"name": name, "size": size, "sha256": sha256}
            for name, _remote, size, sha256 in assets
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"installed": True, "model_id": spec.model_id,
                      "path": str(root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

