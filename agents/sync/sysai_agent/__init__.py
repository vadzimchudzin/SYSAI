"""SYSAI agent synchronizer — the part of SYSAI on an employee's computer (docs/opo/TASKS.md, ОПО-6.3).

Trades the connection code for the agent's token, takes the signed rules from SYSAI, checks the signature, applies
them to the Hermes profile (config.yaml, .env, SOUL.md), keeps SYSAI_Hub skills and tools on the computer, sends the
skills the agent wrote as proposals, reports its pulse and wraps the hub's MCP servers with the same rules. Checks
every action of Hermes's own tools by those rules (`sysai-agent hook`), updates itself to the version SYSAI signed and
installs the window «Агент SYSAI» (`sysai-agent desktop`)."""
__version__ = "0.4.4"
