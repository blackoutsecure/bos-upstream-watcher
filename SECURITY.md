# Security Policy

## Reporting Security Vulnerabilities

**Do not open public GitHub issues for security vulnerabilities.**

If you discover a security vulnerability in Blackout Secure Upstream
Watcher, please report it by emailing security@blackoutsecure.app.

Please include:

- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested remediation (if any)

We acknowledge all security reports within 48 hours.

## Security Best Practices

### When using this action

1. **Pin to a major version tag**, never to `main`:

   ```yaml
   - uses: blackoutsecure/bos-upstream-watcher@v1
   ```

   For audit-grade pinning, use the commit SHA:

   ```yaml
   - uses: blackoutsecure/bos-upstream-watcher@<full-sha>  # v1.0.0
   ```

2. **Scope the GitHub token**. For polling a private upstream repository,
   create a fine-grained PAT with `Contents: read` on that repo only.
   Do not reuse a write-scoped token.

3. **Restrict workflow permissions** at the job level:

   ```yaml
   permissions:
     contents: write   # only when committing the tracker file
   ```

4. **Treat the upstream response as untrusted**. The action validates
   resolved versions against a SemVer-shaped regex before they are written
   to `GITHUB_OUTPUT`, and rejects values containing newlines, but
   downstream steps should still avoid passing the version into a `run:`
   body without surrounding quoting.

## Trust boundaries

| Boundary | Treatment |
|----------|-----------|
| All `inputs.*` | Forwarded via `env:`; never interpolated into `run:` bodies |
| Upstream HTTP responses | Parsed with stdlib `json` / `re`; no `eval` or shell exec |
| Generated outputs | Single-line enforced for all outputs except `release_body`, which is emitted via heredoc (delimiter collisions are detected and extended) |
| Tracker file path | Repo-relative only; rejects absolute paths and `..` |
| Image refs | Supported: `docker.io/<ns>/<image>`, `ghcr.io/<owner>/<image>` (public), `quay.io/<ns>/<image>` (public); all other registries rejected with an explicit error |

## Supported Versions

| Version | Status |
|---------|--------|
| 1.x     | Active |

## Dependencies

This action is **stdlib-only Python**. No `pip install` step runs at
action time. The runner needs `python3` (already present on all
GitHub-hosted runners).

## Vulnerability scanning

- Dependabot watches the `actions:` ecosystem in this repo.
- Generated code is scanned by GitHub code scanning on every push.

## Related files

- [LICENSE](./LICENSE) — Apache License 2.0
- [NOTICE](./NOTICE) — Third-party attribution

---

For security-related questions, contact security@blackoutsecure.app.
