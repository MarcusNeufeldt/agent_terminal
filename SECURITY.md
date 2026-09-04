# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for security issues. Do not open a public issue containing credentials, account data, exploitable details, or proof-of-concept code that could affect a live account.

## Credential handling

- Keep Kraken credentials in `terminal/.env` or process environment variables.
- Keep OpenRouter credentials in `~/.pi/agent/auth.json`.
- Never commit credentials, SQLite databases, logs, or account screenshots.
- Use restricted API permissions. Enable only the Kraken permissions the terminal needs.
- Revoke and replace a key immediately if it appears in Git history, logs, screenshots, issues, or chat.

The terminal starts DISARMED after every server restart. This reduces accidental writes but does not replace exchange-side API restrictions or account monitoring.

## Supported versions

This project currently supports only the latest commit on `main`.
