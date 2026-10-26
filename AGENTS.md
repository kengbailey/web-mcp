# AGENTS.md - Project Guidelines for AI Agents

## Repository Overview
Web MCP is a FastMCP server providing web search, page/PDF/forum reading, Reddit and YouTube tools.
- **Stack:** Python 3.11+, FastMCP 4.1, Docker
- **Entry point:** `src/server/delivery_server.py` (Dockerfile CMD); tools live in `src/server/tools/<area>.py`, logic in `src/core/`.
- **Output contract:** every tool returns ONE compact text block (no structuredContent, no output schemas).
  Long text pages via `next: <cursor>` → `read_content`; listings via `more: <cursor>`. Errors are
  `ERROR <CODE>: <message>` (`src/core/errors.py`). See `doc/v3.md`.
- Keep the tool catalog small: it is resent to clients every turn; `tests/test_contract.py` enforces a byte budget.
- Do not reintroduce Jina Reader or JSON envelopes. Live infra identifiers keep their `webintel` names
  (see `deploy/README.md`).

## Allowed Actions
- Modify files in `src/` and `tests/`
- Add new tools: logic in `src/core/`, registration in `src/server/tools/<area>.py`
- Update `requirements.txt` for new dependencies
- Update `README.md` to document new features

## Restricted Actions
- **DO NOT** modify `.github/workflows/` without explicit approval
- **DO NOT** modify `Dockerfile` or `docker-compose.yml` without explicit approval
- **DO NOT** commit secrets, API keys, or credentials
- **DO NOT** push directly to `main` — always use feature branches

## Workflow
1. Create feature branch: `git checkout -b feature/<name>`
2. Make changes
3. Run tests: `pytest tests/ -v`
4. Commit with descriptive message
5. Push and create PR

## Testing
```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_search.py -v

# Run with coverage
pytest tests/ --cov=src --cov-report=term-missing
```

## Code Style
- Use type hints for all function signatures
- Follow existing patterns in `src/core/` for new fetchers
- Keep handlers thin — business logic goes in `src/core/`

## Branch Naming
- Features: `feature/<short-description>`
- Bugfixes: `fix/<short-description>`
- Refactors: `refactor/<short-description>`

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
