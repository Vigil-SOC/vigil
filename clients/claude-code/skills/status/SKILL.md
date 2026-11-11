---
name: status
description: Check whether this session's Vigil answers for its credential. Use when the Vigil tools fail, before first use, or when asked whether Vigil is connected.
---

# Status

Find out whether the `vigil` MCP server answers for this credential.

Vigil's kernel does the work. This session starts it, steers it and reads it: it does not reason through a workflow itself, and it never acts on Vigil except through the `vigil` MCP tools.

Make one cheap read, such as `list_findings` with a limit of 1. If it answers, say that Vigil answers for this credential and what the read returned. Do not guess which person the credential belongs to.

A refusal or an error is the answer, not something to work around. Report it as given. The usual causes:

- the `vigil` server is missing from `/mcp`: the plugin's Vigil address or credential is unset (`/plugin` → vigil → configure);
- 404: the operator has not opened the MCP surface;
- 401: the credential is wrong, expired or revoked; mint a new one;
- a permission refusal: the credential's person lacks that role.
