# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for security issues. Do not open a public issue containing credentials, account data, exploitable details, or proof-of-concept code that could affect a live account.

## Credential handling

- Keep Kraken credentials in `terminal/.env` or process environment variables.
- Keep OpenRouter credentials in `~/.pi/agent/auth.json`.
- Never commit credentials, SQLite databases, logs, or account screenshots.
- Use restricted API permissions. Enable only the Kraken permissions the terminal needs.
- Revoke and replace a key immediately if it appears in Git history, logs, screenshots, issues, or chat.

The terminal starts DISARMED after every server restart. Each process generates a new browser token, validates the exact local Host and Origin on every POST, accepts JSON only, and requires a one-time signed challenge before arming. The production server injects the token into its HTML. Restarting invalidates old tabs. Trading and AI requests also carry persisted request IDs so an identical replay returns the stored response instead of submitting again.

These checks block browser CSRF and accidental unauthenticated writes. They do not protect against another process or user that can read localhost responses under the same operating-system account. Keep exchange API permissions restricted and monitor the account.

## Supported versions

This project currently supports only the latest commit on `main`.
