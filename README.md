# ai-orchestrator

A local orchestrator that coordinates **Claude Code** (`claude -p`) and **Codex** (`codex exec`) as subprocess workers in a single automated workflow. No API keys, no SDK integration -- it runs entirely through the CLIs you already have with your existing subscriptions.

`orch` is the primary command. `aio` is available as a compatibility alias.

## How it works

You describe a task. The orchestrator drives it through an automated pipeline:

```
SCOPING ─► PLANNING ─► APPROVAL ─► FEASIBILITY ─► EXECUTING ─► REVIEWING
               ▲                                                    │
               └─── replan ◄── ADJUDICATING ◄───────────────────────┘
                                    │
                                    ▼ (pass)
                                   MERGING ─► DONE
```

1. **Scoping** (Claude) -- classifies task complexity, normalizes the prompt
2. **Planning** (Claude) -- generates a step-by-step implementation plan
3. **Plan approval** -- you review and approve or request changes
4. **Feasibility** (Codex/GPT) -- validates the plan is executable in your repo
5. **Execution** (Codex/GPT) -- implements each step in an isolated git worktree, or in-place across a workspace
6. **Review** (Claude Opus) -- reviews the implementation for correctness and security
7. **Adjudication** (Codex/GPT) -- pushes back on or accepts review findings
8. **Fix loop** -- if issues found, plans and implements fixes, then re-reviews
9. **Handoff** -- applies the final diff without committing and prints the suggested git commands

Claude and GPT check each other's work at every stage. Model effort (low/medium/high/max) is automatically selected based on task complexity.

The review phase can also inject non-AI evidence:

- heuristic findings from the bundled AI-gotcha scanner on changed files
- repo-aware review context from `.ai-review/config.json` when installed

## Requirements

