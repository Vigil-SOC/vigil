# Contributing to Vigil

Thank you for your interest in contributing to Vigil. This document covers how to get started, how to submit changes, and how to find things to work on.

## Getting Started

### Prerequisites

- Python 3.10+
- Node.js 18+
- Docker Desktop (must be running)
- Git with submodule support
- Claude API key from [console.anthropic.com](https://console.anthropic.com/)

### Local Setup

```bash
git clone --recurse-submodules https://github.com/DeepTempo/vigil.git
cd vigil
./start_web.sh
```

Access the frontend at http://localhost:6988 and the API at http://localhost:6987.

See the [README](README.md) for full setup instructions including manual install and Docker options.

## How to Contribute

### Fork and Pull Request Workflow

1. **Fork** the repository to your GitHub account
2. **Clone** your fork locally:
   ```bash
   git clone --recurse-submodules https://github.com/YOUR-USERNAME/vigil.git
   cd vigil
   ```
3. **Create a branch** for your work:
   ```bash
   git checkout -b feature/your-feature-name
   ```
4. **Make your changes** and test them
5. **Commit** with sign-off (required — see below):
   ```bash
   git commit -s -m "Add new MCP integration for SentinelOne"
   ```
6. **Push** to your fork:
   ```bash
   git push origin feature/your-feature-name
   ```
7. **Open a Pull Request** from your fork to `DeepTempo/vigil:main`

### Sign-Off Requirement (DCO)

All commits must include a `Signed-off-by` line certifying you have the right to submit the code under Vigil's Apache 2.0 license. This is the [Developer Certificate of Origin](DCO).

Add it automatically with the `-s` flag:

```bash
git commit -s -m "Your commit message"
```

This adds a line like `Signed-off-by: Your Name <your@email.com>` using your git config identity. Configure it once:

```bash
git config user.name "Your Name"
git config user.email "your@email.com"
```

## What to Work On

### Good First Issues

Look for issues labeled [`good-first-issue`](https://github.com/DeepTempo/vigil/labels/good-first-issue) — these are scoped, well-defined tasks suitable for new contributors.

### Using the Auto-Contributor

Vigil includes a competitive research tool in `contrib/auto-contributor/` that identifies capability gaps versus proprietary AI security platforms and generates contribution specifications. If you want to find meaningful work:

1. Pick a proprietary AI SOC or security platform
2. Run the auto-contributor skill to identify gaps
3. The output includes ready-to-file GitHub issues with acceptance criteria

See `contrib/README.md` for details.

### Contribution Areas

Contributions are welcome across all areas:

- **New MCP integrations** — connect Vigil to additional security tools (EDR, SIEM, cloud, ticketing)
- **Agent improvements** — enhance agent prompts, reasoning, or tool usage
- **New Workflows** — define new multi-agent workflows in `workflows/`
- **Detection rules** — add Sigma, Splunk, Elastic, or KQL rules
- **Bug fixes** — check the issue tracker
- **Documentation** — improve docs, add examples, fix errors
- **Tests** — expand test coverage

## Code Guidelines

### Project Structure

```
vigil/
├── workflows/       # Multi-agent workflow definitions (WORKFLOW.md files)
├── contrib/         # Community development tools (not runtime)
├── mcp-servers/     # MCP server implementations
├── backend/         # FastAPI backend + Agent SDK
├── frontend/        # React + MUI frontend
├── services/        # Business logic
├── daemon/          # Headless autonomous SOC
├── database/        # PostgreSQL models
├── data/            # Schemas, registry, taxonomy
├── docs/            # Documentation
└── tests/           # Test suite
```

### Style

- Python: follow existing patterns in `backend/`. Use type hints.
- TypeScript/React: follow existing patterns in `frontend/`.
- Workflows: follow the format of existing `workflows/*/WORKFLOW.md` files.
- MCP servers: follow the patterns in `mcp-servers/`.

### Testing

Run the test suite before submitting:

```bash
./scripts/run_tests.sh
```

New features should include tests. Place them in `tests/` following existing naming conventions.

### Commit Messages

Write clear, descriptive commit messages. Format:

```
Short summary (50 chars or less)

Longer description if needed. Explain what changed and why,
not how (the code shows how). Wrap at 72 characters.

Signed-off-by: Your Name <your@email.com>
```

## Pull Request Guidelines

- One logical change per PR
- Include tests for new functionality
- Update relevant documentation
- Reference related issues: "Closes #123" or "Part of #456"
- Keep PRs reviewable — if a feature is large, break it into smaller PRs

## Community

Join us on [Discord](https://discord.gg/Kw68sPJU) to discuss ideas, get help, and collaborate.

## License

By contributing to Vigil, you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).
