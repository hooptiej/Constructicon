"""Operational command-line tools that ship with the app (#634).

Run from the repo root (or /app in a container): `python3 -m core.manage.<tool> ...`.
  reset_password  reset a password, re-enable an account, create a rescue admin, list accounts
  mcp_token       generate / check the install token (stdlib only: runs on a bare checkout)

They live under core/ because every install mounts core/ into its containers; scripts/ is not
mounted (it holds tests, checks and maintenance tools). Keep this file free of imports: the
mcp_token `generate` path must work with no app dependencies installed.
"""
