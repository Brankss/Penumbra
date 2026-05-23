# Contributing to Penumbra

Thanks for considering a contribution. Penumbra is small on purpose — the whole
codebase is designed to fit in your head in an afternoon. Every line either
earns its weight or gets cut.

## Ground rules

1. **Every change must justify itself against the threat model.**
   Penumbra is a privacy-first tool. A change that improves research quality
   while weakening privacy guarantees does not ship without an explicit opt-in
   flag and a clear note in the README.

2. **No new mandatory dependencies without a fight.**
   The dependency surface is part of the threat model. If you can do it in
   ~50 lines of stdlib Python, do it that way.

3. **Tests are required for any non-trivial change.**
   `pytest` must pass on Python 3.11, 3.12, and 3.13 across Linux, macOS, and
   Windows. CI runs the matrix on every PR.

## Dev setup

```bash
git clone https://github.com/Brankss/Penumbra.git
cd Penumbra

python -m venv .venv
source .venv/bin/activate          # or .venv\Scripts\Activate.ps1 on Windows

pip install -e ".[dev,all]"   # installs penumbra-research from source
playwright install chromium
```

## Running tests

```bash
pytest tests/ -v
```

Smoke tests are deterministic — no network, no LLM calls. If you add an
integration test that needs network or an LLM, mark it `@pytest.mark.integration`
and skip it by default.

## Code style

```bash
ruff check src/ tests/
ruff format src/ tests/
```

- Type hints on every public function. `mypy --strict` should pass.
- `from __future__ import annotations` at the top of every module.
- Docstrings: one paragraph, no rituals. Say what the function does and why.

## Pull request checklist

- [ ] Tests pass locally (`pytest tests/ -v`)
- [ ] `ruff check` and `ruff format --check` are clean
- [ ] If you added a feature: a one-line entry in `CHANGELOG.md` under `## Unreleased`
- [ ] If you touched the threat model: a paragraph in the PR description explaining the impact

## Reporting security issues

Please don't open a public issue for security problems. Email the maintainer
directly (see GitHub profile) or use GitHub's private security advisory feature.
