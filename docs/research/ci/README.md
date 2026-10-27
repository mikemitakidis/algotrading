# Research CI: inactive by design (E09)

`research-offline-ci.yml.example` is a ready-to-review GitHub Actions definition that runs every offline research suite and guard on pull requests touching `research/`. It is deliberately **not** in `.github/workflows/`, so it never runs.

Design rules:

- **Least privilege:** `permissions: contents: read`, `persist-credentials: false`, no secrets referenced, and broker variables forced empty.
- **No side effects:** no deployment, no artefact publishing, no PR comments, no scheduled triggers.
- **Pinned production requirements plus research extras.** This also closes an open verification item: the research suites have so far run locally on numpy 2.5 and Python 3.13, while `requirements.txt` pins numpy 1.26.4.

To enable it, the operator approves, then a reviewed PR moves the file to `.github/workflows/research-offline-ci.yml`. Any broader permission (writes, secrets, schedules, self-hosted runners) needs a separate approval.
