# M21 E6 — the protected `dashboard/app.py` wiring (prepared, NOT applied)

**Status:** prepared 29 Oct 2026. `dashboard/app.py` is a protected runtime file and is **not** changed by PR E6.
This file and `E6_dashboard_app_wiring.patch` are the exact change for your approval.

## 1. The change to `dashboard/app.py` (6 lines added, nothing removed)

```diff
@@ nav @@
     <a href="/paper-loop/metrics" id="n-paperloop">Paper Loop</a>
+    <a href="/template-controls" id="n-templates">Templates</a>
@@ end of file @@
 from dashboard.paper_metrics_view import bp as _d3_paper_metrics_bp  # noqa: E402
 app.register_blueprint(_d3_paper_metrics_bp)

+# M21 E6 — paper-lane template controls (blueprint: auth-required, CSRF on every POST;
+# 503 unless TEMPLATE_CONTROLS_DB_PATH is set, read-only unless TEMPLATE_CONTROLS_WRITES_ENABLED=1).
+from dashboard.template_controls_view import bp as _e6_template_controls_bp  # noqa: E402
+app.register_blueprint(_e6_template_controls_bp)
```

Resulting file: git blob `4bb4e525f7220001f25ab6aa7138fa9c5cacc9ce`, sha256
`7335da7c56c603e6881aef0e2e7ecdaca4d59a1f7d809027678f71e12a864ad7`.

**Behaviour after the change, with no further configuration:** the nav shows "Templates"; every template-controls
route answers 401 without a dashboard session and **503 `not_configured`** with one. Nothing reads or writes a
store until `TEMPLATE_CONTROLS_DB_PATH` is set, and every POST answers **403 `read_only`** until
`TEMPLATE_CONTROLS_WRITES_ENABLED=1`. Live routing is unaffected (Template 1 only). No restart is part of this
change; it takes effect at the next dashboard restart, which is itself an operational approval (OP3).

## 2. The governance records that must land with it (patch 2/2)

The protected-file guards fail on any unapproved change, so the approval is recorded in review:

| file | change |
|---|---|
| `_protected_baselines.py` | `dashboard/app.py`: `to_blob` → `4bb4e525…`, `commit` → the commit that applied patch 1/2, approval text records D3b then E6 |
| `test_m17_backtesting.py` | `_PROTECTED_APPROVED_SHA256["dashboard/app.py"]` → `7335da7c…` |
| `test_m21_c5b_protected_baselines.py` | the exact added code lines from the frozen blob now include the 3 E6 lines |
| `test_m21_1_e6_template_controls_view.py` | "not registered" test replaced by "registered exactly once" + a real-app test: 401 without a session, 503 with one (OFF by default) |

## 3. How to apply (after the 16 PRs and PR E6 are merged; merge commits only)

```bash
git switch -c m21/e6-app-wiring origin/main
git am docs/m21/E6_dashboard_app_wiring.patch          # two commits; no conflicts expected
APP=$(git rev-parse HEAD~1)                            # the commit that changed dashboard/app.py
sed -i "s/b099087ba3f67dfc09b01406d70a030f8cec1d27/$APP/" _protected_baselines.py
git commit -qam "M21 E6: point the approved dashboard/app.py transition at $APP"
```

The placeholder `b099087…` is the commit used for verification; the `sed` makes the record point at the real
commit (`test_approving_commit_is_ancestor_and_produced_to_blob` fails until it does: fail-closed).

## 4. Verification done (29 Oct, on the PR E6 head + the patch, scratch only)

- The patch applies with `git am` to PR E6 head `d417513` (and to any later head that only adds `docs/m21/` files) and produces exactly the verified tree `09a8135`.
- Without patch 2/2, the guards fail as they must (`test_m17` sha256 pin, `test_m20_paper`, `test_m20_paper_storage`,
  `test_m21_c5b`, and the E6 isolation test): the protection works.
- With both patches: the protected guard set passes (evidence in the PR conversation).
- The C5a static isolation guard found that a test importing `dashboard.app` must install `_test_isolation`
  first; the E6 test now does so (PR head `d417513`), so the patch needs no further change.

Python 3.12.3 / NumPy 1.26.4, C5a isolation, checkout outside `/tmp`.
