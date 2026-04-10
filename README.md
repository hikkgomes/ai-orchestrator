# ai-orchestrator

ai-orchestrator runs Claude Code (`claude -p`) and Codex (`codex exec`) as local subprocess workers to scope, plan, implement, review, and hand off code changes from your terminal.

`orch` is the main command. `aio` is available as a compatibility alias.

## Requirements

- Python 3.11+
- Git 2.20+
- Node.js 18+ for installing the vendor CLIs
- Claude Code installed and authenticated
- Codex CLI installed and authenticated

Official setup references:

- [Claude Code setup](https://docs.anthropic.com/en/docs/claude-code/getting-started)
- [Codex CLI getting started](https://help.openai.com/en/articles/11096431-openai-codex-ci-getting-started)
- [Codex CLI sign-in with ChatGPT](https://help.openai.com/en/articles/11381614-codex-cli-and-sign-in-withgpt)

## Quick Start

### New machine setup (macOS)

Install the base tools first:

```bash
brew install git python@3.11 pipx node
pipx ensurepath
exec zsh -l
```

Install and authenticate the vendor CLIs:

```bash
npm install -g @anthropic-ai/claude-code
npm install -g @openai/codex

claude login
codex --login
```

### Install ai-orchestrator

Choose one install mode. If you just want to use the tool, start with PyPI. If you want `orch update` to pull directly from a local clone of this repo, use Source checkout.

**PyPI**: simplest if you just want to use the tool.

```bash
pipx install ai-orchestrator
orch doctor
```

**Source checkout**: best if you want the latest repo changes and `orch update` to pull from this clone.

```bash
git clone https://github.com/hikkgomes/ai-orchestrator.git
cd ai-orchestrator
scripts/install.sh
```

**Editable**: best if you are developing `ai-orchestrator` itself.

```bash
git clone https://github.com/hikkgomes/ai-orchestrator.git
cd ai-orchestrator
scripts/install.sh --editable
```

The Unix install scripts run `orch doctor` at the end and write install metadata used by `orch update`.

Source installs on Unix use the platform wrapper:

- macOS: `scripts/install-macos.sh`
- Linux: `scripts/install-linux.sh`
- Windows: `pwsh -File scripts/install-windows.ps1`

### Set up a project repo

Installing the CLI makes `orch` available on your machine. You still need to initialize each repository where you want to use it.

```bash
cd /path/to/project
orch init
orch doctor
```

Optional shell integration:

```bash
orch install-shell
```

### Update later

Update the CLI itself:

```bash
orch update
```

Then refresh each project repo you use:

```bash
cd /path/to/project
orch sync
orch doctor
```

If you want to refresh scaffolded repo files such as `aio.toml` or `workflows/default.yaml`, run:

```bash
orch init --force
```

Use `--force` carefully. It rewrites scaffolded files in the current repo.

## What `update`, `init`, and `sync` mean

- `orch update`: updates the CLI installed on your machine. It uses the current install mode automatically.
- `orch init`: bootstraps the current repo. It creates or updates `aio.toml`, `workflows/default.yaml`, `.gitignore`, and `.ai-review/`.
- `orch sync`: refreshes `.ai-review/config.json` and `.ai-review/rules.yaml` in the current repo. It does not rewrite `aio.toml` or `workflows/default.yaml`.
- `orch install-shell`: installs shell completion and the `aio` alias.

## Daily Use

Start a run:

```bash
orch new "Add a health check endpoint to the API"
```

`orch new` and `orch run` drive approval gates inline by default. Use `--detach`
or `--no-interactive` if you want the command to return at the next pause.

Skip scoping if the task is already precise:

```bash
orch new "Fix the off-by-one in pagination" --skip-scoping
```

Common commands:

```bash
orch status
orch status latest
orch status <run-id> --watch
orch logs <run-id>
orch logs <run-id> 3
orch show latest plan
orch approve latest plan
orch approve <run-prefix> plan
orch reject <run-id> plan --reason "Split step 3 into smaller pieces"
orch reject <run-id> scope --reason "I mean the REST API, not the GraphQL one"
orch resume <run-id>
orch sync
orch update
orch config
orch clean
orch clean --all
```

`orch run` is also available and starts a new run like `orch new`.

## How It Works

`orch` drives work through this pipeline:

```
SCOPING -> PLANNING -> APPROVAL -> FEASIBILITY -> EXECUTING -> REVIEWING
             ^                                                  |
             |                                                  |
             +--------------- replan <- ADJUDICATING <----------+
                                         |
                                         v
                                       MERGING -> DONE
```

Phase summary:

1. Scoping: Claude normalizes the task and assigns a complexity tier.
2. Planning: Claude generates a step-by-step plan.
3. Plan approval: you approve or reject the plan.
4. Feasibility: Codex or Claude checks that the plan is executable in your repo.
5. Execution: Codex or Claude implements each step.
6. Review: Claude reviews the resulting diff.
7. Adjudication: Codex or Claude decides whether to pass, rework, or replan.
8. Handoff: `orch` stages the final diff and prints suggested git commands. It does not auto-commit for you.

The review phase can also use non-model evidence:

- heuristic findings from the bundled reviewer scanner
- repo-aware context from `.ai-review/config.json`

## VS Code

`orch` works fine in the VS Code integrated terminal.

Typical flow:

```bash
# Terminal 1
orch new "Implement user authentication with JWT"

# Terminal 2
orch status <run-id> --watch
```

If you prefer the older multi-terminal flow where the first command returns at
approval gates, run `orch new --detach "Implement user authentication with JWT"`.

When the run finishes, the staged changes land in your working tree and VS Code picks them up normally.

## Workspace Mode

A workspace is a parent directory that is not itself a git repo but contains multiple git repos such as `frontend/` and `backend/`.

- `orch init` auto-detects git subdirectories and writes `[workspace] repos = [...]` into `aio.toml`.
- Workspace runs operate from the workspace root and can touch multiple repos in one run.
- Workspace runs do not create worktrees; changes are applied in place.
- At the end of the run, `orch` prints per-repo `git add`, `git commit`, and `git push` suggestions instead of committing for you.
- All workspace repos must be clean before execution starts.

## Configuration

Repo-local configuration lives in `aio.toml`. Use `orch config` to see the effective merged config.

Minimal example:

```toml
[routing]
planner = "claude"
worker = "codex"
reviewer = "claude"
adjudicator = "codex"
feasibility_checker = "codex"
scoper = "claude"

[routing.phases.planning]
reasoning_effort = "medium"
max_turns = 5

[scoping]
enabled = true

[feasibility]
enabled = true

[orchestrator]
max_retries = 3
max_rework_loops = 3
max_replan_loops = 2
watchdog_timeout = 3600
```

Notes:

- Complexity-based effort selection is built in.
- Per-phase overrides in `[routing.phases.*]` win over complexity defaults.
- `max_turns` is a phase-level override for Claude phases that need more than one turn.
- `watchdog_timeout` is a global hung-process safety net, not a per-phase runtime budget.

## Repo Files and Runtime Artifacts

`orch init` creates or updates:

- `aio.toml`
- `workflows/default.yaml`
- `.ai-review/config.json`
- `.ai-review/rules.yaml`
- `.gitignore` entries for `.ai-orchestrator/`

Runtime artifacts are written under `.ai-orchestrator/` and should stay gitignored:

```text
.ai-orchestrator/
  state/
  plans/
  results/
  reviews/
  adjudications/
  feasibility/
  logs/
  worktrees/
  approvals/
  feedback/
  executions/
  prompts/         # only when prompt retention is enabled
  metadata.sqlite3
```

## Reviewer Setup

Reviewer heuristics run automatically during the review phase, even without extra setup. The repo-local reviewer files make those reviews much more specific.

- `.ai-review/config.json` stores detected stack, commands, architecture, and risk paths.
- `.ai-review/rules.yaml` stores the bundled review categories used by the prompt.

Use `orch sync` when you want to refresh those files while keeping curated notes and critical paths.

If you only want to refresh the reviewer heuristics without running the broader repo setup flow, `orch review-analyze` is still available. `orch review-install --force` overwrites the reviewer config from scratch.

## Doctor Checks

`orch doctor` verifies:

- Python version
- Git availability and version
- Claude CLI availability
- Codex CLI availability
- Write access to `.ai-orchestrator/`
- Repo config presence and validity

## Development

```bash
python3 -m pytest
python3 -m pytest --cov=ai_orchestrator --cov-report=term-missing
python3 -m ai_orchestrator.cli --help
```

## More Documentation

- [Architecture](docs/architecture.md)
- [Workflow](docs/workflow.md)
- [Design decisions](docs/design-decisions.md)
- [Output contracts](docs/output-contracts.md)
- [Install details](docs/install.md)
- [Security](docs/security.md)

## License

MIT
