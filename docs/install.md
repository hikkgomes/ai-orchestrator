# Install and Distribution

> **Design status: FROZEN** as of 2026-04-08.

## Supported Install Paths

ai-orchestrator supports two primary installation modes:

1. `pipx install ai-orchestrator`
2. Local editable development install with `python -m pip install -e ".[dev]"`

The primary CLI is `orch`. `aio` is installed as a compatibility alias.

## Prerequisites

| Dependency | Minimum | Check |
|---|---|---|
| Python | 3.11 | `python3 --version` |
| Git | 2.20 | `git --version` |
| Claude Code CLI | locally installed | `claude --version` |
| Codex CLI | locally installed | `codex --version` |

Both AI CLIs must already be authenticated outside the orchestrator.

## Recommended Install

### pipx

```bash
pipx install ai-orchestrator
orch doctor
```

### Editable repo install

```bash
git clone https://github.com/<org>/ai-orchestrator.git
cd ai-orchestrator
python3 -m pip install -e ".[dev]"
orch doctor
```

## Bootstrap Scripts

Repository-local install helpers are included for each platform:

```bash
scripts/install-macos.sh
scripts/install-linux.sh
pwsh -File scripts/install-windows.ps1
```

Editable setup from a local checkout:

```bash
scripts/install-macos.sh --editable
scripts/install-linux.sh --editable
pwsh -File scripts/install-windows.ps1 -Editable
```

The Unix launcher `scripts/install.sh` dispatches to the macOS or Linux script automatically.

## Repo Bootstrap

After installation, initialize a repository root with:

```bash
orch init
orch install-shell
orch doctor
```

`orch init` creates:

- `aio.toml`
- `workflows/default.yaml`
- `.gitignore` entries for `.ai-orchestrator/`

`orch install-shell` installs a small shell integration file and an `aio` alias for `orch`.

## Doctor Behavior

`orch doctor` checks:

- Python version
- git availability
- `claude` availability and an auth refresh hint
- `codex` availability and an auth refresh hint
- write permission for `.ai-orchestrator/`
- presence and validity of `aio.toml`
- presence of `workflows/default.yaml`

## Packaging Notes

`pyproject.toml` ships:

- `orch` console entry point
- `aio` compatibility entry point
- `dev` extras for pytest and coverage
- source distribution content for docs, schemas, tests, and install scripts

## Cross-Device Installation Checklist

On a fresh machine:

1. Install Python 3.11+.
2. Install git.
3. Install and authenticate Claude Code CLI.
4. Install and authenticate Codex CLI.
5. Install ai-orchestrator with `pipx` or editable mode.
6. Clone the repository you want to operate on.
7. Run `orch init` if the repo does not already have `aio.toml`.
8. Run `orch doctor`.

## Windows Notes

Windows remains experimental. The PowerShell installer works, but you should expect occasional differences around process termination, path length handling, and worktree cleanup.
