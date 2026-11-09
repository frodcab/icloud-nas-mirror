# Security policy

This application holds your Apple session and serves a web panel, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue** for a vulnerability. Use GitHub's private reporting:
**Security → Report a vulnerability** in this repository. Include steps to reproduce and the version
(`/health` shows it). You will get an answer as soon as possible; this is a one-person project, so there is no
guaranteed response time.

Never include your Apple credentials, `data/secrets.env`, session cookies, TOTP secrets or recovery codes in a report.

## Supported versions

Only the latest release receives fixes.

## Hardening checklist for users

- Keep `data/` readable only by the user that runs the container; it holds Apple session cookies.
- Use a long, unique `GUI_PASS` and enable the authenticator app (TOTP) for every panel user.
- If you expose the panel beyond your LAN, put it behind a reverse proxy with HTTPS and restrict
  `AUTH_ONLY_NETWORKS` where you can.
- Keep the image up to date (`docker compose pull`).
- Only set `AUTH_DISABLED=true` if access to the panel is already restricted by something else (VPN, authenticating reverse proxy, firewall).
  Without a login anyone who can reach the port can act on your iCloud sync. Never combine it with `AUTH_ALLOW_PUBLIC=true` on an exposed port.
