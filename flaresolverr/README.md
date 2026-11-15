# FlareSolverr for Home Assistant OS

Internal [FlareSolverr](https://github.com/Flaresolverr/Flaresolverr) add-on used by the
[Grok2API](https://github.com/chenyme/grok2api) Home Assistant add-on to solve Cloudflare
challenges for Grok Web sessions.

## Purpose

Grok Web (`https://grok.com`) sits behind Cloudflare bot protection. Grok2API obtains a
browser session (SSO token + `cf_clearance` cookie + browser user agent) either from the
stored account cookies or by solving the challenge through FlareSolverr.

This add-on runs the official FlareSolverr image:

```bash
docker run -d --name flaresolverr -p 8191:8191 \
    -e LOG_LEVEL=info -e CAPTCHA_SOLVER=none \
    ghcr.io/flaresolverr/flaresolverr:v3.3.25
```

## Network exposure

- Container port: `8191`
- Published host port: `8191` (Home Assistant OS add-on port mapping, LAN local use)
- **Not exposed to the public Internet.** Do not add NAT/port-forwarding rules or host
  port publishing beyond the Supervisor-managed local mapping.
- Grok2API reaches this add-on via the Home Assistant OS host address, e.g.
  `http://192.168.1.105:8191`.

## Configuration options

| Option | Default | Description |
| --- | --- | --- |
| `log_level` | `info` | FlareSolverr log level (debug/info/...). |
| `captcha_solver` | `none` | CAPTCHA solver backend; keep `none` for Grok Web clearance. |

## Integration with Grok2API

Grok2API runtime settings (via Grok2API Web UI / admin API):

```json
{
  "providerWeb": {
    "clearanceMode": "flaresolverr",
    "flareSolverrURL": "http://192.168.1.105:8191",
    "clearanceTimeout": "1m",
    "clearanceRefresh": "10m"
  }
}
```

After changing these values Grok2API picks them up without restart (runtime settings are
applied live); a restart of the Grok2API add-on may still be required by Supervisor for the
changed process to fully reload. Clear rollback: set `clearanceMode` back to `manual`.

## Troubleshooting

- Check add-on status/logs in Home Assistant → Settings → Devices & Services → Add-ons.
- Check that host port `8191` is free and reachable from the Grok2API container.
- Grok2API logs will show clearance refresh failures if the proxy is unreachable.
