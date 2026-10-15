#!/usr/bin/env python3
"""Compatibility entry point for the original single-project prototype."""
import sys
from murphy import bridge

if __name__ == '__main__':
    raise SystemExit(bridge.main())

sys.modules[__name__] = bridge
