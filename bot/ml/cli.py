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
        help="Assemble a dataset from M16 bars (real in-process "
              "assembler). Emits an inspectable dataset+manifest "
              "report — NOT a reloadable training handoff (see B9.C).")
    bd.add_argument("--symbol", required=True, help="Ticker, e.g. AAPL.")
    bd.add_argument("--anchor-tf", default="15m",
        help="Anchor timeframe (default 15m).")
    bd.add_argument("--anchor-set", default=None,
        help="Anchor set name (default: assembler default).")
    bd.add_argument("--timeframes", default="1D,4H,1H,15m",
        help="Comma-separated timeframes to load (default "
              "1D,4H,1H,15m).")
    bd.add_argument("--output",
        help="Directory to write dataset.parquet + manifest.json. "
              "If omitted, nothing is written (summary only).")
    bd.add_argument("--fixture-mode", action="store_true",
        help="Assemble in fixture mode (small-sample friendly).")
    bd.add_argument("--dry-run", action="store_true",
        help="Validate config and report the planned build WITHOUT "
              "loading bars or writing artifacts.")

    tr = sub.add_parser(
        "train",
        help="Train + evaluate + register a candidate, in-process "
              "(load bars -> assemble -> train -> evaluate -> "
              "register). Assembles its OWN dataset each run; does not "
              "consume a build-dataset output yet (see B9.C).")
    tr.add_argument("--symbol", required=True, help="Ticker, e.g. AAPL.")
    tr.add_argument("--model-type", required=True,
        help="Model type (e.g. B2_logistic, M_random_forest).")
    tr.add_argument("--anchor-tf", default="15m")
    tr.add_argument("--anchor-set", default=None)
    tr.add_argument("--timeframes", default="1D,4H,1H,15m")
    tr.add_argument("--target-label-id",
        default="triple_barrier_atr_2_3_50_won")
    tr.add_argument("--train-mode", default="model_b_candidate_quality")
    tr.add_argument("--seed", type=int, default=42)
    tr.add_argument("--fixture-mode", action="store_true")
    tr.add_argument("--registry-root",
        help="Registry root dir (default: the package default).")
    tr.add_argument("--dry-run", action="store_true",
        help="Validate config + assemble, but do NOT train or "
              "register.")

    ev = sub.add_parser(
        "evaluate",
        help="Strict inspection of a registered model: read its "
              "evaluation_report.json + run B8 artifact-consistency.")
    ev.add_argument("--model-id", required=True,
        help="Registry model_id to evaluate.")
    ev.add_argument("--registry-root",
        help="Registry root dir (default: the package default).")

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
        help="Check whether NON-FORCED promotion would pass (runs the "
              "B8 artifact-consistency + gate checks) WITHOUT mutating "
              "the registry. Not supported with --force (forced-"
              "promotion simulation is not implemented).")

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

    au = sub.add_parser(
        "audit",
        help="Read-only audit/safety runner (M18.B.10). Consolidates "
              "the per-phase repo/hygiene verification into one "
              "parseable verdict. Never mutates anything.")
    au.add_argument("--mode", choices=("static", "hygiene", "full"),
        default="hygiene",
        help="static = fast repo/file checks; hygiene (default) = "
              "static + G10 hygiene test classes; full = + M17.B full "
              "regression (opt-in, heavier).")

    return p


# ── Wired handlers ───────────────────────────────────────────────────

def _make_registry(_registry_root: Optional[str]):
    from bot.ml.registry.registry import Registry
    if _registry_root is not None:
        return Registry(root=_registry_root)
    return Registry()


# ── B9.B-1: real in-process build-dataset / train / evaluate ────────
# The CLI calls the EXISTING real interfaces:
#   m16_loader.load_bars  -> DatasetAssembler.build
#   Trainer.train_one -> evaluate_model -> Registry.register_candidate
#   Registry.get_entry / verify_artifact_consistency
# No new architecture, no AssemblerResult persistence (deferred: B9.C).
#
# `_bars_provider` is an INTERNAL, test-only Python kwarg (like
# `_registry_root`): a callable (symbol, timeframes) -> {tf: DataFrame}.
# Production default loads read-only from M16 via load_bars. Tests
# inject deterministic fixture bars into the SAME real code path — they
# never fake the assembler/trainer/registry logic.

