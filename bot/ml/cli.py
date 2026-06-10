"""bot.ml.cli — argparse surface for the M18 ML pipeline.

RECONSTRUCTED_FROM_TRANSCRIPT_NOT_BYTE_IDENTICAL.

This is the M18.A.1 initial stub form — it registers the subcommand
surface but every command is a documented stub (Q-checklist: safe
partial CLI). M18.A.9 will wire the safely-implementable commands
(`predict`, `registry list`, `registry show`, `registry promote`)
and leave the rest (`build-dataset`, `train`, `evaluate`,
`registry demote`) as STUBs with explicit reason strings.

Invariant (asserted by G1_CLI tests): every subcommand here exits with
exit code 2 and a clear "stub: <reason>" message when not yet wired,
NEVER with a silent success or a Python traceback.

The CLI is invoked via `python -m bot.ml <subcommand> ...`.
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional


# ─── Stub reason strings ─────────────────────────────────────────────
# These are referenced verbatim by G1_CLI tests — keep them stable.

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
    """Emit the documented stub message on stderr, exit with code 2."""
    print(reason, file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse surface.

    The subcommand set is locked at M18.A.1 and asserted by G1_CLI:
      build-dataset / train / evaluate / predict / registry {list,
      show, promote, demote}
    """
    p = argparse.ArgumentParser(
        prog="bot.ml",
        description=(
            "M18 ML pipeline CLI — dataset assembly, training, "
            "evaluation, prediction, and registry administration."),
    )
    sub = p.add_subparsers(dest="command", required=False)

    # ─ build-dataset ────────────────────────────────────────────────
    bd = sub.add_parser("build-dataset",
        help="Assemble a dataset from M16 bars + flywheel.")
    bd.add_argument("--config", required=False,
        help="Path to a DatasetConfig JSON file.")

    # ─ train ────────────────────────────────────────────────────────
    tr = sub.add_parser("train",
        help="Train a model from a TrainConfig.")
    tr.add_argument("--config", required=False,
        help="Path to a TrainConfig JSON file.")

    # ─ evaluate ─────────────────────────────────────────────────────
    ev = sub.add_parser("evaluate",
        help="Build the EvaluationReport for a trained model.")
    ev.add_argument("--model-id", required=False,
        help="Registry model_id to evaluate.")

    # ─ predict ──────────────────────────────────────────────────────
    pr = sub.add_parser("predict",
        help="Run read-only predictions from the registry.")
    pr.add_argument("--model-id", required=False,
        help="Registry model_id (use the current promotion when "
             "omitted, if any exists).")
    pr.add_argument("--input", required=False,
        help="Path to an input row CSV/JSON.")

    # ─ registry {list, show, promote, demote} ────────────────────────
    rg = sub.add_parser("registry",
        help="Inspect or administer the file-based model registry.")
    rg_sub = rg.add_subparsers(dest="registry_command", required=False)

    rg_list = rg_sub.add_parser("list",
        help="List models in the registry (status, anchor_set, cohort).")
    rg_list.add_argument("--status", required=False,
        help="Filter by registry status.")

    rg_show = rg_sub.add_parser("show",
        help="Show one registry entry in detail.")
    rg_show.add_argument("model_id",
        help="The model_id to show.")

    rg_promote = rg_sub.add_parser("promote",
        help="Promote a candidate to current.")
    rg_promote.add_argument("model_id",
        help="The model_id to promote.")
    rg_promote.add_argument("--force", action="store_true",
        help="Force promotion past a JUDGMENT gate (never integrity).")
    rg_promote.add_argument("--override-gate", required=False,
        help="The specific gate being overridden (required with --force).")
    rg_promote.add_argument("--reason", required=False,
        help="Operator-supplied justification for the override.")

    rg_demote = rg_sub.add_parser("demote",
        help="Demote the current model (stub in M18.A.9).")
    rg_demote.add_argument("model_id",
        help="The model_id to demote.")

    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Entrypoint for `python -m bot.ml`."""
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
        # Wired in M18.A.9 — for the M18.A.1 stub form, emit a
        # not-yet-wired notice.
        return _print_stub_and_exit(
            "stub: predict will be wired in M18.A.9.")
    if cmd == "registry":
        sub = getattr(args, "registry_command", None)
        if sub is None:
            print("registry: missing sub-command (list|show|promote|demote)",
                file=sys.stderr)
            return 2
        if sub == "demote":
            return _print_stub_and_exit(STUB_REASON_REGISTRY_DEMOTE)
        # list / show / promote — wired in M18.A.9
        return _print_stub_and_exit(
            f"stub: registry {sub} will be wired in M18.A.9.")

    # Unknown command (argparse should already have rejected it)
    print(f"unknown command: {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
