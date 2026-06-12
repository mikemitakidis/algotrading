"""bot.ml.cli — argparse surface for the M18 ML pipeline.

M18.A.9: SAFE PARTIAL CLI WIRING WITH DOCUMENTED STUBS.

RECOVERED — RECONSTRUCTED_FROM_CHAT_HISTORY_NOT_BYTE_IDENTICAL.
The original M18.A.9 commit (cd4ce36) transcript was not captured;
this file implements the contract documented in the operator-provided
M18 Word history (M18.A.9 closeout), verified against the
byte-faithful A.8 registry/prediction APIs in this repo.

Wiring decisions (from the accepted closeout — do not widen):

  predict            WIRED   clean fit with predict_from_registry
  registry list      WIRED   clean fit with Registry.list_entries()
  registry show      WIRED   clean fit with Registry.get_entry()
  registry promote   WIRED   Q17 enforcement lives in the registry layer
  build-dataset      STUB    DatasetConfig/AssemblerResult persistence
                              surface missing — wiring would expand scope
  train              STUB    needs persisted AssemblerResult to load from
  evaluate           STUB    same persistence gap; latest eval report is
                              already accessible via `registry show`
  registry demote    STUB    parser surface has no flags, but
                              Registry.demote_current() needs a scope_key;
                              adding --scope-key/--model-id would be
                              inventing surface (forbidden)

CLI surface is UNCHANGED from M18.A.1 — no flags added or removed.

Exit-code semantics (locked):
  0   success
  1   known runtime error (model not found, input missing,
       promotion blocked, force-override invalid, ...). Human-readable
       message on stderr; `registry promote` blockages emit a JSON
       object on stderr carrying outcome/gate/gate_category/message.
  2   subcommand not yet implemented (stub) OR argparse error

Test injection point (NOT a CLI flag):
  main(argv, _registry_root=...) accepts an internal Python-only
  kwarg so tests can drive the CLI against temporary directories.
  It is deliberately NOT in the argparse parser.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional


# ── Stub messages (phase-tagged; exit 2) ────────────────────────────
# Each names the exact missing surface bit, per the accepted closeout.

STUB_BUILD_DATASET = (
    "build-dataset is not wired as of M18.A.10+: AssemblerResult has "
    "no persistence layer (and the DatasetConfig/AssemblerConfig "
    "surfaces differ). Use bot.ml.dataset.assembler.DatasetAssembler "
    "directly from a fixture script until that surface is approved."
)
STUB_TRAIN = (
    "train is not wired as of M18.A.10+: it needs a persisted "
    "AssemblerResult to load from, and no persistence layer exists "
    "yet. Use bot.ml.models.trainer directly from a fixture script."
)
STUB_EVALUATE = (
    "evaluate is not wired as of M18.A.10+: blocked on the same "
    "AssemblerResult persistence gap as train. The most recent "
    "evaluation report for a model is already accessible via "
    "`registry show`."
)
STUB_REGISTRY_DEMOTE = (
    "registry demote is not wired as of M18.A.10+: the M18.A.1 parser "
    "surface gave this subcommand no flags, but "
    "Registry.demote_current() requires a scope_key. Wiring it would "
    "require adding --scope-key or --model-id, which is inventing "
    "surface. Use Registry.demote_current() directly."
)


def _stub(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


# ── Parser (surface UNCHANGED from M18.A.1) ─────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bot.ml",
        description=(
            "M18 ML pipeline CLI — dataset assembly, training, "
            "evaluation, prediction, and registry administration. "
            "READ-ONLY/shadow-only in M18: nothing here can write "
            "signals.db or touch live trading."),
    )
    sub = p.add_subparsers(dest="command")

    bd = sub.add_parser(
        "build-dataset",
        help="Assemble a dataset from M16 bars + flywheel (stub).")
    bd.add_argument("--config",
        help="Path to a DatasetConfig JSON file.")

    tr = sub.add_parser(
        "train",
        help="Train a model from a TrainConfig (stub).")
    tr.add_argument("--config",
        help="Path to a TrainConfig JSON file.")

    ev = sub.add_parser(
        "evaluate",
        help="Build the EvaluationReport for a trained model (stub).")
    ev.add_argument("--model-id",
        help="Registry model_id to evaluate.")

    pr = sub.add_parser(
        "predict",
        help="Run read-only predictions from the registry.")
    pr.add_argument("--model-id", required=True,
        help="Registry model_id to predict with.")
    pr.add_argument("--input", required=True,
        help="Path to an input feature-matrix file (.parquet or .csv).")

    rg = sub.add_parser(
        "registry",
        help="Inspect or administer the file-based model registry.")
    rg_sub = rg.add_subparsers(dest="registry_command")

    rg_sub.add_parser(
        "list",
        help="List models in the registry.")

    rg_show = rg_sub.add_parser(
        "show",
        help="Show one registry entry in detail.")
    rg_show.add_argument("--model-id", required=True,
        help="The model_id to show.")

    rg_promote = rg_sub.add_parser(
        "promote",
        help="Promote a candidate to current (Q17 enforced).")
    rg_promote.add_argument("--model-id", required=True,
        help="The model_id to promote.")
    rg_promote.add_argument("--force", action="store_true",
        help="Override a JUDGMENT gate (never integrity — Q17).")
    rg_promote.add_argument("--override-gate", action="append",
        help="A judgment gate being overridden (required with "
              "--force). May be given multiple times.")
    rg_promote.add_argument("--reason",
        help="Operator justification (required with --force).")

    rg_sub.add_parser(
        "demote",
        help="Demote the current model (stub).")

    return p


# ── Wired handlers ───────────────────────────────────────────────────

def _make_registry(_registry_root: Optional[str]):
    from bot.ml.registry.registry import Registry
    if _registry_root is not None:
        return Registry(root=_registry_root)
    return Registry()


def _cmd_predict(args: argparse.Namespace,
                  _registry_root: Optional[str]) -> int:
    from bot.ml.errors import M18Error
    from bot.ml.registry.predictions import predict_from_registry
    try:
        import pandas as pd

        input_path = Path(args.input)
        if not input_path.exists():
            print(f"predict: input file does not exist: {input_path}",
                file=sys.stderr)
            return 1
        suffix = input_path.suffix.lower()
        if suffix == ".parquet":
            X_input = pd.read_parquet(input_path)
        elif suffix == ".csv":
            X_input = pd.read_csv(input_path)
        else:
            print(
                f"predict: unsupported input format {suffix!r} "
                f"(expected .parquet or .csv)", file=sys.stderr)
            return 1

        registry = _make_registry(_registry_root)
        result = predict_from_registry(
            registry=registry,
            model_id=args.model_id,
            X_input=X_input,
        )
        print(json.dumps({
            "command":               "predict",
            "model_id":              result.model_id,
            "n_input_rows":          result.n_input_rows,
            "n_features":            result.n_features,
            "output_path":           result.output_path,
            "batch_id":              result.batch_id,
            "predicted_at_utc":      result.predicted_at_utc,
            "extrapolation_summary": result.extrapolation_summary,
        }, sort_keys=True))
        return 0
    except M18Error as e:
        print(f"predict: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — CLI boundary
        print(f"predict: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _cmd_registry_list(_registry_root: Optional[str]) -> int:
    from bot.ml.errors import M18Error
    try:
        registry = _make_registry(_registry_root)
        entries = registry.list_entries()
        print(json.dumps(
            {"command": "registry-list",
             "n_entries": len(entries),
             "entries": [e.to_dict() for e in entries]},
            sort_keys=True, indent=2, default=str))
        return 0
    except M18Error as e:
        print(f"registry list: {type(e).__name__}: {e}",
            file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"registry list: {type(e).__name__}: {e}",
            file=sys.stderr)
        return 1


def _cmd_registry_show(args: argparse.Namespace,
                        _registry_root: Optional[str]) -> int:
    from bot.ml.errors import M18Error
    try:
        registry = _make_registry(_registry_root)
        entry = registry.get_entry(args.model_id)
        print(json.dumps({"command": "registry-show",
                          "entry": entry.to_dict()},
                          sort_keys=True, indent=2,
                          default=str))
        return 0
    except M18Error as e:
        print(f"registry show: {type(e).__name__}: {e}",
            file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"registry show: {type(e).__name__}: {e}",
            file=sys.stderr)
        return 1


def _cmd_registry_promote(args: argparse.Namespace,
                           _registry_root: Optional[str]) -> int:
    from bot.ml.errors import (
        ForceOverrideRequired,
        M18Error,
        PromotionBlockedError,
    )
    try:
        registry = _make_registry(_registry_root)
        override_gates = tuple(args.override_gate or ())
        entry = registry.promote_to_current(
            args.model_id,
            force=args.force,
            override_gates=override_gates,
            reason=args.reason,
        )
        print(json.dumps({
            "outcome":           "promoted",
            "model_id":          entry.model_id,
            "status":            entry.status,
            "approved_for_live": entry.approved_for_live,
        }, sort_keys=True))
        return 0
    except PromotionBlockedError as e:
        print(json.dumps({
            "outcome":       "blocked",
            "gate":          e.gate,
            "gate_category": e.gate_category,
            "message":       str(e),
        }, sort_keys=True), file=sys.stderr)
        return 1
    except ForceOverrideRequired as e:
        print(json.dumps({
            "outcome": "force_override_required",
            "message": str(e),
        }, sort_keys=True), file=sys.stderr)
        return 1
    except M18Error as e:
        print(f"registry promote: {type(e).__name__}: {e}",
            file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"registry promote: {type(e).__name__}: {e}",
            file=sys.stderr)
        return 1


# ── Entry point ──────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None,
          *,
          _registry_root: Optional[str] = None) -> int:
    """CLI entry point.

    `_registry_root` is an INTERNAL, test-only Python kwarg (note the
    leading underscore). It is intentionally NOT exposed as an
    argparse flag — production callers never see it. G9 tests use it
    to point the registry at a tempfile.TemporaryDirectory so the
    real on-disk data/ml/ tree is never touched.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse error (or --help). argparse uses exit code 2 for
        # usage errors and 0 for --help; pass it through.
        return int(e.code) if e.code is not None else 0

    cmd = args.command
    if cmd is None:
        parser.print_help(file=sys.stderr)
        return 2

    if cmd == "build-dataset":
        return _stub(STUB_BUILD_DATASET)
    if cmd == "train":
        return _stub(STUB_TRAIN)
    if cmd == "evaluate":
        return _stub(STUB_EVALUATE)
    if cmd == "predict":
        return _cmd_predict(args, _registry_root)
    if cmd == "registry":
        rcmd = getattr(args, "registry_command", None)
        if rcmd is None:
            print("registry: missing sub-command "
                  "(list|show|promote|demote)", file=sys.stderr)
            return 2
        if rcmd == "list":
            return _cmd_registry_list(_registry_root)
        if rcmd == "show":
            return _cmd_registry_show(args, _registry_root)
        if rcmd == "promote":
            return _cmd_registry_promote(args, _registry_root)
        if rcmd == "demote":
            return _stub(STUB_REGISTRY_DEMOTE)
        print(f"registry: unknown sub-command {rcmd!r}",
            file=sys.stderr)
        return 2

    print(f"unknown command: {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