def _default_bars_provider(symbol, timeframes):
    from bot.ml.dataset import m16_loader
    return {tf: m16_loader.load_bars(symbol, tf) for tf in timeframes}


def _build_assembler_config(args):
    from bot.ml.dataset import assembler as _asm
    kwargs = dict(
        symbol=args.symbol,
        anchor_tf=args.anchor_tf,
        timeframes=tuple(t.strip() for t in args.timeframes.split(",")
                         if t.strip()),
        fixture_mode=getattr(args, "fixture_mode", False),
    )
    if getattr(args, "anchor_set", None):
        kwargs["anchor_set"] = args.anchor_set
    if getattr(args, "fixture_mode", False):
        # fixture samples are too small for a meaningful AV test
        kwargs["skip_adversarial"] = True
    return _asm.AssemblerConfig(**kwargs)


def _cmd_build_dataset(args, _registry_root, _bars_provider) -> int:
    from bot.ml.errors import M18Error
    from bot.ml.dataset import assembler as _asm
    use_json = getattr(args, "json", False)
    try:
        cfg = _build_assembler_config(args)

        if getattr(args, "dry_run", False):
            result = {
                "outcome":    "dry_run",
                "symbol":     cfg.symbol,
                "anchor_tf":  cfg.anchor_tf,
                "anchor_set": cfg.anchor_set,
                "timeframes": list(cfg.timeframes),
                "would_write": bool(args.output),
            }
            if use_json:
                _emit_json_envelope("build-dataset", ok=True,
                                    status="dry_run", result=result)
            else:
                print(json.dumps(result, sort_keys=True))
            return 0

        provider = _bars_provider or _default_bars_provider
        per_tf = provider(cfg.symbol, list(cfg.timeframes))
        res = _asm.DatasetAssembler(cfg).build(per_tf_bars=per_tf)

        summary = {
            "command":              "build-dataset",
            "symbol":               cfg.symbol,
            "dataset_id":           res.manifest.dataset_id,
            "dataset_hash_sha256":  res.manifest.dataset_hash_sha256,
            "n_rows":               int(res.dataset.shape[0]),
            "n_columns":            int(res.dataset.shape[1]),
            "anchor_set":           res.manifest.anchor_set,
            "adversarial_validation_status":
                res.adversarial_validation_status,
            "adversarial_validation_reason":
                res.adversarial_validation_reason,
            # honesty: this report is NOT a reloadable training handoff
            "reloadable_training_handoff": False,
        }

        written = {}
        if args.output:
            out_dir = Path(args.output)
            out_dir.mkdir(parents=True, exist_ok=True)
            ds_path = out_dir / "dataset.parquet"
            mf_path = out_dir / "manifest.json"
            res.dataset.to_parquet(ds_path, index=False)
            mf_path.write_text(
                json.dumps(res.manifest.to_dict(), sort_keys=True,
                           default=str))
            written = {"dataset_parquet": str(ds_path),
                       "manifest_json": str(mf_path)}
        summary["written"] = written

        if use_json:
            _emit_json_envelope("build-dataset", ok=True,
                                status="completed", result=summary)
        else:
            print(json.dumps(summary, sort_keys=True, default=str))
        return 0
    except M18Error as e:
        if use_json:
            _emit_json_envelope("build-dataset", ok=False,
                status="failed",
                errors=[_err_obj("m18_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"build-dataset: {type(e).__name__}: {e}",
                file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        if getattr(args, "debug", False):
            raise
        if use_json:
            _emit_json_envelope("build-dataset", ok=False,
                status="failed",
                errors=[_err_obj("unexpected_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"build-dataset: {type(e).__name__}: {e}",
                file=sys.stderr)
        return 1


def _cmd_train(args, _registry_root, _bars_provider) -> int:
    from bot.ml.errors import M18Error
    from bot.ml.dataset import assembler as _asm
    from bot.ml.models.trainer import Trainer, TrainConfig
    from bot.ml.evaluation.evaluator import evaluate_model
    use_json = getattr(args, "json", False)
    try:
        cfg = _build_assembler_config(args)
        provider = _bars_provider or _default_bars_provider

        # assemble (real) — needed for both dry-run validation and train
        per_tf = provider(cfg.symbol, list(cfg.timeframes))
        res = _asm.DatasetAssembler(cfg).build(per_tf_bars=per_tf)

        train_config = TrainConfig(
            dataset_id=res.manifest.dataset_id,
            model_type=args.model_type,
            train_mode=args.train_mode,
            target_label_id=args.target_label_id,
            hyperparameters={},
            seed=int(args.seed),
            fixture_mode=getattr(args, "fixture_mode", False),
        )

        if getattr(args, "dry_run", False):
            result = {
                "outcome":    "dry_run",
                "symbol":     cfg.symbol,
                "model_type": args.model_type,
                "dataset_id": res.manifest.dataset_id,
                "n_rows":     int(res.dataset.shape[0]),
                "registered": False,
            }
            if use_json:
                _emit_json_envelope("train", ok=True, status="dry_run",
                                    result=result)
            else:
                print(json.dumps(result, sort_keys=True))
            return 0

        out = Trainer().train_one(train_config, res)
        rep = evaluate_model(out, res)
        registry = _make_registry(_registry_root)
        entry = registry.register_candidate(out, rep, res)

        result = {
            "command":             "train",
            "model_id":            entry.model_id,
            "status":              entry.status,
            "model_type":          entry.model_type,
            "dataset_hash_sha256": entry.dataset_hash_sha256,
            "n_features":          out.n_features,
            "approved_for_live":   entry.approved_for_live,
            "assembles_own_dataset_in_process": True,
        }
        if use_json:
            _emit_json_envelope("train", ok=True, status="completed",
                                result=result)
        else:
            print(json.dumps(result, sort_keys=True, default=str))
        return 0
    except M18Error as e:
        if use_json:
            _emit_json_envelope("train", ok=False, status="failed",
                errors=[_err_obj("m18_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"train: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        if getattr(args, "debug", False):
            raise
        if use_json:
            _emit_json_envelope("train", ok=False, status="failed",
                errors=[_err_obj("unexpected_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"train: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _cmd_evaluate(args, _registry_root) -> int:
    """Strict inspection: read the persisted evaluation_report.json and
    run the B8 artifact-consistency check. Fails non-zero if the report
    is missing/corrupt OR the artifacts are inconsistent."""
    from bot.ml.errors import M18Error
    import bot.ml.registry.storage as _store
    use_json = getattr(args, "json", False)
    try:
        registry = _make_registry(_registry_root)
        entry = registry.get_entry(args.model_id)   # raises if absent
        consistency = registry.verify_artifact_consistency(
            args.model_id)

        ev_path = _store.artifact_path(
            registry.root, args.model_id, _store.ARTIFACT_EVAL_REPORT)
        if not ev_path.exists():
            msg = (f"evaluate: evaluation_report.json missing for "
                   f"model_id={args.model_id!r}")
            if use_json:
                _emit_json_envelope("evaluate", ok=False,
                    status="failed",
                    errors=[_err_obj("missing_evaluation_report", msg)],
                    stream=sys.stderr)
            else:
                print(msg, file=sys.stderr)
            return 1
        try:
            ev_report = _store.read_json(ev_path)
        except Exception:
            msg = (f"evaluate: evaluation_report.json corrupt for "
                   f"model_id={args.model_id!r}")
            if use_json:
                _emit_json_envelope("evaluate", ok=False,
                    status="failed",
                    errors=[_err_obj("corrupt_evaluation_report", msg)],
                    stream=sys.stderr)
            else:
                print(msg, file=sys.stderr)
            return 1

        if not consistency["consistent"]:
            # strict by default: inconsistent artifacts fail non-zero
            if use_json:
                _emit_json_envelope("evaluate", ok=False,
                    status="inconsistent",
                    result={"model_id": args.model_id,
                            "artifact_consistency": consistency},
                    errors=[_err_obj("artifact_inconsistent",
                            f"problems: {consistency['problems']}")],
                    stream=sys.stderr)
            else:
                print(json.dumps(
                    {"command": "evaluate", "outcome": "inconsistent",
                     "problems": consistency["problems"]},
                    sort_keys=True), file=sys.stderr)
            return 1

        result = {
            "command":              "evaluate",
            "model_id":             args.model_id,
            "model_type":           entry.model_type,
            "dataset_hash_sha256":  entry.dataset_hash_sha256,
            "artifact_consistency": consistency,
            "evaluation_report":    ev_report,
        }
        if use_json:
            _emit_json_envelope("evaluate", ok=True, status="completed",
                                result=result)
        else:
            print(json.dumps(result, sort_keys=True, default=str))
        return 0
    except M18Error as e:
        if use_json:
            _emit_json_envelope("evaluate", ok=False, status="failed",
                errors=[_err_obj("m18_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"evaluate: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        if getattr(args, "debug", False):
            raise
        if use_json:
            _emit_json_envelope("evaluate", ok=False, status="failed",
                errors=[_err_obj("unexpected_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"evaluate: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _cmd_audit(args, _registry_root) -> int:
    """B10: thin wrapper over the read-only AuditRunner. Default
    output is human-readable; --json emits the B9 envelope. Exit 0 if
    every sub-check passed, 1 otherwise."""
    use_json = getattr(args, "json", False)
    try:
        from bot.ml.audit import AuditRunner
        report = AuditRunner().run(mode=args.mode)
        ok = report["ok"]
        if use_json:
            errors = [
                _err_obj("check_failed", name)
                for name in report["failed"]
            ]
            _emit_json_envelope(
                "audit", ok=ok,
                status="passed" if ok else "failed",
                result=report, errors=errors,
                stream=(None if ok else sys.stderr))
        else:
            print(f"audit mode={report['mode']} "
                  f"{report['n_checks']} checks, "
                  f"{report['n_failed']} failed -> "
                  f"{'OK' if ok else 'FAIL'}")
            for c in report["checks"]:
                print(f"  [{c['status']}] {c['name']}: {c['details']}")
            if not ok:
                print(f"FAILED: {report['failed']}", file=sys.stderr)
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        if getattr(args, "debug", False):
            raise
        if use_json:
            _emit_json_envelope(
                "audit", ok=False, status="failed",
                errors=[_err_obj("unexpected_error", str(e))],
                stream=sys.stderr)
        else:
            print(f"audit: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


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
            # B9.A: report whether NON-FORCED promotion would pass
            # without mutating the registry. Runs the B8 consistency
            # check and inspects the entry's blocked reasons; never
            # calls promote_to_current (no pointer move, no status
            # change).
            #
            # IMPORTANT (B9.A fix): this models ONLY the non-forced
            # promotion path. It deliberately does NOT simulate the
            # --force / --override-gate / --reason judgment-gate logic
            # in Registry.promote_to_current, so combining --dry-run
            # with --force would be MISLEADING (a judgment-blocked
            # model that --force could promote would show
            # would_promote=false). Rather than fake that path, we
            # reject the combination cleanly until a real no-mutation
            # forced-promotion simulation exists.
            if args.force:
                msg = ("registry promote --dry-run --force is not "
                       "supported: dry-run models only the non-forced "
                       "promotion path and cannot faithfully simulate "
                       "--force/--override-gate judgment-gate logic "
                       "without risking a misleading result. Run "
                       "--dry-run without --force to preview, or run "
                       "the real --force promotion.")
                if use_json:
                    _emit_json_envelope(
                        "registry-promote", ok=False,
                        status="dry_run_force_unsupported",
                        errors=[_err_obj(
                            "dry_run_force_unsupported", msg)],
                        stream=sys.stderr)
                else:
                    print(f"registry promote: {msg}", file=sys.stderr)
                return 1

            entry = registry.get_entry(args.model_id)   # raises if absent
            consistency = registry.verify_artifact_consistency(
                args.model_id)
            would_pass = (consistency["consistent"]
                          and not entry.promotion_blocked_reasons
                          and entry.promotion_eligible)
            result = {
                "outcome":            "dry_run",
                "dry_run_scope":      "non_forced_only",
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
          _registry_root: Optional[str] = None,
          _bars_provider=None) -> int:
    """CLI entry point.

    `_registry_root` and `_bars_provider` are INTERNAL, test-only
    Python kwargs (note the leading underscore). They are intentionally
    NOT exposed as argparse flags — production callers never see them.
    `_registry_root` points the registry at a tempdir; `_bars_provider`
    is a callable (symbol, timeframes) -> {tf: DataFrame} that supplies
    deterministic fixture bars INTO the real assembler code path (it
    does not fake the assembler/trainer/registry logic). Production
    defaults to a read-only M16 load.
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

    if cmd == "build-dataset":
        return _cmd_build_dataset(args, _registry_root, _bars_provider)
    if cmd == "train":
        return _cmd_train(args, _registry_root, _bars_provider)
    if cmd == "evaluate":
        return _cmd_evaluate(args, _registry_root)
    if cmd == "audit":
        return _cmd_audit(args, _registry_root)
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
