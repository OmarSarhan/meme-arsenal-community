# Meme Arsenal Reserve Placeholder

[README and documentation index](../README.md#read-next) ·
[Reserve layout and backup](../README.md#reserve-layout) ·
[Local configuration](../README.md#configuration)

The real `meme-arsenal-reserve/` directory is local product data and is ignored
by git. It can contain databases, Telegram sessions, downloaded media, generated
catalogs, remix drafts, logs, and deployment state.

This is a placeholder, not a sample media library. Creating a directory does not
initialize the full owner app or recover an existing library. Use the
[community setup](../deploy/community/README.md#upload-first-setup) for the owner
app and its [complete recovery procedure](../deploy/community/README.md#restore-to-a-disposable-empty-instance)
for existing data. Local tooling initialization is described in the
[developer guide](../docs/development.md#first-local-setup). Keep reserve data private.

Create or link the real reserve locally when running local tooling:

From the repository root (this creates only the directory, not its database):

```bash
mkdir -p meme-arsenal-reserve
```
