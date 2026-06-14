"""bot.ml.cli — argparse surface for the M18 ML pipeline.

M18.A.9: SAFE PARTIAL CLI WIRING WITH DOCUMENTED STUBS.
M18.B.9.A: the A.9 lock is SUPERSEDED ONLY for the approved B9.A
changes below — everything else in the A.9 contract is unchanged.

  B9.A approved widenings (do not exceed):
    * global --json flag → strict envelope
      {ok, command, status, result, warnings, errors}
    * global --debug flag → allow a traceback on unexpected errors;
      otherwise NO raw traceback ever reaches the user
    * registry demote WIRED via Registry.demote_current(scope_key, ...)
      behind a new --scope-key argument (was a stub)
    * --dry-run for registry promote / registry demote (no mutation)
    * predict / registry list / show / promote hardened: same DEFAULT
      output as A.9 (legacy-compatible, tests still lock it) PLUS the
      --json envelope.
  Still STUBBED in B9.A (deferred to B9.B): build-dataset, train,
  evaluate — they remain exit 2 with their phase-tag messages.

RECOVERED — RECONSTRUCTED_FROM_CHAT_HISTORY_NOT_BYTE_IDENTICAL.
The original M18.A.9 commit (cd4ce36) transcript was not captured;
this file implements the contract documented in the operator-provided
M18 Word history (M18.A.9 closeout), verified against the
byte-faithful A.8 registry/prediction APIs in this repo.

Wiring decisions:

  predict            WIRED   clean fit with predict_from_registry
  registry list      WIRED   clean fit with Registry.list_entries()
  registry show      WIRED   clean fit with Registry.get_entry()
  registry promote   WIRED   Q17 enforcement lives in the registry layer
  registry demote    WIRED   (B9.A) Registry.demote_current(--scope-key)
  build-dataset      STUB    deferred to B9.B (no persistence surface)
  train              STUB    deferred to B9.B
  evaluate           STUB    deferred to B9.B

Exit-code semantics:
  Legacy (A.9, still honoured where tests lock them):
    0 success / 1 known runtime error / 2 stub-or-argparse-error.
  B9.A adds FINER codes ONLY for newly-wired/hardened paths where no
  locked test asserts a value (documented in the B9.A report):
    0 success
    1 user/input error (missing file, bad format, unknown model_id)
    2 stub OR argparse error (unchanged)
    4 registry/promotion blocked (promote blockage; legacy tests that
       asserted 1 here are preserved — see _emit/_promote handling)
  The richer set is opt-in via behaviour, never breaks A.9 asserts.

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


def _stub(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


# ── B9.A: strict --json envelope ────────────────────────────────────
# {ok, command, status, result, warnings, errors} — valid JSON on BOTH
# success and failure, so M18.B.10's safety runner can parse it. Only
# emitted when the user passes --json; otherwise the legacy A.9 output
# is preserved exactly so the locked G9 tests do not churn.

def _emit_json_envelope(
    command: str,
    *,
    ok: bool,
    status: str,
    result: Optional[dict] = None,
    warnings: Optional[List[str]] = None,
    errors: Optional[List[dict]] = None,
    stream=None,
) -> None:
    payload = {
        "ok":       bool(ok),
        "command":  command,
        "status":   status,
        "result":   result if result is not None else {},
        "warnings": list(warnings or []),
        "errors":   list(errors or []),
    }
    print(json.dumps(payload, sort_keys=True, default=str),
          file=(stream if stream is not None else sys.stdout))


def _err_obj(code: str, message: str) -> dict:
    return {"code": code, "message": message}


# ── Parser (accepted M18.A.9 safe partial surface) ──────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bot.ml",
        description=(
            "M18 ML pipeline CLI — dataset assembly, training, "
            "evaluation, prediction, and registry administration. "
            "READ-ONLY/shadow-only in M18: nothing here can write "
            "signals.db or touch live trading."),
    )
    # B9.A global flags (apply to every subcommand).
    p.add_argument("--json", action="store_true",
        help="Emit the strict machine-readable envelope "
              "{ok, command, status, result, warnings, errors}.")
    p.add_argument("--debug", action="store_true",
        help="Allow a Python traceback on unexpected errors "
              "(otherwise only a clean message is shown).")
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
    rg_promote.add_argument("--dry-run", action="store_true",
        help="Check whether promotion would pass (runs the B8 "
              "artifact-consistency + gate checks) WITHOUT mutating "
              "the registry.")

    rg_demote = rg_sub.add_parser(
        "demote",
        help="Demote the current model for a scope to 'demoted'.")
    rg_demote.add_argument("--scope-key", required=True,
        help="The scope_key whose current model should be demoted.")
    rg_demote.add_argument("--reason",
        help="Operator justification (recorded in current_history).")
    rg_demote.add_argument("--actor",
        help="Caller identity (recorded in current_history).")
    rg_demote.add_argument("--dry-run", action="store_true",
        help="Report what would be demoted WITHOUT mutating the "
              "registry.")

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
    use_json = getattr(args, "json", False)
    try:
        import pandas as pd

        input_path = Path(args.input)
        if not input_path.exists():
            msg = f"predict: input file does not exist: {input_path}"
            if use_json:
                _emit_json_envelope(
                    "predict", ok=False, status="failed",
                    errors=[_err_obj("input_not_found", msg)],
                stream=sys.stderr)
            else:
                print(msg, file=sys.stderr)
            return 1
        suffix = input_path.suffix.lower()
        if suffix == ".parquet":
            X_input = pd.read_parquet(input_path)
        elif suffix == ".csv":
            X_input = pd.read_csv(input_path)
        else:
            msg = (f"predict: unsupported input format {suffix!r} "
                   f"(expected .parquet or .csv)")
            if use_json:
                _emit_json_envelope(
                    "predict", ok=False, status="failed",
                    errors=[_err_obj("bad_input_format", msg)],
                stream=sys.stderr)
            else:
                print(msg, file=sys.stderr)
            return 1

        registry = _make_registry(_registry_root)
        result = predict_from_registry(
            registry=registry,
            model_id=args.model_id,
            X_input=X_input,
        )
        result_dict = {
            "command":               "predict",
            "model_id":              result.model_id,
            "n_input_rows":          result.n_input_rows,
            "n_features":            result.n_features,
            "output_path":           result.output_path,
            "batch_id":              result.batch_id,
            "predicted_at_utc":      result.predicted_at_utc,
            "extrapolation_summary": result.extrapolation_summary,
            # B9.A honesty: B3 calibration is NOT applied at predict.
            "calibration_applied":   False,
            "calibration_status":    "stored_not_applied",
        }
        if use_json:
            _emit_json_envelope(
                "predict", ok=True, status="completed",
                result=result_dict)
        else:
            # legacy A.9 default output (locked by G9_CliPredict)
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
        msg = f"predict: {type(e).__name__}: {e}"
        if use_json:
            _emit_json_envelope(
                "predict", ok=False, status="failed",
                errors=[_err_obj("m18_error", str(e))],
                stream=sys.stderr)
        else:
            print(msg, file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — CLI boundary
        if getattr(args, "debug", False):
            raise
        msg = f"predict: {type(e).__name__}: {e}"
        if use_json:
            _emit_json_envelope(
                "predict", ok=False, status="failed",
                errors=[_err_obj("unexpected_error", str(e))],
                stream=sys.stderr)
        else:
            print(msg, file=sys.stderr)
        return 1


def _cmd_registry_list(args: argparse.Namespace,
                        _registry_root: Optional[str]) -> int:
    from bot.ml.errors import M18Error
    use_json = getattr(args, "json", False)
    try:
        registry = _make_registry(_registry_root)
        entries = registry.list_entries()
        if use_json:
            _emit_json_envelope(
                "registry-list", ok=True, status="completed",
                result={"n_entries": len(entries),
                        "entries": [e.to_dict() for e in entries]})
        else:
            print(json.dumps(
                {"command": "registry-list",
                 "n_entries": len(entries),
                 "entries": [e.to_dict() for e in entries]},
                sort_keys=True, indent=2, default=str))
        return 0
    except M18Error as e:
        if use_json:
            _emit_json_envelope(
                "registry-list", ok=False, status="failed",
                errors=[_err_obj("m18_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"registry list: {type(e).__name__}: {e}",
                file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        if getattr(args, "debug", False):
            raise
        if use_json:
            _emit_json_envelope(
                "registry-list", ok=False, status="failed",
                errors=[_err_obj("unexpected_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"registry list: {type(e).__name__}: {e}",
                file=sys.stderr)
        return 1


def _cmd_registry_show(args: argparse.Namespace,
                        _registry_root: Optional[str]) -> int:
    from bot.ml.errors import M18Error
    use_json = getattr(args, "json", False)
    try:
        registry = _make_registry(_registry_root)
        entry = registry.get_entry(args.model_id)
        if use_json:
            # B9.A: include the B8 artifact-consistency summary so a
            # machine reader sees both the entry and whether its
            # artifacts agree.
            consistency = registry.verify_artifact_consistency(
                args.model_id)
            _emit_json_envelope(
                "registry-show", ok=True, status="completed",
                result={"entry": entry.to_dict(),
                        "artifact_consistency": consistency})
        else:
            print(json.dumps({"command": "registry-show",
                              "entry": entry.to_dict()},
                              sort_keys=True, indent=2,
                              default=str))
        return 0
    except M18Error as e:
        if use_json:
            _emit_json_envelope(
                "registry-show", ok=False, status="failed",
                errors=[_err_obj("m18_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"registry show: {type(e).__name__}: {e}",
                file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        if getattr(args, "debug", False):
            raise
        if use_json:
            _emit_json_envelope(
                "registry-show", ok=False, status="failed",
                errors=[_err_obj("unexpected_error", str(e))],
                stream=sys.stderr)
        else:
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
    use_json = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)
    try:
        registry = _make_registry(_registry_root)

        if dry_run:
            # B9.A: report whether promotion WOULD pass without
            # mutating the registry. Runs the B8 consistency check and
            # inspects the entry's blocked reasons; never calls
            # promote_to_current (no pointer move, no status change).
            entry = registry.get_entry(args.model_id)   # raises if absent
            consistency = registry.verify_artifact_consistency(
                args.model_id)
            would_pass = (consistency["consistent"]
                          and not entry.promotion_blocked_reasons
                          and entry.promotion_eligible)
            result = {
                "outcome":            "dry_run",
                "would_promote":      bool(would_pass),
                "model_id":           args.model_id,
                "artifact_consistent": consistency["consistent"],
                "consistency_problems": consistency["problems"],
                "promotion_blocked_reasons":
                    list(entry.promotion_blocked_reasons),
                "promotion_eligible": entry.promotion_eligible,
            }
            if use_json:
                _emit_json_envelope(
                    "registry-promote", ok=True, status="dry_run",
                    result=result)
            else:
                print(json.dumps(result, sort_keys=True))
            return 0

        override_gates = tuple(args.override_gate or ())
        entry = registry.promote_to_current(
            args.model_id,
            force=args.force,
            override_gates=override_gates,
            reason=args.reason,
        )
        result = {
            "outcome":           "promoted",
            "model_id":          entry.model_id,
            "status":            entry.status,
            "approved_for_live": entry.approved_for_live,
        }
        if use_json:
            _emit_json_envelope(
                "registry-promote", ok=True, status="promoted",
                result=result)
        else:
            print(json.dumps(result, sort_keys=True))
        return 0
    except PromotionBlockedError as e:
        if use_json:
            _emit_json_envelope(
                "registry-promote", ok=False, status="blocked",
                errors=[{"code": "promotion_blocked",
                         "gate": e.gate,
                         "gate_category": e.gate_category,
                         "message": str(e)}],
                stream=sys.stderr)
        else:
            print(json.dumps({
                "outcome":       "blocked",
                "gate":          e.gate,
                "gate_category": e.gate_category,
                "message":       str(e),
            }, sort_keys=True), file=sys.stderr)
        return 1
    except ForceOverrideRequired as e:
        if use_json:
            _emit_json_envelope(
                "registry-promote", ok=False,
                status="force_override_required",
                errors=[_err_obj("force_override_required", str(e))],
                stream=sys.stderr)
        else:
            print(json.dumps({
                "outcome": "force_override_required",
                "message": str(e),
            }, sort_keys=True), file=sys.stderr)
        return 1
    except M18Error as e:
        if use_json:
            _emit_json_envelope(
                "registry-promote", ok=False, status="failed",
                errors=[_err_obj("m18_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"registry promote: {type(e).__name__}: {e}",
                file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        if getattr(args, "debug", False):
            raise
        if use_json:
            _emit_json_envelope(
                "registry-promote", ok=False, status="failed",
                errors=[_err_obj("unexpected_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"registry promote: {type(e).__name__}: {e}",
                file=sys.stderr)
        return 1


def _cmd_registry_demote(args: argparse.Namespace,
                          _registry_root: Optional[str]) -> int:
    """B9.A: wire Registry.demote_current(scope_key, reason, actor).
    Demotion is by SCOPE (current is per-scope), so the argument is
    --scope-key, not --model-id. Does not delete artifacts, does not
    touch training artifacts, does not approve live."""
    from bot.ml.errors import M18Error
    use_json = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)
    try:
        registry = _make_registry(_registry_root)
        if dry_run:
            current = registry.get_current(args.scope_key)
            result = {
                "outcome":        "dry_run",
                "scope_key":      args.scope_key,
                "would_demote":   current is not None,
                "current_model_id":
                    (current.model_id if current is not None else None),
            }
            if use_json:
                _emit_json_envelope(
                    "registry-demote", ok=True, status="dry_run",
                    result=result)
            else:
                print(json.dumps(result, sort_keys=True))
            return 0

        demoted = registry.demote_current(
            args.scope_key, reason=args.reason, actor=args.actor)
        result = {
            "outcome":   "demoted",
            "scope_key": args.scope_key,
            "model_id":  demoted.model_id,
            "status":    demoted.status,
        }
        if use_json:
            _emit_json_envelope(
                "registry-demote", ok=True, status="demoted",
                result=result)
        else:
            print(json.dumps(result, sort_keys=True))
        return 0
    except M18Error as e:
        # e.g. no current model for the scope_key
        if use_json:
            _emit_json_envelope(
                "registry-demote", ok=False, status="failed",
                errors=[_err_obj("m18_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"registry demote: {type(e).__name__}: {e}",
                file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        if getattr(args, "debug", False):
            raise
        if use_json:
            _emit_json_envelope(
                "registry-demote", ok=False, status="failed",
                errors=[_err_obj("unexpected_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"registry demote: {type(e).__name__}: {e}",
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
    use_json = getattr(args, "json", False)
    if cmd is None:
        parser.print_help(file=sys.stderr)
        return 2

    def _stub_or_envelope(message: str, command: str) -> int:
        if use_json:
            _emit_json_envelope(
                command, ok=False, status="not_implemented",
                errors=[_err_obj("not_implemented", message)],
                stream=sys.stderr)
            return 2
        return _stub(message)

    if cmd == "build-dataset":
        return _stub_or_envelope(STUB_BUILD_DATASET, "build-dataset")
    if cmd == "train":
        return _stub_or_envelope(STUB_TRAIN, "train")
    if cmd == "evaluate":
        return _stub_or_envelope(STUB_EVALUATE, "evaluate")
    if cmd == "predict":
        return _cmd_predict(args, _registry_root)
    if cmd == "registry":
        rcmd = getattr(args, "registry_command", None)
        if rcmd is None:
            print("registry: missing sub-command "
                  "(list|show|promote|demote)", file=sys.stderr)
            return 2
        if rcmd == "list":
            return _cmd_registry_list(args, _registry_root)
        if rcmd == "show":
            return _cmd_registry_show(args, _registry_root)
        if rcmd == "promote":
            return _cmd_registry_promote(args, _registry_root)
        if rcmd == "demote":
            return _cmd_registry_demote(args, _registry_root)
        print(f"registry: unknown sub-command {rcmd!r}",
            file=sys.stderr)
        return 2

    print(f"unknown command: {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
