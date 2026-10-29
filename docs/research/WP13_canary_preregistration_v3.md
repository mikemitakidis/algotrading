# WP13 canary — identity binding, historical v2 evidence, and the preregistered v3 run

Status: **v3 preregistered, NOT yet run** (identity schema `/2`: the full planted-effect spec is recorded). It runs once, on the WP13 head the reviewer approves, because
the experiment identity includes the code fingerprint and any later code change would make the
result stale. Synthetic evidence only: it calibrates the walk-forward *decision procedure*, never a
real-market edge.

## 1. What changed (PR #24 review 5686443135)

The v2 battery's plan bound only `pipeline_identity("wf:<template>", code_identity(CODE_PATHS))`.
The WFConfig, the search space, the universe and session counts, the generator and planted-effect
protocol, the cost model and the seed schedule were not part of any identity, so a battery could have
been reused for an evaluation that differed in any of them.

Now `research/optimiser/canary.py::experiment_identity(template, cfg, protocol)` records, canonically:

| Section | Contents |
|---|---|
| `pipeline` | family `wf:<template>`; decision code paths and `code_identity`; trial code paths and `code_identity` (what the candidate's ledger trials carry); the full `WFConfig` (folds, embargo, holdout fraction, metric, min OOS trades, DSR/PBO thresholds, stress, size multipliers, economics model, engine config, basis); the template's resolved parameter search space |
| `data_protocol` | generator `research.common.synthetic.generate`; every `MarketSpec` parameter except the per-run seed; planted effect kind and horizon; universe size, session count, timeframe |
| `design` | name, nulls, effect sizes, positives per effect, required effects (resolved), false-pass target, alpha, minimum power, base seed, power rule |
| `seed_schedule` | `child_seed(base_seed, plan.name, 'null'/'pos', [repr(effect),] index)` |
| `holdout_policy` | holdout fraction; sealed holdout never used by the canary |
| `costs_and_liquidity` | the economics model (fees, regulatory schedule, spread buckets, impact, ADV/bar participation, ...) |

- The identity is part of the `BatteryPlan` and therefore of its digest. It is recorded in the ledger
  with its own `experiment_digest`. `pipeline_id` is bound to the digest of the `pipeline` section.
- The report card (`research/integrity/report_card.py`) re-hashes the recorded plan, and requires the
  identity for `wf:` families and for any battery that carries one. It accepts the battery only when:
  - the recorded experiment hashes to its digest;
  - that digest equals the `canary_experiment` the evaluation requires;
  - the family and the candidate trial's code digest match;
  - `pipeline_id` is bound to the experiment;
  - the plan agrees with the declared design.
- Any mismatch leaves the canary unverified, so the card cannot PASS.
- Plans without an identity hash exactly as before. Other research batteries are unaffected.

## 2. The historical v2 battery — preserved, not re-labelled

The v2 battery was run at commit `609aa85`, with plan digest
`16aaccc1460078fb26501811ff7103021536523c90db649e986abf6fbf326f43`. Its report is copied byte-for-byte to
`docs/research/evidence/wp13_canary_v2_historical_609aa85.json`
(sha256 `7a6760a18e76aff9a01d414c5739fa22c5b66cb78b971d140ab9dea2f2a7268f`).

| Item | v2 result |
|---|---|
| Template / config | `mean_reversion`, `WFConfig()` defaults |
| Universe | 30 symbols × 750 daily sessions |
| Planted effect | `rsi_oversold`, 0.5σ, horizon 5 |
| Seeds | base seed 5000, name `wp13-canary-v2` |
| Nulls | **0 / 59** passes → false-pass upper bound 4.95 % |
| Positives | **20 / 20** detections → power lower bound 0.83 |
| Verdict | `CALIBRATED` |
| Ledger | verified at run time (tip `3bcd4885…`); in memory, not exported |

Is it valid under the corrected definition?

- **As evidence of that exact run: yes.** Every input is declared in the report or fixed by the code at
  `609aa85`, so it can be reproduced by re-running that commit.
- **As calibration accepted by the report card: no.**
  - Its plan carries no experiment identity, and its ledger was not exported, so the plan/result entries
    cannot be checked.
  - Its `pipeline_id` used the decision-code digest, while WP13 trials carry the search-code digest. The
    report card therefore never matched it to a WP13 candidate. That was fail-closed: it could not
    falsely calibrate anything, but it calibrated nothing either.
  - The corrected code changes the code fingerprint (`canary.py`, `controls.py` and `report_card.py` are
    in the code paths).
- The corrections touch only identity and recording code. They do not touch the decision code under
  test (`walkforward`, `genome`, `economics`, `bot/backtesting`). The v2 counts are therefore expected to
  describe the corrected pipeline's decisions too. That expectation is what v3 tests; v2 is not
  re-labelled to claim it.

## 3. Preregistered v3 (`canary.PROTOCOL_V3`)

The design is identical to v2. Only the name changes, which gives new, independent seeds. No retuning.

- `template = mean_reversion`, `cfg = WFConfig()`.
- `n_nulls = 59`, `effect_sizes = (0.5,)`, `n_per_effect = 20`, `required = (0.5,)`.
- `n_symbols = 30`, `n_sessions = 750`, `horizon = 5`, `base_seed = 5000`, `name = wp13-canary-v3`.
- `false_pass_target = 0.05`, `alpha = 0.05`, `min_power = 0.8`, power rule: Clopper–Pearson lower bound
  at every required effect.

The identity digest at the head that carries this document is printed by
`python -c "from research.optimiser import canary as C, walkforward as W; print(C.experiment_digest('mean_reversion', W.WFConfig(), C.PROTOCOL_V3))"`.
It changes with any code change in `CODE_PATHS`, by design.

## 4. One-shot execution procedure (prepared; NOT run)

**When:**
- once, after #19 → #20 → #24 are merged;
- on the merged `main` commit whose WP13 code is the reviewed source.

Never on a draft branch, and never twice.

**Environment:**
- Python 3.12.3, NumPy 1.26.4, pandas 2.2.3, SciPy 1.17.1. The result records it in `environment`.
- Offline: no network, no broker, no VPS.

**Commands:**

```bash
git switch --detach <merged main SHA>          # record the SHA in the report
git status --porcelain                         # must be empty
python -c "from research.optimiser import canary as C, walkforward as W; \
print(C.experiment_digest('mean_reversion', W.WFConfig(), C.PROTOCOL_V3))"   # record before the run
OUT=$HOME/m21_canary_v3_$(git rev-parse --short HEAD)
OMP_NUM_THREADS=1 python -m research.optimiser.canary --template mean_reversion --out "$OUT" 2>&1 | tee "$OUT.log"
```

- No `--name` or design flags: the CLI then runs exactly `PROTOCOL_V3`.
- Expected runtime: 79 walk-forward runs at about 33 s each, so **about 45–60 minutes** (measured on a 2-CPU host).

**Outputs**, in `$OUT/`:
- `canary_mean_reversion.json`: verdict, counts, Clopper–Pearson bounds, the plan record with the full experiment identity, `experiment_digest`, protocol, `ledger_tip`, `ledger_ok`, environment;
- `canary_mean_reversion.ledger.jsonl`: hash-chained;
- `canary_mean_reversion.ledger.db`;
- the run log.

**Acceptance of the run itself, before reading the verdict:**
- `ledger_ok` is true;
- `Ledger.verify_jsonl(...)` is ok;
- the recorded `experiment_digest` equals the digest printed before the run;
- the plan's `design` equals `PROTOCOL_V3`.

**Preservation:**
- Commit the JSON, the JSONL, the log and a `sha256sum` manifest verbatim to `docs/research/evidence/wp13_canary_v3_<sha>/`, in a docs-only PR.
- Never edit, re-run or re-name them.
- v2 stays where it is.

**Reporting.** One Issue #3 comment and one PR #24 comment, each giving:
- verdict, null passes / 59 and the upper bound, detections / 20 and the lower bound;
- experiment digest, code digest, ledger tip, environment, runtime, merged SHA.

**Decision rule (fixed now):**
- The verdict is final for this identity.
- If it is not CALIBRATED: no retuning, no second run. The real-data walk-forward cannot obtain a verified canary, so it cannot PASS, until a new design is preregistered with your approval.
- A WP13 evaluation consumes v3 only by passing that exact digest as `canary_experiment`.
