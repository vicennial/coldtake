# Contributing

## Public-tree rules

coldtake is a public project. Keep the tree free of:

- hostnames, IP addresses (other than the loopback defaults), ports other than defaults;
- home-directory paths, credentials, tokens, account or session material;
- names of private companies or projects, and real handles or email addresses.

Demo and test content is fictional. Tests never call real provider CLIs or networks and
never read a real home directory: use fake executables, a fake `HOME`, and
`env -u CODEX_HOME -u CLAUDE_CONFIG_DIR` for subprocess smoke tests.

## Commits

Commit with the maintainers' public no-reply identity. A commit message ends with a single
`Co-Authored-By:` trailer for any AI assistant and carries no session links.

## Checks

```
ruff check . && ruff format --check .
pytest
```

The suite treats warnings as errors.
