# Changelog

Every milestone records what it built **and what it deliberately did not**.
The second half is the interesting one: this project is defined as much by the
fence around it as by the code inside it.

## Milestone 1 — package and CLI shell

**Built.** An installable package with a `sbx` console script and four
argparse subcommands (`seal`, `run`, `verify`, `ls`), each with its real
argument surface, plus `--version`. The verb list has exactly one definition —
a command registry that the parser, the tests and `scripts/smoke.sh` all read
from — so a fifth verb cannot appear without a test failing. Expected failures
raise `SbxError` and surface as a one-line message on stderr with exit 1;
anything else is a bug and is allowed to traceback. Added a fresh-venv install
smoke script and CI on macOS and Linux across Python 3.11 and 3.13.

**Deliberately not built.** Any actual behaviour: every verb parses correctly
and then reports that it is not implemented yet. No logging framework, no
config file, no `--verbose`, no plugin system, no lint or type-check
dependency — the ground rule is stdlib-only with `pytest` as the single dev
extra, and adding tooling deps would break the claim the README makes.
