FROM node:20-alpine

WORKDIR /app

RUN npm install -g serve

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

# serve binds 3000, so no privileged port is needed and the process can drop
# to the image's built-in unprivileged user.
USER node

EXPOSE 3000

CMD ["serve", "-s", ".", "-l", "3000"]
