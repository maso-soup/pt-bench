#!/usr/bin/env python3
"""Back-compat shim: `python run.py ...` still works. The runner now lives in the
package as `bench.cli` and installs as the `pt-bench` console command."""
from bench.cli import main

if __name__ == "__main__":
    main()
