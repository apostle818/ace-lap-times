# Frontend

Static page: `index.html`, `app.js`, and Chart.js vendored into `vendor/`.

The container image fetches Chart.js at build time (see `Dockerfile`), so it
is not in the repository. To run the frontend from a source checkout, fetch
it once:

```bash
npm install --no-save chart.js@4.4.7
mkdir -p vendor && cp node_modules/chart.js/dist/chart.umd.min.js vendor/
npx serve -s . -l 3000
```

Without that step everything works except the progress chart, which shows a
message saying the library is missing.

Chart.js is deliberately served from this app rather than a CDN: the page
then loads no third-party script, which keeps the Content-Security-Policy in
`nginx/nginx.conf` at `script-src 'self'`.
