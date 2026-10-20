# Maintainer release checklist

Suggested repository: `SomaCoder/fusion-quilter-bridge`.
Suggested description:

> A verified, script-first Fusion Electronics / EAGLE to KiCad bridge for Quilter workflows.

Suggested topics: `pcb`, `kicad`, `eagle`, `fusion-360`, `quilter`,
`eda`, `research-software`, `python`.

## Before publishing

- [ ] Confirm ownership and authority to license the original work under MIT.
- [ ] Confirm the lab wordmark is the intended official version, and approve its
      public use. See [branding and licensing](../NOTICE.md).
- [ ] Review the README and [validation evidence](validation.md).
- [ ] Run the public demo and fast tests from a clean checkout.
- [ ] Run `python tools\check_release.py` and inspect every staged path.
- [ ] Enable GitHub secret scanning/push protection if available.
- [ ] Require PR review and passing checks on `main`; disable force pushes.
- [ ] Keep Actions token permissions read-only unless a specific job needs more.

The repository ignores user inputs and generated output, but `.gitignore` is not
a confidentiality boundary. Never force-add a design or log. Only the original
synthetic fixture is intended for distribution.

## Authentication

Copilot sign-in does not establish that Git or GitHub CLI has repository-write
access. Sign in using `gh auth login --web`, the VS Code GitHub sign-in, or your
normal Git credential manager. Do not paste passwords or tokens into an issue,
README, script or chat.

## Create and publish after approval

Run these **inside the prepared repository**, never in a parent research folder.
Git and GitHub CLI must be installed and the account shown by `gh auth status`
must be the account you intend to use. These commands create a **private**
repository for initial review; change its visibility in GitHub after approval.

```powershell
git init -b main
git add .
git diff --cached --stat
git diff --cached
git status --short
# Inspect the entire staged set before committing.
git commit -m "Prepare public research bridge" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
gh auth status
gh repo create SomaCoder/fusion-quilter-bridge --private --source . --remote origin --push
```

Use [social-card.png](assets/social-card.png) for the repository's social preview.
The [presentation page](index.html) is a static local preview with links to the
accompanying source documentation. No analytics, remote fonts, API keys or build
service are required. A GitHub Pages deployment is a separate opt-in step:
adapt the documentation links to the published URLs and approve public visibility
before enabling it. This package does not enable Pages or change repository settings.

The local ZIP, if provided, is built from the explicit source inventory by
`python tools\check_release.py --archive`. The archive omits generated outputs,
logs, local configuration and itself.
