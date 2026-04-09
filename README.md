# ai-orchestrator

> **Design status: FROZEN** as of 2026-04-08.

A local orchestrator that coordinates Claude Code (`claude -p`) and Codex (`codex exec`) as subprocess workers. It runs entirely through local CLIs: no API keys, no SDK integration, no direct HTTP calls from the orchestrator itself.

`orch` is the primary command. `aio` remains available as a compatibility alias.

## Requirements

- Python 3.11+
- Git 2.20+ with `git worktree`
- Claude Code CLI installed and authenticated
- Codex CLI installed and authenticated

## Install

### PyPI with pipx

```bash
pipx install ai-orchestrator
orch doctor
```

### Local editable development install

```bash
git clone https://github.com/<org>/ai-orchestrator.git
cd ai-orchestrator
python3 -m pip install -e ".[dev]"
orch doctor
```

### Platform bootstrap scripts

```bash
scripts/install-macos.sh
scripts/install-linux.sh
pwsh -File scripts/install-windows.ps1
```

For editable repo installs:

```bash
scripts/install-macos.sh --editable
scripts/install-linux.sh --editable
pwsh -File scripts/install-windows.ps1 -Editable
```

## First-Time Repo Setup

From the repository root:

```bash
orch init
orch install-shell
orch doctor
```

`orch init` writes:

- `aio.toml`
- `workflows/default.yaml`
- `.gitignore` entries for `.ai-orchestrator/`

## Commands

```bash
orch init
orch new "Add a richer doctor view"
orch run "Implement the Windows install script"
orch status
orch status <run-id> --watch
orch approve <run-id> plan
orch reject <run-id> merge --reason "Needs more coverage"
orch resume <run-id>
orch logs <run-id>
orch doctor
orch install-shell
```

## Doctor Checks

`orch doctor` verifies:

- Python version
- git availability
- `claude` availability with an authentication hint
- `codex` availability with an authentication hint
- write access to `.ai-orchestrator/`
- repo config presence and validity

## Development

```bash
python3 -m pytest
python3 -m pytest --cov=ai_orchestrator --cov-report=term-missing
python3 -m ai_orchestrator.cli --help
```

## Runtime Layout

Runtime artifacts are stored under `.ai-orchestrator/`:

- `state/`
- `plans/`
- `results/`
- `reviews/`
- `adjudications/`
- `logs/`
- `worktrees/`
- `approvals/`
- `feedback/`
- `executions/`
- `metadata.sqlite3`

## Cross-Device Setup

On a second machine:

1. Install Python, git, Claude Code CLI, and Codex CLI.
2. Install ai-orchestrator with `pipx install ai-orchestrator` or a local editable checkout.
3. Clone the target repo.
4. Run `orch init` if the repo does not already contain `aio.toml`.
5. Run `orch doctor`.
6. Start a run with `orch new "<task>"`.

## Documentation

- [Architecture](docs/architecture.md)
- [Workflow](docs/workflow.md)
- [Install](docs/install.md)
- [Security](docs/security.md)
- [Output contracts](docs/output-contracts.md)

## License

MIT
