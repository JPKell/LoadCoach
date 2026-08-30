# Lockfiles

Exact, hash-verified pins for this repository's **own** CI and release pipeline, required by
Packaging and Release Standards §4 and Security Standards §11.

| File | Contents | Used by |
|---|---|---|
| `release.in` / `release.lock` | The build and publish chain (`build`, `hatchling`, `twine`) | `release.yml`, and CI's `build` job |
| `ci.lock` | Runtime dependencies plus the `dev` extra | **Not yet present — see below** |

## What these are not

They do **not** define what a consumer installs. `pip install loadcoach` resolves the compatible
ranges in `pyproject.toml`; an application that shipped pinned runtime dependencies would be
un-coinstallable with the rest of the suite. These files exist so that a green build stays green:
without them every CI run re-resolves, and a new `ruff` or `mypy` release can change the result
with no commit to explain it — and `pip-audit` would be auditing today's resolution rather than
what the build actually used.

## `ci.lock` is blocked, and so is every CI job that installs this package

`pyproject.toml` declares `weightsdb>=0.2,<0.3` and `mirrorwall>=0.2,<0.3` as **runtime**
dependencies. Neither package is published:

```console
$ pip index versions weightsdb
ERROR: No matching distribution found for weightsdb
$ pip index versions mirrorwall
ERROR: No matching distribution found for mirrorwall
$ pip install .            # in a fresh, non-root virtualenv
ERROR: Could not find a version that satisfies the requirement mirrorwall<0.3,>=0.2 (from loadcoach)
```

`pip-compile` fails for the same reason (`No matching distribution found for weightsdb<0.3,>=0.2`),
so `ci.lock` cannot be generated, and it must not be faked: a lock whose hashes name artifacts no
index serves would install nowhere.

Locally this repository works because both packages are **editable installs** pointing at
`../py/WeightsDB` and `../py/MirrorWall`. That is exactly the arrangement a lockfile exists to
stop a project from mistaking for a working release.

**What unblocks it:** publishing `weightsdb 0.2.0` and `mirrorwall 0.2.0`, which is the step
handoff entries WDB3 and LCX23 hold open as a human decision. Once both are on PyPI:

```bash
pip install pip-tools
pip-compile --strip-extras --extra dev --generate-hashes \
    --output-file requirements/ci.lock pyproject.toml
```

and then, in `.github/workflows/ci.yml`, replace every `pip install -e ".[dev]"` with

```yaml
      - run: pip install --require-hashes -r requirements/ci.lock
      - run: pip install . --no-deps
```

except in the 3.14 early-warning job, which resolves from ranges on purpose.

## Coverage measures the installed package, not the checkout

`[tool.coverage.run] source` in `pyproject.toml` names the **importable package** (`loadcoach`)
rather than `src/loadcoach`, with a `[tool.coverage.paths]` mapping back to the checkout. A
path-based source reports 0 % against the non-editable install CI will use — the tests all pass,
nothing is measured, and the coverage gate fails with a number that looks like a catastrophe
instead of a configuration error.

## Regenerating

Run after any change to the build chain, and commit the result:

```bash
pip install pip-tools
pip-compile --strip-extras --generate-hashes \
    --output-file requirements/release.lock requirements/release.in
```

`uv pip compile` is the sanctioned alternative (Security Standards §11).

Generated with **pip-tools 7.6.1**. The `--no-index` in the header comment is part of the recorded
command, not part of the resolution: the lock was resolved against PyPI, and re-running the
command above reproduces it byte for byte.

## Interpreter

Resolved on Python 3.13, matching every other repository in the suite. Every pin's
`requires-python` admits 3.12, and no pin is CPython-ABI-specific, so the same lock installs on
both supported versions.
