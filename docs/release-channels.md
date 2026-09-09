# Release channels

Fenris ships through four channels. Every channel builds from the same
tagged commit, and every release passes the same gate first: the full
offline suite plus ruff must be green on that exact commit (see
[.github/workflows/release.yml](.github/workflows/release.yml)). No
channel skips the gate.

| Channel | Artifact | Who it's for | Cadence |
| :--- | :--- | :--- | :--- |
| **GitHub Releases** | Tauri desktop bundles (.dmg / .msi / AppImage/.deb), for all three platforms | Non-technical users; the double-clickable product | on tag |
| **PyPI (`fenris-drive`)** | Wheel + sdist (the CLI and engine) | Terminal users; `pipx install fenris-drive` | on tag |
| **git tags** | annotated `vX.Y.Z` tags on master | Packagers, reproducibility, archaeology | on tag |
| **branch `master`** | source | developers | continuous |

## Versioning

Semver, with one project-specific reading: the **answer contract is part
of the API**. Anything that changes what a user can trust — the six
correctness invariants in [CONTRIBUTING.md](CONTRIBUTING.md), the
answer-state ladder, citation rendering — bumps at least the minor, even
if not a line of Python code changed shape.

- `X.0.0` — an answer-contract change (new state, changed enforcement)
- `X.Y.0` — feature, connector, CLI/API addition
- `X.Y.Z` — fixes only, including security patches

The Python package, the desktop app, and the git tag carry the same
version number, always. `fenris version` prints it everywhere.

## The release procedure

1. `CHANGELOG.md` gets a `[Unreleased] -> X.Y.Z` entry merged first.
2. The release commit is tagged `vX.Y.Z` (annotated, signed if possible)
   and pushed. The tag is the trigger; CI builds everything from it.
3. The `Release` workflow runs the gate, then:
   - builds wheel + sdist, checks them with twine, publishes to PyPI
     under `fenris-drive` via trusted publishing;
   - builds the engine sidecar (PyInstaller onedir) on each OS, then the
     Tauri bundle for macOS (arm64 + x64), Linux, and Windows;
   - opens a **draft** GitHub release with all artifacts attached.
4. A maintainer reviews the draft — asset presence per platform, the
     twine check, the CHANGELOG section — then publishes it.
5. Security patches: publish the fix release, then credit the reporter
   and update [SECURITY.md](SECURITY.md) / the disclosure doc.

A release is never published straight from `workflow_dispatch` input;
dispatch exists for channel repairs (re-upload a missing artifact), and
it passes through the same gate.

## What is deliberately not a channel

- **Bleeding-edge binaries.** No nightly channel: index-format changes
  land rarely, and a nightly would accustom users to trusting unreviewed
  builds.
- **Auto-updates.** The desktop bundles carry Tauri's updater plugin
  wiring, but update keys are not yet published; until they are, the
  GitHub Releases page is the updater. When signing keys land, this doc
  changes first.
- **Model weights.** `fenris models fetch` is an explicit user action
  with pinned sha256s; no release ever bundles or auto-downloads
  weights.

## PyPI name

The package name is **`fenris-drive`** (PyPI's `fenris` belongs to an
unrelated project). The CLI, the module, and the desktop app are all
still just `fenris`.
