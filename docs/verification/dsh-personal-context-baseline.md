# DSH Personal Context Baseline

This is the frozen initial snapshot recorded on 2026-08-17. Its rc5 release and source commit are historical evidence, not the current compatibility target. See the current [DSH compatibility report](../../dsh-plugin/DSH_COMPATIBILITY.md) for the rc2 target and verification result.

- OmniMe base commit: `3ab4993bf5c4f6889212a0aaaf4f475332c680af`
- DSH release: `0.1.0-rc.5`
- DSH source commit: `47f943859bef60e4160492346772ded9b24f765a`
- Node minimum: `22.19`
- Legacy database: preserved, never migrated in place
- Legacy keyboard/OCR capture: remains active until G4 passes

## Existing test baseline

Command:

```text
PYTHONPATH=src venv/bin/python -m pytest -q
```

Environment: local Python 3.14 virtual environment. Runtime dependencies were
installed from `requirements.txt`; test-only `pytest` (test runner) and
`httpx2` (required by the current Starlette test client) were added.

Fresh-environment first run:

```text
exit code 1
2 failed, 320 passed in 18.52s
```

The two failed nodes were:

- `tests/test_chat_window_capture.py::test_sampler_selects_frontmost_eligible_layer_zero_window`
- `tests/test_chat_window_capture.py::test_sampler_throttles_typing_to_one_capture_per_250ms`

Both showed a sampler baseline/capture wait timeout and the common log
`baseline sampler worker did not stop within 1.00s`. No code or test changes
were made in response.

Successful reruns with the same command:

```text
322 passed in 7.83s
322 passed in 7.10s
322 passed in 6.93s
322 passed in 6.76s
```

Conclusion: observed one existing nondeterministic sampler flake; subsequent
consecutive runs passed.
