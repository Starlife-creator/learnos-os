"""Run the LearnOS test suite.

Use package-mode discovery (`-t .`) so that ``tests/__init__.py`` hardening
patches are applied (sandbox-safe temp-dir cleanup + HTTP client timeout floor).
Without package mode, ``unittest discover -s tests`` imports test modules
top-level and skips the package initializer, leaving environment-noise failures
unmitigated.

Usage:
    python run_tests.py                 # full suite
    python run_tests.py tests.test_db   # a single module
"""
import sys
import unittest

if __name__ == "__main__":
    argv = ["unittest"]
    if len(sys.argv) > 1:
        argv += sys.argv[1:]
    else:
        argv += ["discover", "-s", "tests", "-t", ".", "-p", "test_*.py"]
    unittest.main(module=None, argv=argv, exit=False)
