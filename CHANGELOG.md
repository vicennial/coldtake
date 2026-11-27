# Changelog

All notable changes to `coldtake` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-27

### Added
- **Blind cross-family MCP server & CLI (`coldtake`):** `coldtake_alternate`, `coldtake_panel`, `coldtake_review`, `coldtake_check`, plus async `coldtake_submit`, `coldtake_status`, and `coldtake_result`, all with strict Pydantic JSON schemas.
- **Zero-API-key subscription providers:** Runs local `claude`, `codex`, and `gemini` CLIs in stripped, read-only, tool-less sandboxes (prompts over stdin), with optional fail-closed OpenRouter fallback under hard SQLite USD caps.
- **CLI onboarding & inspection:** `coldtake try` (10s offline demo), `coldtake doctor`, `coldtake init`, `coldtake install <claude|codex|gemini|cursor> [--print] [--with-skill]`, `coldtake ask`, `coldtake check`, `coldtake history`, `coldtake show <id>`, `coldtake stats [--share]`, and `coldtake report --out scorecard.html`.
- **Read-only MCP telemetry resources:** `coldtake://stats` and `coldtake://history` over both `stdio` and loopback `http`.
- **Omnigent Extension API v1 (`integrations/omnigent`, `coldtake-omnigent`):** Sandboxed **Coldtake Scorecard** sidebar page, conformance-verified against Omnigent's extension checker and conflict-free with `polly` and `debby`.
- **Versioned Extension API (`API_VERSION = 2`):** Custom providers, route resolvers, contracts, and read-only record sources ([docs/extending.md](docs/extending.md)).
- **54-task audited evaluation suite (`evals/`):** Real 432-run subscription baseline comparing `self_review`, `cross_review`, `blind_same`, and `blind_other`.
