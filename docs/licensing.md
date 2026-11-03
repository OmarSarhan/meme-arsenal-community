# Licensing and asset provenance

Original project code, documentation, and project-created assets use
[BSD Zero Clause (0BSD)](../LICENSE), to the extent the contributors hold rights
in them. Attribution and publication of modifications are not conditions of this
license. The owner selected it on 2026-11-01. The canonical text is listed by
[SPDX](https://spdx.org/licenses/0BSD.html)
and the [Open Source Initiative](https://opensource.org/license/0bsd).

For example, when redistributing a built dashboard, include its generated
`THIRD_PARTY_LICENSES.txt` alongside `index.html` and retain the applicable
third-party notices. The project license does not grant rights to imported memes.

## Bundled project assets

| Material | Source record | Distribution treatment |
| --- | --- | --- |
| Five dashboard PNG icons | [Generation manifest](../frontend/dashboard/src/assets/dashboard-icons/manifest.json) records Gemini generation, original icon concepts, and an instruction against copying logos. | Project assets under 0BSD to the extent rights exist; preserve the generation record. |
| Two 4×4 PPM fixtures | [Fixture directory](../tests/fixtures) contains simple numeric color grids with synthetic captions and example.com metadata. | Project test assets under 0BSD; no real meme archive is bundled. |
| Images created by tests | [conftest.py](../tests/conftest.py) draws simple rectangles and text using Pillow. | Generated test fixtures, not an imported media collection. |
| UI line icons | The `lucide-react` dependency, including its Feather-derived icons. | Lucide ISC and applicable Feather MIT notices, not the project license. |

The icon manifest is the available generation record; it is not proof of
exclusive copyright or a guarantee that generated imagery is unique. Google's
[Gemini API terms](https://ai.google.dev/gemini-api/terms#use_of_generated_content)
state that Google does not claim ownership of generated content, while users
remain responsible for its use. No additional upstream asset license was found
alongside the five PNGs. Future externally sourced assets need their own source
and permission record before inclusion.

## Dependencies and distributions

Dependencies keep their own licenses. The root 0BSD license does not relicense
React, Lucide, Python packages, ffmpeg, container base images, or other third-party
software. Retain their license files when redistributing those components.

The dashboard build emits `THIRD_PARTY_LICENSES.txt` alongside `index.html` using
Vite's dependency-license collection. Distribute that file with the built
dashboard, including reserve exports and container images. It includes the
licenses of bundled JavaScript dependencies; it is not a complete inventory of
the container's operating-system packages or Python environment.

Python package metadata declares `0BSD` and includes `LICENSE`. The application
image also copies the license into `/app/LICENSE`. Check the emitted notices and
the actual image's dependency inventory before publishing binary distributions.

Imported Telegram messages, uploads, link-preview media, and generated user
remixes are user content. This software license grants no rights to someone
else's media, trademarks, or likenesses. Do not commit an operator's reserve as
sample content.
