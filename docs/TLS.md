# Putting the stack behind HTTPS

The stack listens on plain HTTP on port 8099. Everything the browser and the
tray app send — your password at sign-in, the session token on every request,
and the `X-API-Key` header on every lap upload — crosses the network
unencrypted.

On a home LAN that is a contained risk: anyone who could read that traffic is
already inside your network. It stops being contained the moment the port is
reachable from the internet. **Do not port-forward 8099.** If you want to
reach your lap times from outside the house, put TLS in front of it.

Two approaches, both leaving the compose stack unchanged.

## A VPN (simplest, no certificate)

Run WireGuard or Tailscale on the server and connect from outside. Nothing is
exposed publicly, the traffic is encrypted by the tunnel, and there is no
certificate to renew. If you only need access for yourself and a few friends,
stop here — this is the least that can go wrong.

## A reverse proxy with a certificate

If you want a real hostname, put Caddy in front. It obtains and renews a
Let's Encrypt certificate on its own. Point a DNS record at your server,
forward 80 and 443 to it, and leave 8099 closed to the outside:

```caddyfile
# /etc/caddy/Caddyfile
laps.example.com {
    reverse_proxy localhost:8099
}
```

Traefik and nginx-proxy-manager do the same job if you already run one.

Once every request genuinely arrives over HTTPS, uncomment the
`Strict-Transport-Security` header at the bottom of
`ace-laptimes/nginx/nginx.conf` and rebuild the nginx image. Do it only then:
the header is ignored over plain HTTP, and a browser that has seen it will
refuse to use HTTP for that hostname for a year, which is awkward to undo.

## Trusted proxy hops

The backend reads the client address from `X-Forwarded-For`, trusting exactly
one hop — the bundled nginx. Adding Caddy or Traefik in front makes two, so
set `TRUSTED_PROXY_HOPS=2` on the backend service. Getting this wrong means
rate limits and the admin Connected Clients panel see the proxy's address
instead of the real client's.

## The tray app

Point it at the HTTPS URL once you have one. It warns when configured against
a plain-HTTP address that is not on your own machine, because the API key it
stores is sent on every upload.
