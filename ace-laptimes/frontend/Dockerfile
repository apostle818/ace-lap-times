FROM node:26-alpine

WORKDIR /app

RUN npm install -g serve

# The build context carries the app plus assets/thumbs — the track and car
# thumbnails, drawn for this repo (see assets/thumbs/README.md). They ship in
# the image and are served same-origin like everything else here, so nothing
# is fetched at page load and img-src in nginx.conf stays 'self' with no
# remote origin. Same reasoning as the Chart.js vendoring below, minus the
# download: these were never anybody else's to fetch.
COPY . .

# Chart.js is served from this image rather than a CDN. Nothing third-party
# is fetched at page load, so the CSP can stay at script-src 'self' and a
# compromise of someone else's CDN cannot execute in a user's session.
# The npm package ships dist/chart.umd.js — already bundled and minified.
# (The dist/chart.umd.min.js path the CDN used to serve does not exist in the
# package; jsdelivr generates that variant itself.) The existence check makes
# a future rename fail here with a readable message instead of a bare cp error.
RUN npm install --no-save --no-package-lock chart.js@4.4.7 \
 && test -f node_modules/chart.js/dist/chart.umd.js \
    || (echo "chart.js dist layout changed - update this Dockerfile" && exit 1) \
 && mkdir -p vendor \
 && cp node_modules/chart.js/dist/chart.umd.js vendor/ \
 && rm -rf node_modules package.json package-lock.json

# Outfit and JetBrains Mono are vendored the same way, for the same reason:
# index.html used to load them from fonts.googleapis.com/fonts.gstatic.com,
# which was the one third-party origin this page still trusted (the CSP in
# nginx.conf had to carve out both hosts to allow it). @fontsource ships the
# same families as plain @font-face CSS plus woff2/woff files, fetched once
# at build time instead of by every visitor's browser. Only the "latin"
# subset is pulled — this UI is English-only, so the latin-ext/cyrillic/
# vietnamese/... subsets Google's own CSS API would otherwise also serve are
# not needed. The weight list matches the old Google Fonts URL exactly
# (Outfit 300/400/500/600/700/800/900, JetBrains Mono 400/500/600/700 — see
# --font/--mono in index.html). The existence checks make a future
# @fontsource layout change fail here with a readable message instead of a
# missing font at runtime.
RUN npm install --no-save --no-package-lock @fontsource/outfit@5.3.0 @fontsource/jetbrains-mono@5.3.0 \
 && mkdir -p vendor/fonts/files \
 && for w in 300 400 500 600 700 800 900; do \
      test -f node_modules/@fontsource/outfit/latin-$w.css \
        || (echo "@fontsource/outfit layout changed - update this Dockerfile" && exit 1); \
      cat node_modules/@fontsource/outfit/latin-$w.css >> vendor/fonts/fonts.css; \
      cp node_modules/@fontsource/outfit/files/outfit-latin-$w-normal.woff2 \
         node_modules/@fontsource/outfit/files/outfit-latin-$w-normal.woff \
         vendor/fonts/files/; \
    done \
 && for w in 400 500 600 700; do \
      test -f node_modules/@fontsource/jetbrains-mono/latin-$w.css \
        || (echo "@fontsource/jetbrains-mono layout changed - update this Dockerfile" && exit 1); \
      cat node_modules/@fontsource/jetbrains-mono/latin-$w.css >> vendor/fonts/fonts.css; \
      cp node_modules/@fontsource/jetbrains-mono/files/jetbrains-mono-latin-$w-normal.woff2 \
         node_modules/@fontsource/jetbrains-mono/files/jetbrains-mono-latin-$w-normal.woff \
         vendor/fonts/files/; \
    done \
 && rm -rf node_modules package.json package-lock.json

# serve binds 3000, so no privileged port is needed and the process can drop
# to the image's built-in unprivileged user.
USER node

EXPOSE 3000

CMD ["serve", "-s", ".", "-l", "3000"]
