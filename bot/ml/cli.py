"""bot.ml.cli — argparse surface for the M18 ML pipeline (M18.A.9 rewrite).

RECONSTRUCTED_FROM_TRANSCRIPT_NOT_BYTE_IDENTICAL.

The original M18.A.9 rewrite (commit cd4ce36) was in a session whose
transcript was not captured. This rewrite implements the design that
was explicitly approved in this chat session (compaction summary):

  - safe partial CLI wiring (predict + registry list/show/promote
    are LIVE; build-dataset, train, evaluate, registry demote are
    DOCUMENTED STUBS with explicit reason strings).
  - every stub exits with code 2 and prints the locked
    `stub: <reason>` message on stderr. Never silent success,
    never a Python traceback.

Invariants asserted by G1_CLI tests:
  - Every subcommand registered here is dispatchable.
  - Every stub message starts with the literal "stub: " prefix.
  - --force on `registry promote` requires --override-gate and --reason.
  - `registry promote` NEVER overrides an integrity gate, even with
    --force (Q17).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional


STUB_REASON_BUILD_DATASET = (
    "stub: build-dataset is documented but not wired in M18.A.9. "
    "Use bot.ml.dataset.assembler.AssemblerConfig + DatasetAssembler "
    "directly from a fixture script until the CLI lands."
)
STUB_REASON_TRAIN = (
    "stub: train is documented but not wired in M18.A.9. "
    "Use bot.ml.models.trainer.Trainer.train_one() directly from a "
    "fixture script until the CLI lands."
)
STUB_REASON_EVALUATE = (
    "stub: evaluate is documented but not wired in M18.A.9. "
    "Use bot.ml.evaluation.evaluator.evaluate() directly from a "
    "fixture script until the CLI lands."
)
STUB_REASON_REGISTRY_DEMOTE = (
    "stub: registry demote is documented but not wired in M18.A.9. "
    "Use bot.ml.registry.Registry.demote_current() directly from a "
    "fixture script until the CLI lands."
)


def _print_stub_and_exit(reason: str) -> int:
    print(reason, file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bot.ml",
        description=(
            "M18 ML pipeline CLI — dataset assembly, training, "
            "evaluation, prediction, and registry administration."),
    )
    sub = p.add_subparsers(dest="command", required=False)

    bd = sub.add_parser("build-dataset",
        help="Assemble a dataset from M16 bars + flywheel.")
    bd.add_argument("--config", required=False,
        help="Path to a DatasetConfig JSON file.")

    tr = sub.add_parser("train",
        help="Train a model from a TrainConfig.")
    tr.add_argument("--config", required=False,
        help="Path to a TrainConfig JSON file.")

    ev = sub.add_parser("evaluate",
        help="Build the EvaluationReport for a trained model.")
    ev.add_argument("--model-id", required=False,
        help="Registry model_id to evaluate.")

    pr = sub.add_parser("predict",
        help="Run read-only predictions from the registry.")
    pr.add_argument("--model-id", required=False,
        help="Registry model_id (default: current promotion).")
    pr.add_argument("--input", required=True,
        help="Path to an input row JSON file (single row mapping).")
    pr.add_argument("--registry-root", default="data/ml/registry",
        help="Filesystem root for the model registry.")

    rg = sub.add_parser("registry",
        help="Inspect or administer the file-based model registry.")
    rg.add_argument("--registry-root", default="data/ml/registry",
        help="Filesystem root for the model registry.")
    rg_sub = rg.add_subparsers(dest="registry_command", required=False)

    rg_list = rg_sub.add_parser("list",
        help="List models in the registry.")
    rg_list.add_argument("--status", required=False,
        help="Filter by registry status.")
    rg_list.add_argument("--anchor-set", required=False,
        help="Filter by anchor_set.")

    rg_show = rg_sub.add_parser("show",
        help="Show one registry entry in detail.")
    rg_show.add_argument("model_id", help="The model_id to show.")

    rg_promote = rg_sub.add_parser("promote",
        help="Promote a candidate to current.")
    rg_promote.add_argument("model_id", help="The model_id to promote.")
    rg_promote.add_argument("--force", action="store_true",
        help="Override a JUDGMENT gate (never integrity, Q17).")
    rg_promote.add_argument("--override-gate", required=False,
        help="Gate being overridden (required with --force).")
    rg_promote.add_argument("--reason", required=False,
        help="Operator justification (required with --force).")

    rg_demote = rg_sub.add_parser("demote",
        help="Demote the current model (stub in M18.A.9).")
    rg_demote.add_argument("model_id", help="The model_id to demote.")

    return p


def _cmd_predict(args: argparse.Namespace) -> int:
    try:
        from bot.ml.registry import Registry
        from bot.ml.registry.predictions import predict
    except ImportError as e:
        print(f"predict: import error: {e}", file=sys.stderr)
        return 1
    try:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"predict: input file not found: {input_path}",
                file=sys.stderr)
            return 1
        with open(input_path) as f:
            row = json.load(f)
        if not isinstance(row, dict):
            print(f"predict: --input must be a JSON object, got "
                  f"{type(row).__name__}", file=sys.stderr)
            return 1
        registry = Registry(Path(args.registry_root))
        model_id = args.model_id
        if model_id is None:
            current = registry.current()
            if current is None:
                print("predict: no --model-id supplied and no current "
                      "promotion exists", file=sys.stderr)
                return 1
            model_id = current.model_id
        prediction_row = predict(
            registry=registry, model_id=model_id, input_row=row)
        print(json.dumps(prediction_row, sort_keys=True))
        return 0
    except Exception as e:
        print(f"predict: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _cmd_registry_list(args: argparse.Namespace) -> int:
    try:
        from bot.ml.registry import Registry
    except ImportError as e:
        print(f"registry list: import error: {e}", file=sys.stderr)
        return 1
    try:
        registry = Registry(Path(args.registry_root))
        entries = registry.list_entries(
            status=args.status, anchor_set=args.anchor_set)
        out = []
        for e in entries:
            out.append({
                "model_id":   e.model_id,
                "status":     e.status,
                "anchor_set": getattr(e, "anchor_set", None),
                "train_mode": getattr(e, "train_mode", None),
                "model_type": getattr(e, "model_type", None),
                "created_at_utc": getattr(e, "created_at_utc", None),
            })
        print(json.dumps(out, sort_keys=True, indent=2))
        return 0
    except Exception as e:
        print(f"registry list: {type(e).__name__}: {e}",
            file=sys.stderr)
        return 1


def _cmd_registry_show(args: argparse.Namespace) -> int:
    try:
        from bot.ml.registry import Registry
    except ImportError as e:
        print(f"registry show: import error: {e}", file=sys.stderr)
        return 1
    try:
        registry = Registry(Path(args.registry_root))
        entry = registry.get(args.model_id)
        if entry is None:
            print(f"registry show: model_id not found: {args.model_id}",
                file=sys.stderr)
            return 1
        if hasattr(entry, "to_dict"):
            d = entry.to_dict()
        else:
            from dataclasses import asdict
            d = asdict(entry)
        print(json.dumps(d, sort_keys=True, indent=2, default=str))
        return 0
    except Exception as e:
        print(f"registry show: {type(e).__name__}: {e}",
            file=sys.stderr)
        return 1


def _cmd_registry_promote(args: argparse.Namespace) -> int:
    try:
        from bot.ml.registry import Registry
        from bot.ml.errors import (
            PromotionBlockedError, ForceOverrideRequired,
        )
    except ImportError as e:
        print(f"registry promote: import error: {e}",
            file=sys.stderr)
        return 1
    if args.force:
        if not args.override_gate:
            print("registry promote: --force requires --override-gate",
                file=sys.stderr)
            return 2
        if not args.reason:
            print("registry promote: --force requires --reason",
                file=sys.stderr)
            return 2
    try:
        registry = Registry(Path(args.registry_root))
        registry.promote_to_current(
            model_id=args.model_id,
            force=args.force,
            override_gate=args.override_gate,
            reason=args.reason,
        )
        print(json.dumps(
            {"promoted": True,
              "model_id": args.model_id,
              "force": args.force,
              "override_gate": args.override_gate,
              "reason": args.reason},
            sort_keys=True))
        return 0
    except PromotionBlockedError as e:
        print(f"registry promote: BLOCKED — "
              f"gate={getattr(e,'gate',None)!r} "
              f"category={getattr(e,'gate_category',None)!r}: {e}",
            file=sys.stderr)
        return 3
    except ForceOverrideRequired as e:
        print(f"registry promote: --force INVALID — {e}",
            file=sys.stderr)
        return 4
    except Exception as e:
        print(f"registry promote: {type(e).__name__}: {e}",
            file=sys.stderr)
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.command
    if cmd is None:
        parser.print_help(file=sys.stderr)
        return 2
    if cmd == "build-dataset":
        return _print_stub_and_exit(STUB_REASON_BUILD_DATASET)
    if cmd == "train":
        return _print_stub_and_exit(STUB_REASON_TRAIN)
    if cmd == "evaluate":
        return _print_stub_and_exit(STUB_REASON_EVALUATE)
    if cmd == "predict":
        return _cmd_predict(args)
    if cmd == "registry":
        sub = getattr(args, "registry_command", None)
        if sub is None:
            print("registry: missing sub-command "
                  "(list|show|promote|demote)", file=sys.stderr)
            return 2
        if sub == "demote":
            return _print_stub_and_exit(STUB_REASON_REGISTRY_DEMOTE)
        if sub == "list":
            return _cmd_registry_list(args)
        if sub == "show":
            return _cmd_registry_show(args)
        if sub == "promote":
            return _cmd_registry_promote(args)
        print(f"registry: unknown sub-command: {sub!r}", file=sys.stderr)
        return 2
    print(f"unknown command: {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