- Python 3.11+
- Git 2.20+ (with worktree support)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- [Codex CLI](https://github.com/openai/codex) installed and authenticated

## Install

### New machine setup (macOS)

On a brand new Mac, install the prerequisites in this order:

```bash
# 1. Install Homebrew if needed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Install required tools
brew install git python@3.11 pipx claude codex

# 3. Enable pipx in your shell
pipx ensurepath
exec zsh -l

# 4. Verify the basics
git --version
python3.11 --version
pipx --version
```

Then install and authenticate the two AI CLIs using their normal vendor setup flows, and verify:

```bash
claude login
codex login
```

After that, install ai-orchestrator:

```bash
git clone https://github.com/hikkgomes/ai-orchestrator.git
cd ai-orchestrator
scripts/install.sh   # Unix wrapper: installs from this checkout, writes update metadata, runs orch doctor
```

To update the CLI later from any directory:

```bash
orch self-update
```

### From PyPI (recommended)

```bash
python3.11 -m pip install --user pipx
pipx ensurepath
exec zsh -l
pipx install ai-orchestrator
orch doctor
```

### From source (development)

```bash
git clone https://github.com/hikkgomes/ai-orchestrator.git
cd ai-orchestrator
scripts/install.sh --editable   # Unix wrapper: editable install + writes update metadata + orch doctor
```

### Platform bootstrap scripts

```bash
# macOS
scripts/install-macos.sh

# Linux
scripts/install-linux.sh

# Windows (experimental)
pwsh -File scripts/install-windows.ps1
```

Add `--editable` (or `-Editable` on Windows) for a local dev install. The Unix install
scripts write install metadata so `orch self-update` can update and reinstall from anywhere.

### Updating

```bash
# Update the CLI (pull latest source + reinstall) from any directory
orch self-update

# After updating, refresh workspace configs in each project repo you use
cd <project>
orch sync
```

## First-time repo setup

Installing the CLI on your machine makes `orch` available in any terminal, including VS Code.
You still need to initialize each repository or workspace you want to orchestrate.

From the root of the repository or workspace you want to orchestrate:

```bash
orch init           # writes aio.toml, workflows/default.yaml, .gitignore + .ai-review/ setup
orch install-shell  # installs shell integration and `aio` alias
orch doctor         # verifies everything is working
```

If the repo changes significantly, or after running `orch self-update`, refresh
the reviewer context without discarding curated notes or risk paths:

```bash
orch sync
```

## Usage

### Start a run

```bash
orch new "Add a health check endpoint to the API"
```

This launches the full pipeline. In interactive mode, plan approval pauses inline; final handoff does not require approval.

### Skip scoping

If you already know exactly what you want:

```bash
orch new "Fix the off-by-one in pagination" --skip-scoping
```

### Non-interactive mode

```bash
orch run "Refactor auth middleware"
```

Non-interactive mode is the default. The run pauses at approval gates. Use `approve`/`reject` commands to drive it:

```bash
orch approve <run-id> plan
orch reject <run-id> plan --reason "Split step 3 into smaller pieces"
```

If scoping flags the task as not actionable:

```bash
orch reject <run-id> scope --reason "I mean the REST API, not the GraphQL one"
```

### Monitor and manage

```bash
orch status                      # overview of all runs
orch status <run-id> --watch     # live status for a specific run
orch logs <run-id>               # view event log
orch logs <run-id> 3             # view a specific step result
orch resume <run-id>             # resume a paused or blocked run
orch sync                        # refresh .ai-review/ config and rules after an update
orch self-update                 # pull latest source and reinstall the CLI
orch config                      # show effective configuration
orch clean                       # remove completed run artifacts
orch clean --all                 # remove all run artifacts
```

## Using with VS Code

ai-orchestrator works from any terminal, including the VS Code integrated terminal.

### Setup

1. Install prerequisites (Python 3.11+, Git, Claude Code CLI, Codex CLI)
2. Install ai-orchestrator (`pipx install ai-orchestrator`)
3. Open your project in VS Code
4. Open the integrated terminal (`Ctrl+`` ` or `Cmd+`` `)
5. Run `orch init && orch doctor` if this is the first time using the orchestrator in this repo

### Typical workflow in VS Code

```bash
# Start an orchestrated run
orch new "Implement user authentication with JWT"

# The orchestrator will:
#   1. Scope and classify the task
#   2. Generate a plan and pause for your approval
#   3. Run feasibility checks
#   4. Execute the plan in an isolated worktree
#   5. Review and adjudicate the result
#   6. Stage the final diff and print commit/push suggestions

# While the run executes, you can open a second terminal to monitor:
orch status <run-id> --watch

# After handoff, the staged changes land in your working tree.
# VS Code will detect the file changes automatically.
```

### Tips

- Use **split terminals** -- one for running `orch new`, another for `orch status --watch`
- After handoff completes, VS Code's Source Control panel picks up the changes immediately
- The `.ai-orchestrator/` directory is gitignored -- it won't clutter your Source Control view
- You can keep coding in other files while a single-repo run executes; the orchestrator works in an isolated worktree

## Workspace mode

A workspace is a parent directory that is not itself a git repo but contains multiple git repos such as `frontend/` and `backend/`.

- `orch init` auto-detects git subdirectories and writes `[workspace] repos = [...]` into `aio.toml`.
- Agents run from the workspace root and can work across all configured repos in one run.
- Workspace runs do not create worktrees; changes are applied in place.
- At the end of the run, `orch` prints per-repo `git add` / `git commit` / `git push` suggestions instead of committing for you.
- All workspace repos must be clean before execution starts.
- Known limitation: workspace retries reset repo changes back to `HEAD`, so rollback is repo-wide rather than step-local.

## Configuration

Edit `aio.toml` at your repo root. Key settings:

```toml
[routing]
planner = "claude"              # which CLI plans (claude or codex)
worker = "codex"                # which CLI implements
reviewer = "claude"             # which CLI reviews
adjudicator = "codex"           # which CLI adjudicates (cross-model by default)
feasibility_checker = "codex"   # which CLI checks feasibility
scoper = "claude"               # which CLI scopes tasks

[routing.phases.scoping]
reasoning_effort = "high"       # per-phase effort override

[routing.phases.reviewing]
reasoning_effort = "high"

[scoping]
enabled = true                  # set to false to skip scoping globally

[feasibility]
enabled = true
timeout = 120

[approval]
require_plan_approval = true
# require_merge_approval may still appear in older configs but is no longer used.

# Uncomment for multi-repo workspace mode:
# [workspace]
# repos = ["frontend", "backend"]

[orchestrator]
max_retries = 3
max_rework_loops = 3
max_replan_loops = 2
```

Complexity-adaptive effort selection is built in. When scoping classifies a task as `simple`, `moderate`, `complex`, or `architectural`, downstream phases automatically use appropriate effort levels. Per-phase overrides in `[routing.phases.*]` take precedence.

## Reviewer setup

Reviewer heuristics run automatically during the review phase, even if you do
not configure anything else. `orch review-install` adds repo-aware context that
makes reviews more targeted. It creates:

- `.ai-review/config.json` for detected stack, commands, architecture, and risk paths
- `.ai-review/rules.yaml` for the bundled review categories used in the prompt

Use `orch sync` to refresh the detected fields after updating the CLI. This
preserves manually curated notes and critical paths. If you only want to update
the heuristics without pulling new CLI code, run `orch review-analyze` directly.
Once config exists, `orch review-install` requires `--force` to overwrite it.

## Runtime layout

Artifacts are stored under `.ai-orchestrator/` (gitignored):

```
.ai-orchestrator/
  state/           # run state JSON files
  plans/           # plan artifacts
  results/         # step result artifacts
  reviews/         # review artifacts
  adjudications/   # adjudication artifacts
  feasibility/     # feasibility check artifacts
  logs/            # event logs
  worktrees/       # ephemeral git worktrees
  approvals/       # approval decisions
  feedback/        # rejection feedback
  executions/      # execution manifests
  prompts/         # rendered prompts (opt-in)
  metadata.sqlite3 # invocation metadata
```

## Doctor checks

`orch doctor` verifies:

- Python version (3.11+ required)
- Git availability and version
- `claude` CLI availability with auth hint
- `codex` CLI availability with auth hint
- Write access to `.ai-orchestrator/`
- Repo config presence and validity

## Development

```bash
python3 -m pytest
python3 -m pytest --cov=ai_orchestrator --cov-report=term-missing
python3 -m ai_orchestrator.cli --help
```

## Documentation

- [Architecture](docs/architecture.md)
- [Workflow](docs/workflow.md)
- [Design decisions](docs/design-decisions.md)
- [Output contracts](docs/output-contracts.md)
- [Install](docs/install.md)
- [Security](docs/security.md)

## License

MIT
