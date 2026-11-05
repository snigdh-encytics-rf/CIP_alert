"""Shared test setup.

The existing suites check the notebook's numbers (699 cycles, 221 alerts), which were produced without the
object-name aliases in rules/object_aliases.json. Run them in that mode; tests/test_aliases.py covers the
default (aliases on). Servers started by the UI tests inherit this environment.
"""
import os

os.environ['CIP_OBJECT_ALIASES'] = 'off'
