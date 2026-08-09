# Tests

```
tests/
├── conftest.py       shared fixtures; the CLI is driven as the real console script
├── test_*.py         the unit and integration suites, one per component
├── hostile/          strategies that attack the cell — inputs, not tests
└── adversarial/      strategies that attack the *time gate* — written by hand,
                      by the human, never by an agent (milestone 5)
```

Two rules the suite is built on.

**No mocks for the OS.** Isolation, resource limits and subprocess behaviour are
exercised against the real operating system. A test that patches `setrlimit`
asserts something about the patch, not about containment.

**Attack fixtures are not tests.** Everything under `hostile/` and
`adversarial/` is named `strategy_*.py` so pytest never collects it. The tests
load those files by path and feed them to the cell; running one directly would
do exactly what it says it does. See `hostile/README.md`.
