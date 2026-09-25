# Contributing

Thanks for helping with GPT Image Studio.

## Development Setup

1. Install Python 3.10 or newer.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Start locally with `.\start.ps1` (desktop client) or `python app.py` (internal service only).

## Code Style

- Keep provider URLs and API keys out of source code, configuration files, logs, and history.
- Do not add a default provider URL or a default API key.
- Only `gpt-image-*` models are supported; do not add non-GPT model families back.
- Keep the official parameter model: 10 ratios times `1K / 2K / 4K`, with quality derived from the tier.
- Follow the existing Flask backend and Tkinter desktop client structure.
- Keep files UTF-8 encoded and prefer LF line endings.

## Validation

Before submitting changes:

- Run `python -m py_compile app.py client.py make_icon.py`.
- Run `python -m unittest discover -s tests`.
- Start the app and verify `/api/health`.
- If packaging changes, run `.\build.ps1`.

## Commits

- Use focused, descriptive commit messages.
- Keep generated files under `build/`, `dist/`, and `outputs/` out of commits.

## Pull Requests

- Open pull requests against `main`.
- Describe what changed and how the change was verified.
