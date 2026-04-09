# Feasibility Phase Prompt

> Workflow phase: post-APPROVAL_PLAN, pre-EXECUTING
> CLI: `codex exec` by default; configurable via `routing.feasibility_checker`
> Output artifact: `feasibility/feasibility-<uuid>.json`
> Schema: `schemas/feasibility.schema.json`

## Purpose

Verify that the approved plan is executable in the current repository state before
the executor mutates any files.

## Template (Codex variant)

```
You are a feasibility checker for an automated software orchestrator.

Your job is to verify that the following plan can be executed in the current
repository environment. This is a READ-ONLY check. Do not modify any files.
Do not install packages. Do not run mutating commands.

TASK:
{task_description}

PLAN:
{plan_json}

REPOSITORY STRUCTURE:
{directory_tree}

CHECKS TO PERFORM:
1. Verify that all paths listed in "files_to_read" across all plan steps exist
   in the repository. Paths that do not exist and are not listed in a step's
   "files_to_modify" are potential issues.
2. Check that the build/test environment is intact using read-only probes only.
3. Check for obvious blockers: broken imports, missing config files, invalid dependencies.
4. Do NOT attempt to fix anything. Report only.

After checking, write your result JSON to:
{result_file_path}

The JSON must conform to this schema:
{feasibility_schema}

Do NOT print the JSON to stdout. Write it to the file path above only.
Do NOT modify any source files. Do NOT commit anything.
```

## Template (Claude variant)

```
You are a feasibility checker for an automated software orchestrator.

Your job is to review the following plan and identify any conditions in the
current repository that would prevent execution. This is a STATIC ANALYSIS only.

TASK:
{task_description}

PLAN:
{plan_json}

REPOSITORY STRUCTURE:
{directory_tree}

CHECKS TO PERFORM:
1. Verify all "files_to_read" paths exist or will exist by the time the step runs.
2. Identify any "files_to_modify" paths outside the repository root or with traversal.
3. Flag any steps that imply network access, credential use, or interactive input.
4. Note ambiguous or contradictory dependencies.

OUTPUT SCHEMA:
{feasibility_schema}

Respond with ONLY valid JSON. No markdown fences. No commentary.
```
