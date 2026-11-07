# M21 E6 — the protected `dashboard/app.py` wiring (prepared, NOT applied)

**Status:** prepared 29 Oct 2026, revised 30 Oct after the release-safety review. `dashboard/app.py` is a protected
runtime file and is **not** changed by PR E6. This file and `E6_dashboard_app_wiring.patch` are the exact change
for your approval.

**30 Oct revision:** the view is now **read-only**. Its first version had propose/approve/activate/rollback/withdraw
routes that changed the active template configuration outside the WP14 Promotion & Approval process (no shadow
evidence, no change budget, no operator token). Those routes were removed; every change goes through WP14.

## 1. The change to `dashboard/app.py` (6 lines added, nothing removed)

```diff
@@ nav @@
     <a href="/paper-loop/metrics" id="n-paperloop">Paper Loop</a>
+    <a href="/template-controls" id="n-templates">Templates</a>
@@ end of file @@
 from dashboard.paper_metrics_view import bp as _d3_paper_metrics_bp  # noqa: E402
 app.register_blueprint(_d3_paper_metrics_bp)

+# M21 E6 — read-only view of the paper-lane template configuration (GET only, auth-required;
+# 503 unless TEMPLATE_CONTROLS_DB_PATH is set). Changes go through WP14 promotion, never this page.
+from dashboard.template_controls_view import bp as _e6_template_controls_bp  # noqa: E402
+app.register_blueprint(_e6_template_controls_bp)
```

Resulting file: git blob `63d3420169ea8487d03b4a16f5d26b0ccca46bef`, sha256
`64f31599c265ec0f8666016962fa0392ee4bd277d16c155b3769ad2a82ecb66d`.

**Behaviour after the change, with no further configuration:** the nav shows "Templates"; both routes answer 401
without a dashboard session and **503 `not_configured`** with one. Nothing is read until
`TEMPLATE_CONTROLS_DB_PATH` is set, and then only with a read-only connection. There is no write route. Live
routing is unaffected (Template 1 only). No restart is part of this change; it takes effect at the next dashboard
restart, which is itself an operational approval.

## 2. The governance records that must land with it (patch 2/2)

| file | change |
|---|---|
| `_protected_baselines.py` | `dashboard/app.py`: `to_blob` → `63d3420…`, `commit` → the commit that applied patch 1/2, approval text records D3b then E6 |
| `test_m17_backtesting.py` | `_PROTECTED_APPROVED_SHA256["dashboard/app.py"]` → `64f31599…` |
| `test_m21_c5b_protected_baselines.py` | the exact added code lines from the frozen blob now include the 3 E6 lines |
| `test_m21_1_e6_template_controls_view.py` | "not registered" test replaced by "registered exactly once" + a real-app test: 401 without a session, 503 with one, no write route |

## 3. How to apply (after the 16 PRs and PR E6 are merged; merge commits only)

```bash
git switch -c m21/e6-app-wiring origin/main
git am docs/m21/E6_dashboard_app_wiring.patch          # two commits; no conflicts expected
APP=$(git rev-parse HEAD~1)                            # the commit that changed dashboard/app.py
sed -i "s/13bc47ca733ad843fd35f6c1b05c9c0122aa4eef/$APP/" _protected_baselines.py
git commit -qam "M21 E6: point the approved dashboard/app.py transition at $APP"
```

The placeholder `13bc47c…` is the commit used for verification; the `sed` makes the record point at the real
commit (`test_approving_commit_is_ancestor_and_produced_to_blob` fails until it does: fail-closed).

## 4. Verification (30 Oct, scratch only)

- The patch applies with `git am` to the read-only E6 head `46c2ce4` (and to later heads that only change
  `docs/m21/`); the §3 placeholder step was then run as written.
- Without patch 2/2 the protected guards fail, as they must (`test_m17` sha256 pin, the M20 runtime guards,
  `test_m21_c5b`, the E6 isolation test).
- With both: the protected guard set result is in the PR conversation.
- Earlier findings kept: a test importing `dashboard.app` installs `_test_isolation` first (C5a); the WP14 P6
  guard records this view as a read-only reader of the controls store.

Python 3.12.3 / NumPy 1.26.4, C5a isolation, checkout outside `/tmp`.
