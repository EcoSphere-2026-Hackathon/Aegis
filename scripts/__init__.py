"""Harnesses and operational tools.

A package rather than loose files because ``run.py --demo`` imports the
golden-demo replay, so the same module has to resolve identically whether it
is run directly or imported. Without this, a type checker sees the file under
two module names and refuses to check either.
"""
