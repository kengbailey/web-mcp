# Tests

```sh
.venv/bin/python -m pytest tests/ -q                 # offline suite (MockTransport, in-memory MCP client)
.venv/bin/python -m pytest tests/ -q -m integration  # live providers; skipped without keys
```

- `conftest.py`: `make_client(handler, **settings)` gives an in-memory MCP client over a
  `Services` whose HTTP routes all use an `httpx.MockTransport`; `build_services` for
  direct class tests.
- `test_contract.py`: catalog byte budget, no output schemas, text-only results.
- `test_foundation.py`: text budgets, caches, cursors, paging.
- One `test_<area>.py` per area (web, xenforo, exa, reddit, youtube, stt).
