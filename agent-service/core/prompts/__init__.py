"""Prompts live in the repo, versioned with the code. Each prompt module has a
`PROMPT_VERSION` that is added to trace metadata.

Prompts never contain simulation answer keys; `tests/test_no_answer_keys.py` checks
every prompt module and every tool.
"""
