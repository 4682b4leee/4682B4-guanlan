#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令跑全量测试：python3 tests/run_all.py"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

if __name__ == "__main__":
    suite = unittest.TestLoader().discover(str(TESTS), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
