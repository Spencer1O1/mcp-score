# Contributing to mcp-score

Thanks for your interest in contributing! This guide covers everything you need to get started.

## Prerequisites

- **Python 3.14+**
- **[Devbox](https://www.jetify.com/devbox)** (recommended) or manually install `uv`, `ruff`, `pyright`
- **MuseScore Studio 4** — only for `render_score`, the live plugin (which needs 4.4.2 or later) and the integration tests. Everything else runs without it

## Development setup

```bash
git clone https://github.com/Spencer1O1/mcp-score.git
cd mcp-score

# Option A: devbox + direnv (recommended)
direnv allow

# Option B: devbox shell
devbox shell

# Option C: manual (if not using devbox)
uv venv --python python3.14
source .venv/bin/activate
uv sync
```

The devbox shell automatically creates a virtualenv, installs dependencies, and configures git hooks.

## Running checks

All checks must pass before merging:

```bash
devbox run check    # runs all of the below
devbox run lint     # ruff check .
devbox run format   # ruff format .
devbox run typecheck  # pyright src/
devbox run test     # pytest
```

Or directly (inside devbox shell):

```bash
ruff check .
ruff format --check .
pyright src/
pytest
```

Markdown is formatted with Prettier; CI checks it, so run `npx prettier --write "**/*.md"` on the files you touch.

`docs/reference.md` is generated from the tool docstrings, input schemas and
the CLI parser; a test fails when it drifts. After changing a tool, run
`uv run scripts/generate_reference.py` and commit the result. Tool docstrings
are what MCP clients see, so write the reference there.

The separate delivery check `uv run python scripts/smoke_wheel.py` builds and
installs a wheel into a temporary environment, verifies bundled resources and
tool schemas without source imports, and copies the plugin only into an explicit
temporary directory. It may download build/runtime dependencies; offline pytest
does not run it. CI has a dedicated wheel-smoke job. See
[fork provenance and installation](docs/fork-installation.md).

## Code style

Follow the code standards in [CONVENTIONS.md](CONVENTIONS.md) — code quality,
testing, commit conventions, and Python-specific rules are all defined there.

**Devbox commit gotcha:** When committing through devbox (`devbox run -- git
commit`), write multi-line messages to a temp file and use `git commit -F
/tmp/msg.txt` instead of `-m` with a HEREDOC — devbox can produce literal `\n`
otherwise.

## Tests

```bash
pytest                    # run all tests
pytest tests/test_cli.py  # run specific test file
pytest -k "test_install"  # run tests matching pattern
```

### Integration tests

`tests/integration/` runs the real thing against MuseScore Studio: the
headless `render_score` export and the live plugin bridge. The tests are
marked `integration` and skip unless `MCP_SCORE_INTEGRATION=1` is set, so the
plain `pytest` run stays fast and offline. CI runs them in the `Integration`
workflow: on Linux against the versions in `tests/integration/musescore-versions.json` (the oldest supported line, one in between and the newest), and on Windows and macOS against the newest.

`scripts/musescore_harness.py` installs MuseScore and drives it on all three
platforms (Linux needs `xvfb`, `xdotool` and the Qt runtime libraries listed
in `.github/workflows/integration.yml`):

```bash
# 1. Download MuseScore into the cache (prints the executable)
export MCP_SCORE_MUSESCORE_PATH="$(uv run scripts/musescore_harness.py install)"

# 2. Headless export tests (on Linux, MuseScore needs an X display even for export)
MCP_SCORE_INTEGRATION=1 xvfb-run -a pytest tests/integration/test_musescore_render.py

# 3. Live bridge tests: start MuseScore with the plugin listening on :8765
uv run scripts/musescore_harness.py start tests/integration/fixtures/fixture.musicxml
MCP_SCORE_INTEGRATION=1 pytest tests/integration/test_musescore_bridge.py
uv run scripts/musescore_harness.py stop
```

On Linux and Windows, `start` uses a marked private profile under the harness
cache. Linux redirects only the child process's XDG directories. Windows extracts
the official portable build into that profile and launches its executable
directly; it does not install an MSI or run the PortableApps launcher. `HOME` is
unchanged. Document, plugin, and temporary paths are private as well.

Local/default macOS startup refuses before profile writes. Native CFPreferences
isolation could not be demonstrated on the supported macOS runner, even with
CoreFoundation home and preference-daemon overrides. The macOS CI job therefore
uses the **whole disposable GitHub-hosted runner account** as its boundary. This
requires `MUSESCORE_MACOS_RUNNER_ISOLATION=1`, hosted macOS Actions metadata, and
consistent runner account/home/temp paths; local and self-hosted environments
are rejected. These checks protect against accidental use, not forged metadata.
This mode intentionally changes the disposable account's native MuseScore
preferences and data; it makes no per-process profile-preservation claim.

The macOS account paths remain separate from the marked artifact root. Custom
artifact roots that overlap native profile paths in either direction, including
resolved symlink aliases and Library ancestors, are refused before adoption or
cleanup even if a valid marker is present. Before
each fixture launch, the exact native MuseScore `session` directory is atomically
renamed into a unique backup inside that root. This prevents session recovery
from overriding the fixture. Symlinked sources are refused; failed moves leave
the source intact, with no copy/delete fallback. `clean` removes only marked
artifacts and session backups, never the account home or native data tree.
Remaining native preferences/data are left for hosted-runner disposal.

Before configuration writes, startup refuses an existing MuseScore process or
occupied bridge/display endpoint. The process record stores launched PIDs,
creation times, and executable identities, including application descendants and
Xvfb. `stop` terminates only identities that still match that record; it never
kills applications by name or removes another display's socket. Stale PID records
do not authorize signaling a reused PID. Exited/zombie descendants without an
executable identity are omitted during live discovery; persisted ownership
records still require complete identities. Root, marker, state, preferences,
lock, and plugin control links remain refused. MuseScore's passive built-in
style links below a real `myStyles` directory may point into its app bundle;
cleanup unlinks those assets without following targets. Log collection skips
all symlink roots and descendants. The profile and desktop must use English
menu labels.

The passage-fixture CI phase also exercises native UI selection reads. Only
that startup step opts into `MUSESCORE_SELECTION_TEST_SETUP=1`, which adds a fixed
selection setup driver to the installed plugin **copy** under the marked owned
profile. It can clear or select fixture notes/ranges and observe native state;
it cannot evaluate code. The source/package plugin never includes its dispatch
command, and the next startup reinstalls the production plugin. Missing or
ambiguous insertion points and linked/unowned destinations are refused. The
normal bridge phase verifies that the driver is absent. Native dirty/viewport
and undo-stack inspection are not exposed by the supported wrappers; selection
preservation is checked directly and undo preservation through a disposable
edit/read/undo round trip.

The guarded-pitch restart phase separately opts into
`MUSESCORE_PITCH_BATCH_TEST_SETUP=1`. Under the marked owned profile only, it
installs fixed arm/read commands and a one-shot fault into the plugin copy. The
fault records primitive pitch/TPC values after the first real native write and
throws before the second, proving full target restoration and preservation of
a prior fixture undo entry. No arbitrary code or fault parameters are accepted.
All unique insertion points and unlinked owned paths are checked before writes.
Source/package files remain unchanged, the normal handshake lacks these test
commands, and the next startup restores production files. Cleanup never sends
an undo after an uncertain, failed or previously attempted undo.

The same restarted pitch phase can separately opt into
`MUSESCORE_CHORD_TONE_TEST_SETUP=1`. Fixed no-argument add/remove arms and a
primitive-state read command are installed only in the marked owned copy.
Unique post-add/remove insertion points record actual native chord cardinality
and pitches, then throw once after a demonstrated structural change. The test
requires full target/passage restoration and preservation of its prior own
sentinel undo entry. Every QML/module sentinel and unlinked owned destination is
validated before fault writes. Source/package code stays unchanged; normal
handshakes lack the test commands and the next startup restores production.
No arbitrary code/fault parameters or production hooks exist, and failure or
uncertainty never authorizes a cleanup undo.

`stop` retains the marked artifacts and logs for inspection. Collect them before
removing the profile:

Startup also writes a bounded `startup-diagnostics.json` in the marked artifact
root. It records the latest fixture's phases, UTC/elapsed times, recorded process
identities, and each launch attempt's status/exit code. A failed attempt's bounded
stdout tail is retained before the next attempt overwrites stdout. Final failure
evidence is collected before owned-process cleanup; diagnostic or cleanup errors
are reported without replacing the original startup error. Startup/retry policy
is unchanged.

Only verified hosted macOS mode can additionally query the owned application's
bounded Accessibility window/sheet titles, roles and modal flags. The fixed helper
rechecks PID/creation-time/executable ownership and every UI element's PID. It
does not activate windows, perform actions, inspect the global desktop, capture
screenshots, request permissions, or change preferences. Permission denial,
unavailable metadata, timeouts and invalid replies are recorded explicitly.
OS-owned crash dialogs are outside this application's ownership boundary, and
captured window metadata does not prove which dialog caused a startup failure.
The native CI proof reuses the first fixture, checks contents/selection/cursor
and a confirmed own undo sentinel, and accepts explicit permission denial without
claiming that window capture succeeded. Default/local macOS still refuses before
profile writes. Log collection applies the same no-follow rules to this report.

```bash
uv run scripts/musescore_harness.py collect-logs musescore-logs
uv run scripts/musescore_harness.py clean
```

Linux and Windows CI snapshot the ordinary application profile after headless
rendering, then assert that live startup, fixture changes, and cleanup leave it
unchanged. Startup requires current application logs and persisted preferences
inside the declared boundary. macOS retains the same real live tests and records
an explicit account-boundary report after cleanup, including expected native
profile mutations, session moves, artifact removal, and no remaining MuseScore
processes. It does not assert ordinary-profile preservation in whole-runner mode.

The harness reads these environment variables:

| Variable                           | Meaning                                                                                                                                |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `MUSESCORE_MACOS_RUNNER_ISOLATION` | macOS CI only: explicit `1` opt-in to the verified disposable GitHub-hosted runner account boundary; local/self-hosted use is refused. |
| `MUSESCORE_VERSION`                | Release to install; defaults to the newest in `tests/integration/musescore-versions.json`                                              |
| `MUSESCORE_PROFILE_DIR`            | Optional private test-profile directory; an existing nonempty unowned directory is refused.                                            |
| `MUSESCORE_DOWNLOAD_URL`           | Asset to download; otherwise the release is looked up through the GitHub releases API                                                  |
| `GITHUB_TOKEN`                     | Raises the GitHub API rate limit for that lookup                                                                                       |
| `MUSESCORE_CACHE_DIR`              | Where downloads and the unpacked application live; defaults to `~/.cache/mcp-score/musescore-<version>`                                |
| `MUSESCORE_DISPLAY`                | Linux only: the Xvfb display the GUI runs on, default `:87` (not `:99`, which `xvfb-run` takes for the render tests)                   |

## PR process

All contributions go through pull requests — **do not push directly to `main`**.
Keep each PR to one change; small PRs get reviewed and merged faster.

1. Fork or create a feature branch from `main`
2. Make your changes
3. Ensure `devbox run check` passes
4. Push and create a pull request
5. CI must pass: lint, format, typecheck, tests, Markdown formatting (Prettier)
   and the integration tests against real MuseScore
6. A maintainer will review your PR — address any feedback
7. The maintainer squash-merges once approved

Dependent pull requests must remain drafts while targeting a feature branch.
After the parent merges, retarget the dependent PR to `main`, check its diff and
mergeability, and verify independent review and CI for its current head before
marking it ready. Merging into a feature branch does not deliver that change to
`main`, even when the feature branch was previously merged.

Much of this project is written with Claude Code, and that is fine for
contributions too. Every PR is reviewed by the maintainer before merge, so
say what you tested and how.

Releases follow [semver](https://semver.org/); versions stay 0.x until the
tool set is stable, so minor releases may still change tool names and
parameters.

The existing restarted-pitch and both note/rest phases can additionally set
`MUSESCORE_LYRIC_TEST_SETUP=1` and `MCP_SCORE_LYRIC_FIXTURE=pitch`, `normal` or
`concert` for the matching test invocation. The fixed owned-copy driver observes
native XML/plain text immediately after the real TEXT write and fails once to
prove cancellation plus prior-history preservation. It accepts no code or fault
parameters; paths/ownership/sentinels are checked before writes, the packaged
plugin lacks its commands, and normal reseeding removes it. No uncertain failure
or undo response authorizes cleanup/retry undo.

The guarded note/rest phases start their own `guarded-note-rest.musicxml` and
`guarded-note-rest-concert.musicxml` fixtures with
`MUSESCORE_NOTE_REST_TEST_SETUP=1`. Only their marked installed plugin copies
receive fixed post-rewrite cancellation probes; normal seeding restores the
packaged source. `MCP_SCORE_NOTE_REST_FIXTURE=normal` or `concert` opts into the
matching integration test phase. Never opt these phases into a user score or
normal application profile. Native versions below the supported write API
actively verify refusal before arming any failure probe.

### Cutting a release

Release notes live on the [Releases page](https://github.com/tskovlund/mcp-score/releases);
there is no changelog file. Conventional commit messages are the raw material:
with squash merges, every commit on `main` is one typed line, and the release
workflow turns them into a draft.

1. Open a release PR that bumps `version` in `pyproject.toml`. Merge it.
2. Tag the merge commit and push the tag: `git tag -a vX.Y.Z -m "mcp-score-server X.Y.Z"`
   then `git push origin vX.Y.Z`. Tags are signed and protected.
3. The `Release` workflow builds the package, publishes it to PyPI through the
   `pypi` environment (approve the deployment when asked) and creates a
   **draft** GitHub release with the build artifacts and notes drafted by
   [git-cliff](https://git-cliff.org/) from the commits since the previous tag,
   grouped by type (`cliff.toml`).
4. Curate the draft into authored release notes and publish it. Published
   releases are immutable, so review before publishing.

### Prompt request PRs

We welcome **prompt request PRs** — pull requests that contain a well-described
specification instead of code. These are designed to be implemented by AI agents.

A prompt request PR should include:

- **Clear problem statement** — what should change and why
- **Acceptance criteria** — specific, testable outcomes
- **Constraints** — any boundaries on the solution (e.g. "must not break existing
  API", "should use music21's built-in transposition")
- **Examples** — input/output examples or before/after descriptions where helpful

Label prompt request PRs with `prompt-request`. A maintainer or AI agent will
pick them up, implement the changes, and push commits to the PR branch. The
normal review process applies after implementation.

## Project structure

The package layout, with a line per module, is in [docs/architecture.md](docs/architecture.md#package-structure). Tests live in `tests/`, grouped by concern; `tests/integration/` holds the opt-in tests against a real MuseScore.

## Skill development

The `score-generate` skill (`.claude/skills/score-generate/SKILL.md`) teaches Claude to write music21 Python scripts. To modify it:

1. Edit `SKILL.md` or files in `references/`
2. Test by running `mcp-score install-skill` and asking Claude Code to generate a score
3. Common changes: adding instrument examples, fixing music21 API gotchas, updating the template

The skill is bundled with the pip package so users can install it with `mcp-score install-skill`. The `score_generation_guide` tool and the `score-generate` MCP prompt serve the same files to other MCP clients, so a change to the skill reaches every client.

## Architecture decisions

The rationale is in [docs/architecture.md](docs/architecture.md#key-design-decisions); the decision ledger behind the beta is [issue #91](https://github.com/tskovlund/mcp-score/issues/91). Check both before reopening a settled question.
