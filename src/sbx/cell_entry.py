"""The cell's entry point, copied into its working directory as `main.py`.

The runtime sits beside it in a `_sbx` package — the same encoder and the same
protocol module the host uses, so the wire format has one definition rather
than two that drift. Nothing sbx-specific is imported from anywhere else: the
cell has no site-packages and cannot reach the installed package.
"""

from _sbx.runtime import main

main()
