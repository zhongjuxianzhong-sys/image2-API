# Security Policy

## Local Configuration

This project intentionally has no built-in provider URL or API key.
Users enter both values in the desktop client. If the user enables
"remember", both values are encrypted with Windows DPAPI and stored in
`client-credentials.dat`; only the current Windows user can decrypt them.
Otherwise the values exist only for the current process and are never written
to disk.

## Handling Sensitive Data

- The internal service binds to `127.0.0.1` only and never reads `.env` or environment variables.
- Never commit `.env` files, API keys, tokens, or provider URLs.
- Keep API keys out of logs and history records.
- Keep `client-credentials.dat` out of commits; it is DPAPI-encrypted but still sensitive.
- Generated images and history under `outputs/` are ignored and should not be committed.

## Reporting a Vulnerability

If you find a security issue, use GitHub's private security advisory workflow
instead of filing a public issue.
