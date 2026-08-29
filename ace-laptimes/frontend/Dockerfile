FROM node:20-alpine

WORKDIR /app

RUN npm install -g serve

COPY . .

# Chart.js is served from this image rather than a CDN. Nothing third-party
# is fetched at page load, so the CSP can stay at script-src 'self' and a
# compromise of someone else's CDN cannot execute in a user's session.
RUN npm install --no-save --no-package-lock chart.js@4.4.7 \
 && mkdir -p vendor \
 && cp node_modules/chart.js/dist/chart.umd.min.js vendor/ \
 && rm -rf node_modules package.json package-lock.json

# serve binds 3000, so no privileged port is needed and the process can drop
# to the image's built-in unprivileged user.
USER node

EXPOSE 3000

CMD ["serve", "-s", ".", "-l", "3000"]
