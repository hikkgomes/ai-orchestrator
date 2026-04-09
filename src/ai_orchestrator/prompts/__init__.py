"""Prompt template construction for all workflow phases.

Templates are Python f-strings per DD-13 (docs/design-decisions.md).

Each function returns a fully rendered prompt string ready to pass to a
CLI adapter.  Every prompt includes:
1. The full JSON schema for the expected output.
2. The explicit instruction: "Respond with ONLY valid JSON. No markdown fences.
   No commentary."

See AGENTS.md for the full prompt template specifications.
"""
