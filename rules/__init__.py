"""
Rules as capsules: signed, content-addressed behavior that fires on incoming
capsules, emits capsules that cite the rule and its input, and earns or loses
trust from reported outcomes (Leighton Weight). See rules/engine.py.
"""

from rules.engine import Rule, RuleEngine  # noqa: F401
from rules.pattern import matches, validate_when  # noqa: F401
