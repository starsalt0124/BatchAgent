from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .manifest import ManifestError, create_sample_manifest, load_manifest
from .provider import create_provider
from .scheduler import recover_running, run_manifest, status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="batchagent", description="Markdown-driven batch agent harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a sample manifest.")
    init_parser.add_argument("manifest", nargs="?", default="BATCHAGENT.md")

    doctor_parser = subparsers.add_parser("doctor", help="Validate a manifest without running tasks.")
    doctor_parser.add_argument("manifest")

    status_parser = subparsers.add_parser("status", help="Print task status counts.")
    status_parser.add_argument("manifest")

    run_parser = subparsers.add_parser("run", help="Run eligible tasks from a manifest.")
    run_parser.add_argument("manifest")
    run_parser.add_argument("--limit", type=int, default=None, help="Maximum tasks to run.")
    run_parser.add_argument("--retry-failed", action="store_true", help="Treat failed tasks as eligible.")

    recover_parser = subparsers.add_parser("recover", help="Move stale running tasks to retry or failed.")
    recover_parser.add_argument("manifest")
    recover_parser.add_argument("--to", choices=["retry", "failed", "todo"], default="retry")

    models_parser = subparsers.add_parser("models", help="List models through the configured provider.")
    models_parser.add_argument("manifest", nargs="?", default=None)
    models_parser.add_argument("--base-url", default=None)
    models_parser.add_argument("--api-key-env", default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            create_sample_manifest(args.manifest)
            print(f"created {args.manifest}")
            return 0
        if args.command == "doctor":
            manifest = load_manifest(args.manifest)
            _print_manifest_summary(manifest)
            return 0
        if args.command == "status":
            print(json.dumps(status(args.manifest), ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            results = asyncio.run(run_manifest(args.manifest, limit=args.limit, retry_failed=args.retry_failed))
            for result in results:
                state = "done" if result.success else "failed"
                print(f"{state}\t{result.task_id}\t{result.run_dir}")
                if result.error:
                    print(f"  error: {result.error}")
            return 0 if all(result.success for result in results) else 1
        if args.command == "recover":
            changed = recover_running(args.manifest, args.to)
            print(f"recovered {changed} running task(s) to {args.to}")
            return 0
        if args.command == "models":
            return _models(args)
    except (ManifestError, RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _print_manifest_summary(manifest) -> None:
    counts: dict[str, int] = {}
    for task in manifest.tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    print(f"name: {manifest.config.name}")
    print(f"provider: {manifest.config.provider}")
    print(f"model: {manifest.config.model}")
    print(f"workspace: {Path(manifest.config.workspace)} ({manifest.config.workspace_mode})")
    print(f"parallel: {manifest.config.parallel}, max_concurrency: {manifest.config.effective_concurrency}")
    print(f"tasks: {len(manifest.tasks)}")
    print(json.dumps(counts, ensure_ascii=False, indent=2))


def _models(args) -> int:
    if args.manifest:
        config = load_manifest(args.manifest).config
    else:
        from .models import BatchConfig

        config = BatchConfig()
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key_env:
        config.api_key_env = args.api_key_env
    provider = create_provider(config)
    print(json.dumps(provider.list_models(), ensure_ascii=False, indent=2))
    return 0

