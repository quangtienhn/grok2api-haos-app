# Security Policy

## Reporting vulnerabilities

Please open a private issue or contact the maintainer. Do not post secrets, cookies, tokens or
production configuration in public issues.

## Secret handling rules for this repository

- Never commit `.env`, `secrets.env`, database files (`.db`), browser profiles, cookie jars, logs,
  or any file containing real cookies/session tokens/API keys/passwords.
- `.env.example` (if added) must contain placeholders only.
- Test fixtures must not contain real credentials.
- If you discover a real secret in the tree or history, report it; do not force-push or rewrite
  history yourself.

## Production safety

- Do not run tests against production Grok sessions.
- Do not attempt to extract or log cookies/tokens.
- FlareSolverr and Grok2API must remain LAN-only; never expose port 8000 or 8191 to the public
  Internet.
