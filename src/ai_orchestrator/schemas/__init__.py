"""Bundled JSON schemas for workflow artifact validation.

These files mirror the canonical schemas in the top-level ``schemas/`` directory.
They are included in the installed wheel so that ``validator.py`` can load them
via ``importlib.resources`` without requiring the source tree.
"""
