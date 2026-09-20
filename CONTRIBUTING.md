# Contributing

Thanks for helping with Image2 Studio.

## Development Setup

1. Install Python 3.10 or newer.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Start locally with `.\start.ps1` or `python app.py`.

## Code Style

- Keep provider URLs and API keys out of source code, configuration files, logs, and history.
- Do not add a default provider URL or a default API key.
- Follow the existing Flask backend and vanilla JavaScript frontend structure.
- Keep files UTF-8 encoded and prefer LF line endings.

## Validation

Before submitting changes:

- Run `python -m py_compile app.py`.
- Run `python -m unittest discover -s tests`.
- Start the app and verify `/api/health`.
- If frontend behavior changes, check desktop and mobile views.
- If packaging changes, run `.\build.ps1`.

## Commits

- Use focused, descriptive commit messages.
- Keep generated files under `build/`, `dist/`, and `outputs/` out of commits.

## Pull Requests

- Open pull requests against `main`.
- Describe what changed and how the change was verified.
