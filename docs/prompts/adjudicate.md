# Legacy Adjudicate Prompt

This prompt is retained only for readers of old run artifacts. New runs do not
enter a separate adjudication phase.

Current behavior lives in `docs/prompts/review.md`: Claude writes the initial
review, Codex performs a review-shaped cross-check inside REVIEWING, and Claude
Opus/max makes one final decision if they disagree.
