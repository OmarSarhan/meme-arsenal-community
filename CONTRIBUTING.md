# Contributing

Meme Arsenal serves individuals running one private library. Public contributions
go to [meme-arsenal-community](https://github.com/OmarSarhan/meme-arsenal-community).
Start with the
[developer guide](docs/development.md) for installation, source files and checks.
The full application uses the online API; the SQLite/mock demo has fewer features.

Open an issue describing a concrete use case or reproducible bug before a large
change. Small fixes can go directly into a pull request. Include the behavior
before and after the change, the relevant issue, and the checks actually run.
Keep changes focused and preserve existing data and IDs. A plugin framework,
team accounts, and a broad schema rewrite are outside the first-release scope.

For example, reproduce a search bug with a generated image and synthetic caption,
state the query and expected item, and name the focused test you ran.

Use temporary fixtures and mock or stubbed providers for automated tests. Do not
include credentials, Telegram sessions, production dumps, logs containing user
content, or private media in issues, commits, or test fixtures. See
[security reporting](SECURITY.md) for vulnerabilities and [data flows](docs/privacy.md)
for what live provider operations can transmit.

Original contributions are accepted under the project's [0BSD license](LICENSE).
Submit only work you have the right to contribute. Record the source and license
of any third-party material and retain its notices; do not relabel it as 0BSD.
When adding dependencies or assets, update the [licensing notes](docs/licensing.md)
and inspect the generated dashboard dependency notices.
