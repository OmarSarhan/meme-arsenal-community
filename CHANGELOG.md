# Changelog

## 0.1.1 — 2026-11-03

- Check private upload ownership before readiness and file availability when selecting remix sources. Other sessions receive the same not-found response regardless of upload state.
- Refuse legacy reserve backups when destination free space is below the reserve's apparent size plus 1 GiB. This is a capacity estimate, not a guarantee that the database dump will fit. The legacy script still does not produce a complete community backup.
- Rewrite setup, operations, contributor and reference documentation in plain English, with public acquisition/reporting links and dated evidence limits.
- Show the maintained dashboard package version beside the homepage title.

## 0.1.0 — 2026-11-02

Initial source release for the Linux amd64 community Compose deployment. See the release preparation record for evidence and limits.
