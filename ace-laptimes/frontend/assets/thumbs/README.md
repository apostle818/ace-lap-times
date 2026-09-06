# Track and car thumbnails

Every file in this directory was drawn from scratch for this repository. None
of it was downloaded, traced from a photograph, or copied from a map, a game
asset, or any other work. They are covered by the repository's MIT licence
along with the rest of the source — see `LICENSE` at the repo root.

Nothing here is fetched at page load: the files ship inside the frontend image
(`frontend/Dockerfile` copies the build context, this directory included) and
are served from the same origin as the app, so the CSP in `nginx/nginx.conf`
stays at `img-src 'self' data:` with no third-party origin trusted. That is
the same reason Chart.js is vendored rather than pulled from a CDN.

## What they are

**`track-*.svg`** — a stylised loop per circuit. These are decorative glyphs
that echo each circuit's general character (long and narrow for Monza, a
sprawling ribbon for the Nordschleife, a crossover for Suzuka). **They are not
survey-accurate track maps** and should not be read as one; an accurate map
would be someone else's copyrighted work.

**`car-*.svg`** — a side-profile silhouette per car *class*, not per model:
`car-gt3` and `car-gt4` for the two GT classes, `car-formula` for open-wheelers.
No silhouette claims to be a particular manufacturer's car.

**`track-unknown.svg` / `car-unknown.svg`** — the neutral placeholders, drawn in
a muted grey so they read as "no artwork for this one" rather than as a claim
about the track or car. Anything the lookup tables in `app.js` do not recognise
lands here.

## Adding one

1. Draw a new `48 × 32` SVG matching the existing plate (`#14141e` fill, a
   `#2a2a3a` hairline border, one accent colour from the app palette).
2. Add its slug to `TRACK_THUMBS` or `CAR_THUMBS` in `frontend/app.js`.
3. CI checks that every name those tables point at exists here, so a typo in
   either place fails the build rather than shipping a broken image.

Keep the filename a plain `[a-z0-9-]` slug: `app.js` only ever puts a name
taken from those constant tables into the image URL, never a track or car name
that came from the server, and the CI check assumes that shape.
