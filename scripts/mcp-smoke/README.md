# Manual MCP smoke scripts

These scripts are exploratory checks for local MCP services. They are **not**
part of the automated pytest or frontend Vitest suites — nothing runs them for
you. Run one explicitly when the service it exercises is available.

## Prerequisites

- **Node.js** plus **Playwright** and its Chromium browser. Neither is declared
  in a project manifest, so install them yourself:

  ```bash
  npm install -g playwright
  npx playwright install chromium
  ```

- The **Vigil frontend running locally**. Most scripts expect
  `http://127.0.0.1:6988`; `test_localhost_mcp.js` expects `http://localhost:5173`.

## Running

```bash
node scripts/mcp-smoke/<file>.js
```

Each script launches a Chromium browser via Playwright and writes screenshots
(`*.png`) and text dumps (`*.txt`) to the **current working directory**. Run it
from a scratch location, or clean up afterward, to avoid leaving artifacts in
the repo tree.

They may also require service-specific environment variables. Do not add
credentials or captured customer data to this directory.
