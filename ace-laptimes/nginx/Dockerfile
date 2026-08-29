FROM nginx:alpine

# The official image already runs its worker processes as the unprivileged
# `nginx` user; only the master stays root, which it needs to bind port 80.
# Requests are handled by the workers, so no USER directive is added here —
# changing it would mean moving off port 80 and breaking every existing
# compose file's port mapping.

RUN rm /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/conf.d/default.conf
