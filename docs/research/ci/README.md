# Research CI: inactive by design (E09)

`research-offline-ci.yml.example` is a ready-to-review GitHub Actions definition that runs all eight offline research suites (foundation, integrity, evidence, mllab, genome, data, agents, six-lane integration) and the guards (M17/M18 G10 hygiene, hygiene suite, quarantine, C5b, C5a) on pull requests touching `research/`. It is deliberately **not** in `.github/workflows/`, so it never runs. `test_m21_research_data` checks that every `test_m21_research_*.py` present in the tree is listed (comprehensive audit, PR #8: the agents and integration suites were missing).

Not yet verified: the example's install step (`pip install -r requirements.txt` plus `scipy lightgbm catboost`) has not been executed on a clean runner. Locally, the suites run under the production pins (Python 3.12.3, NumPy 1.26.4) without LightGBM/CatBoost, whose models are then skipped; that must be checked when E09 activation is approved.

Design rules:

- **Least privilege:** `permissions: contents: read`, `persist-credentials: false`, no secrets referenced, and broker variables forced empty.
- **No side effects:** no deployment, no artefact publishing, no PR comments, no scheduled triggers.
- **Pinned production requirements plus research extras.** This also closes an open verification item: the research suites have so far run locally on numpy 2.5 and Python 3.13, while `requirements.txt` pins numpy 1.26.4.

To enable it, the operator approves, then a reviewed PR moves the file to `.github/workflows/research-offline-ci.yml`. Any broader permission (writes, secrets, schedules, self-hosted runners) needs a separate approval.
