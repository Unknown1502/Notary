# Third-party notices

Notary is MIT licensed (see [LICENSE](LICENSE)). It bundles and depends on the
following third-party work.

## Bundled fonts — SIL Open Font License 1.1

The production build embeds these font files directly into `frontend/dist/`
via `@fontsource`, so they are **redistributed** with the application. OFL-1.1
requires the copyright notice and licence text to accompany the font software,
which is the reason this file exists.

The OFL applies to the font software only. It does not affect the licence of
the application code.

### IBM Plex Sans, IBM Plex Mono

> Copyright 2019 IBM Corp. All rights reserved.
>
> This Font Software is licensed under the SIL Open Font License, Version 1.1.

Full text: <https://scripts.sil.org/OFL> · Source: <https://github.com/IBM/plex>

### Space Grotesk

> Copyright 2020 The Space Grotesk Project Authors
> (<https://github.com/floriankarsten/space-grotesk>)
>
> This Font Software is licensed under the SIL Open Font License, Version 1.1.

Full text: <https://scripts.sil.org/OFL>

Verbatim licence texts ship in the installed packages at
`frontend/node_modules/@fontsource/ibm-plex-sans/LICENSE`,
`frontend/node_modules/@fontsource/ibm-plex-mono/LICENSE`, and
`frontend/node_modules/@fontsource-variable/space-grotesk/LICENSE`.

## Runtime dependencies

All permissive; none impose obligations on this project beyond attribution.

| Package | Licence |
|---|---|
| Genblaze (`genblaze-core`, `-s3`, `-gmicloud`, `-luma`) | MIT |
| FastAPI, Starlette, uvicorn, pydantic, httpx, sse-starlette | MIT |
| React, react-dom | MIT |
| Vite, TypeScript, esbuild, Rollup | MIT / Apache-2.0 |
| boto3, botocore | Apache-2.0 |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| numpy | BSD-3-Clause |
| Pillow | MIT-CMU (HPND) |

## External services

Backblaze B2, GMI Cloud, and Luma are used through their public APIs under
their own terms. No vendor code is redistributed here.
