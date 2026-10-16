---
name: Release
description: Ship a new version of the rocket CLI — bump every version reference, open the gated release PR, tag main, and publish the GitHub release. Use whenever user-visible changes have merged to main (or are about to ship), or when the latest release is older than main.
---

# Releasing rocket

Nothing here is automatic: every user-visible change must be followed by a version bump and a
GitHub release. The `protect-main` ruleset blocks direct pushes, so all steps go through a PR,
and merges are squash-only. Merging requires the user's explicit approval — never merge a PR
on your own.

## When a release is due

- Any change that affects the installed tool (CLI behavior, commands, generated configs,
  dependencies) → release as soon as it merges. Do not leave `main` ahead of the latest tag.
- Repo-only changes (this skill, CI config, README wording alone) → no release needed.
- Version policy while pre-1.0: features → minor (`0.x.0`); fixes shipped with code → patch
  (`0.x.y`); breaking changes take a minor bump and must be called out in the notes.

## 1. Bump every version reference (all four must stay in sync)

| File | What to update |
| --- | --- |
| `pyproject.toml` | `[project].version` |
| `src/rocket/__init__.py` | `__version__` — drives `rocket --version` and the `rocket init` user agent |
| `uv.lock` | run `uv lock`; the `rocket` package entry carries the version, and CI runs `uv sync --locked` |
| `README.md` | the pinned install example (`git+https://github.com/driftcell/rocket@vX.Y.Z`) and the literal user-agent version in the `rocket init` section |

Placeholder strings in `server.example.toml`, the `config.py` hint and test user agents
intentionally keep old version styles — do not touch them.

## 2. Verify locally (all must pass before the PR)

```bash
uv lock && uv sync --locked --no-progress
uv run ruff format --check . && uv run ruff check . && uv run ty check
uv run pytest -q
uv run python -m rocket --version                 # → rocket X.Y.Z
uv run python -c "from rocket.wizard import INIT_USER_AGENT; print(INIT_USER_AGENT)"
```

## 3. Release PR (required checks `lint` + `test` gate the merge)

```bash
jj describe -m "Release X.Y.Z"
jj bookmark create release-vX.Y.Z -r @
jj git push -b release-vX.Y.Z
gh pr create --base main --head release-vX.Y.Z --title "Release X.Y.Z" --body "…"
gh pr checks <PR> --watch
# STOP here: report the PR and wait for the user's explicit approval before merging
gh pr merge <PR> --squash --delete-branch   # only after the user approves
```

Then sync jj (the local `main` bookmark fast-forwards automatically; the old working-copy
commit is auto-abandoned):

```bash
jj git fetch
jj new main
```

## 4. Tag and publish (annotated tags via git, not jj; source-only releases)

```bash
git rev-parse main                       # confirm it matches the merged release commit
git tag -a vX.Y.Z -m "rocket vX.Y.Z" main
git push origin vX.Y.Z
gh release create vX.Y.Z --title "rocket vX.Y.Z" --latest --notes "…"
```

Match the notes style of the previous release (`gh release view <prev-tag>` for reference):
**Highlights** → **Also in this release** → **Install** (pinned to the new tag) → **Usage** →
a `**Full changelog**: https://github.com/driftcell/rocket/compare/vPREV...vX.Y.Z` line.
Attach no binary assets.

## 5. Verify the release

```bash
gh api repos/driftcell/rocket/releases/latest --jq .tag_name                   # → vX.Y.Z
git rev-parse "vX.Y.Z^{commit}" main                                           # identical hashes
gh run list --workflow ci.yml --limit 2 --json headBranch,status,conclusion    # main: success
uvx --from "git+https://github.com/driftcell/rocket@vX.Y.Z" rocket --version   # → rocket X.Y.Z
```

Finally, tell the user how to upgrade an existing install: `uv tool upgrade rocket` (or
`uv tool install "git+https://github.com/driftcell/rocket@vX.Y.Z" --force`).
