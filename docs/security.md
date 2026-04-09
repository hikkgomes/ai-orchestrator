# Security

> **Design status: FROZEN** as of 2026-04-08.

## Threat Model

ai-orchestrator runs AI CLI tools as subprocesses that read and write to a local git repository. The primary threats are:

1. **Prompt injection via repository content** — malicious file contents could manipulate AI behavior
2. **Arbitrary code execution** — AI-generated code could be harmful
3. **Credential exposure** — prompts or logs could leak secrets
4. **Transcript leakage** — sensitive code and context sent to vendor CLIs and stored locally
5. **State tampering** — corrupted state files could cause unintended behavior
6. **Supply chain** — compromised dependencies
7. **Vendor CLI operational failures** — auth expiry, rate limits, seat restrictions

## First-Order Tradeoff: Context Exposure

This product sends repository file contents to vendor CLIs as prompt context. Those CLIs transmit this content to remote model APIs. This is a fundamental tradeoff:

- **What is sent:** file contents selected by the planner (or explicitly by the user), git diffs, directory structure
- **Where it goes:** to the vendor's API (Anthropic for Claude, OpenAI for Codex) via the vendor CLI
- **What is stored locally:** orchestrator logs may contain the same content (opt-in)
- **What the orchestrator cannot control:** vendor-side data retention, caching, or training policies

Users working with proprietary, confidential, or regulated code must evaluate this tradeoff before using the tool. The orchestrator's secret scanning is best-effort defense in depth, not a guarantee.

## Mitigations

### 1. No API Keys in the Orchestrator

The orchestrator never handles API keys, tokens, or credentials. Authentication is entirely delegated to the CLI tools (`claude`, `codex`), which manage their own credential stores. The orchestrator's Python process never sees or transmits API credentials.

### 2. Subprocess Isolation

AI CLIs run as subprocesses with controlled environments:

- **Environment variables** — only `PATH`, `HOME`, `USER`, `LANG`, `TERM`, `GIT_DIR`, `GIT_WORK_TREE`, and explicitly allowlisted vars are passed. Credential vars like `AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN`, etc. are stripped.
- **Working directory** — set to the worktree path, not the user's home directory.
- **No shell expansion** — commands are passed as argument arrays to the Python `subprocess` APIs, not as shell strings. `shell=False` always.
- **Timeouts** — every subprocess has a hard timeout (configurable per phase). Killed on timeout.
- **No network access control** — the orchestrator cannot restrict network access of CLI subprocesses. The CLIs make outbound network requests to their vendor APIs.
- **No home directory sandboxing** — vendor CLIs read auth state, caches, and config from the user's home directory. This is intentional (auth must work) but means vendor CLI behavior is not fully isolated.

### 3. Artifact Validation

Every AI output is validated before the orchestrator acts on it:

- **JSON schema validation** — outputs must conform to schemas. Invalid output triggers retry, not execution.
- **Application-level validation** — path normalization (reject `..` anywhere in path, not just leading), dependency graph acyclicity, step ordering, and file correspondence checks.
- **No eval/exec** — the orchestrator never evaluates AI-generated code. It only reads structured JSON outputs and git diffs.

### 4. Git Worktree Isolation

Mutating steps run in a single git worktree per run, not on the main branch:

- The main branch is never modified until explicit human approval and merge
- The worktree is a full checkout on a separate branch
- Failed/rejected worktrees are removed without affecting the main branch
- Merge requires human approval by default
- Base commit SHA is recorded and verified before merge

### 5. Human Approval Gates

By default, two approval gates exist:

- **Plan approval** — human reviews what the AI plans to do before execution
- **Merge approval** — human reviews the actual diff before it touches the main branch

These can be individually disabled in config but cannot be programmatically bypassed at runtime.

### 6. Secret Scanning in Prompts

Before sending file content as part of a prompt, the orchestrator runs a lightweight scan for common secret patterns:

- AWS keys (`AKIA...`)
- Private keys (`-----BEGIN.*PRIVATE KEY-----`)
- High-entropy strings assigned to known secret variable names
- `.env` file contents

If a potential secret is detected:
- The file is excluded from the prompt
- A warning is logged
- The step proceeds without that file's contents

**This is best-effort.** It will miss novel secret formats, encoded secrets, and secrets in non-standard locations. Users must ensure sensitive files are in `.gitignore` and not tracked.

### 7. Log Hygiene

- Raw CLI output (which may contain file contents from prompts) is stored only when `logging.retain_raw_output = true`
- Prompt files are stored only when `logging.retain_prompts = true`
- Both default to `false` — no sensitive content is retained by default
- Orchestrator event logs (state transitions, decisions, errors) are always retained but contain no file contents
- All logs are in `.ai-orchestrator/logs/` which should be in `.gitignore`
- `aio clean` removes logs for completed runs

### 8. State File Integrity

- State files use atomic writes (write to temp file, then `os.replace()`)
- `filelock` prevents concurrent access from multiple `aio` processes
- On parse failure, the run is marked unrecoverable (no guessing at recovery)

### 9. Merge Safety

Before merging:
- Base branch must have a clean working tree (no uncommitted changes)
- Base commit SHA must match what was recorded at worktree creation
- If the base branch has advanced, the user must explicitly approve
- Git hooks are not disabled — if repo hooks exist, they run during merge

### 10. Dependency Minimization

The orchestrator has minimal dependencies (click, rich, jsonschema, pydantic, filelock, tomli). No AI SDKs, no HTTP client libraries, no web frameworks. This reduces supply chain attack surface.

All dependencies are pinned in lock files for reproducible installs.

## What the Orchestrator Does NOT Protect Against

| Risk | Status | Notes |
|---|---|---|
| Malicious AI-generated code | **User responsibility** | The merge approval gate is the checkpoint. Users must review diffs. |
| AI CLI phoning home | **Delegated to CLI vendor** | Claude and Codex CLIs make network requests; the orchestrator cannot prevent this. |
| Vendor data retention | **Delegated to CLI vendor** | Code sent as prompt context is transmitted to vendor APIs. |
| AI hallucinating file paths | **Mitigated** | Application-level path validation catches invalid paths; worktree isolation limits blast radius. |
| Local privilege escalation | **Out of scope** | The orchestrator runs with the user's permissions. |
| Repo with malicious git hooks | **User responsibility** | Git hooks run during worktree and merge operations. Users should audit hooks. |
| CLI auth expiry mid-run | **Mitigated** | `BLOCKED_ON_CLI` state; user fixes auth, then resumes. |
| CLI rate limiting / quota | **Mitigated** | Treated as step failure; retried or surfaced as `BLOCKED_ON_CLI`. |

## Recommended Practices

1. **Always enable merge approval** — review every diff before it lands.
2. **Keep secrets out of the repo** — the orchestrator reads tracked files as prompt context.
3. **Run `aio doctor` after CLI updates** — verifies version compatibility.
4. **Use `.gitignore`** — ensure `.ai-orchestrator/` runtime directories are ignored.
5. **Keep `logging.retain_raw_output` off** unless debugging — avoids local copies of prompt context.
6. **Pin dependencies** — use `pip install ai-orchestrator==X.Y.Z` in shared environments.
7. **Audit `aio.toml` changes** — treat orchestrator config as security-relevant.
8. **Evaluate vendor data policies** — understand where your code goes when sent to Claude/Codex APIs.
