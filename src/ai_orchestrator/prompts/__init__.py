"""Prompt template construction for all workflow phases.

Templates are Python f-strings per DD-13 (docs/design-decisions.md).

Each function returns a fully rendered prompt string ready to pass to a
CLI adapter. JSON phases validate parsed output against schemas; markdown
phases intentionally return plain text artifacts.

See AGENTS.md for the full prompt template specifications.
"""
