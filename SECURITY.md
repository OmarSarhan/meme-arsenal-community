# Security reporting

Report vulnerabilities through GitHub's
[confidential reporting form](https://github.com/OmarSarhan/meme-arsenal-community/security/advisories/new).
Sign in to GitHub, then use **Security → Report a vulnerability**. Do not post
sensitive exploit details, credentials, sessions, database dumps, or personal
media in public issues. If the confidential route is unavailable, keep those
details private rather than opening a public report.

The initial release is v0.1.0. Include the exact version or commit and whether
you use the supported Linux amd64 community Compose deployment or local tooling.
See [release evidence and limits](docs/release-preparation.md) for the tested scope.

Reporting verification: on 2026-11-02, the release coordinator enabled private
vulnerability reporting for `OmarSarhan/meme-arsenal-community` and confirmed the
API returned `enabled: true`. Unauthenticated, cookie-free requests to its public
Security and Advisories pages returned the **Report a vulnerability** action.
Following that action reached GitHub sign-in with the reporting destination
preserved. This verifies public action availability; no authenticated reporter
form submission or synthetic vulnerability report was performed.

Include the affected commit, deployment mode, a minimal reproduction using
synthetic data, and the observed impact. The current trust model and provider
data flows are documented in [privacy.md](docs/privacy.md).
