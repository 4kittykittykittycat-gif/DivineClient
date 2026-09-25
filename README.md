# DivineSMP Web Client: Hosting and Relay Guide

This folder puts the DivineSMP client on a website. Players open a link, click **Play**, and they're in. They can also install it as an app.

It sets up two things:

1. **The web client.** These are the files in this folder: `index.html`, `manifest.webmanifest`, `sw.js`, `version.json` and `icons/`.
2. **Your own relay.** This is `relay/dsmp_relay.py`. Browsers can't open Minecraft connections by themselves, so the client connects through a small relay. Right now it uses a free public relay (anura.pro). Running your own relay has three benefits:
   - Lower ping.
   - Nobody else's server in the path.
   - It only connects to DivineSMP, so strangers can't use it as a free proxy.

When the client is served from a website, it looks for a relay on the same site at `/wisp/`. If it finds one, it uses it automatically, and players don't need to change any settings. If there's no relay there, it falls back to the public one.

---

## What you need

- A Linux machine you can run commands on. The one running Velocity is best, because players then connect locally.
- A subdomain pointing at that machine, e.g. **`client.divinesmp.org`**. Add an `A` record at your DNS provider with the machine's IP.
  - Use a new subdomain. Don't use `play.divinesmp.org`: it may go through TCPShield or another DDoS filter that only passes Minecraft traffic.
- Ports **80 and 443** open on that machine, for the website and HTTPS.
- Python 3.8 or newer (`python3 --version`). No extra packages are needed.

> **Only have a Minecraft host panel (Pterodactyl etc.) and no Linux access?** Rent the cheapest VPS you can find (1 GB RAM is plenty) and do everything below on it. In the relay's `--route`, use your server's real address instead of `127.0.0.1:25565`.

---

## Step 1: Upload the client

Copy this whole folder to the machine, for example to `/var/www/divinesmp-web`:

```bash
sudo mkdir -p /var/www/divinesmp-web
sudo cp -r index.html manifest.webmanifest sw.js version.json icons /var/www/divinesmp-web/
```

## Step 2: Start the relay

```bash
sudo mkdir -p /opt/divinesmp-relay
sudo cp relay/dsmp_relay.py /opt/divinesmp-relay/
# try it once in the foreground (Ctrl+C to stop):
python3 /opt/divinesmp-relay/dsmp_relay.py --route "divinesmp.org=127.0.0.1:25565" --route "*.divinesmp.org=127.0.0.1:25565"
```

`127.0.0.1:25565` is where **Velocity** listens. Check the `bind = ` line in `velocity.toml` and use that port.

- **If `velocity.toml` has `haproxy-protocol = true`** (common with TCPShield or HAProxy), add `--proxy-protocol` to the command. Otherwise Velocity will reject the relay. This also means Velocity sees each web player's real IP.
- **If Velocity runs on a different machine,** replace `127.0.0.1` with that machine's IP.

When it looks right, make it run forever as a service:

```bash
sudo cp relay/divinesmp-relay.service /etc/systemd/system/
# (edit the ExecStart line in that file if you changed the port or need --proxy-protocol)
sudo systemctl daemon-reload
sudo systemctl enable --now divinesmp-relay
journalctl -u divinesmp-relay -f     # watch players connect
```

## Step 3: HTTPS with Caddy (easiest)

Caddy gets and renews the HTTPS certificate for you.

```bash
sudo apt install -y caddy          # Debian/Ubuntu; see caddyserver.com/docs/install for others
sudo cp relay/Caddyfile /etc/caddy/Caddyfile
# edit the first line if your subdomain isn't client.divinesmp.org
sudo systemctl reload caddy
```

Open **https://client.divinesmp.org**. The DivineSMP title screen should appear. Then:

1. Click **Play DivineSMP**.
2. Run `journalctl -u divinesmp-relay` and you should see a line like `connected to play.divinesmp.org:25565 -> 127.0.0.1:25565`.

*(Already use nginx? Use `relay/nginx-divinesmp.conf` instead of Caddy, and get a certificate with `certbot --nginx -d client.divinesmp.org`.)*

## Step 4: Link it from your website

On divinesmp.org, add a big **"Play in your browser"** button that links to `https://client.divinesmp.org`.

If you'd rather serve the client from `divinesmp.org/play` itself, upload the files there instead. Then open `index.html`, find this line and put your relay's address in it:

```html
<meta name="dsmp-wisp" content="auto">
```
becomes
```html
<meta name="dsmp-wisp" content="wss://client.divinesmp.org/wisp/">
```

---

## Updating the client

When you get a new `DivineSMP.html`, rebuild or copy it over `index.html` on the server. Then write a new build number into `version.json`, e.g. `{"build": "2026-10-01", "notes": "New boss bars!"}`.

- **Players with the page open** get a small "A new version is ready, Reload" message. It never appears mid-fight, only in menus.
- **Everyone else** gets the new version the next time they open the page.

## Installing as an app

In Chrome, Edge and on Chromebooks, players see an **Install DivineSMP App** button on the title screen, or the install icon in the address bar. The app:

- opens full screen, without browser tabs;
- has the DivineSMP icon in the launcher and shelf;
- still opens when offline, although you need internet to play.

---

## Good to know

- **Player IPs.** Without `--proxy-protocol`, every web player reaches Velocity from `127.0.0.1` (or from the relay's IP). That matters for:
  - IP bans;
  - "max accounts per IP" anti-bot rules;
  - alt detectors.

  Players on the public relay already all share anura.pro's IP, so this is no worse. For real IPs, use `--proxy-protocol` together with HAProxy/TCPShield-style setups. The relay log (`journalctl -u divinesmp-relay`) also shows each player's real IP.
- **Safety.** The relay only connects to the addresses you list with `--route`, plus Mojang's skin and sound download servers. Everything else is refused, so it can't be abused as an open proxy. It also limits each IP to 6 tabs and each tab to 6 connections.
- **Offline-mode names only.** The client never asks for Microsoft or Minecraft account logins or tokens. Players pick a username, the same as today.
- **Testing on your own PC.** Run `python3 relay/dsmp_relay.py --static . --route "*.divinesmp.org=play.divinesmp.org:25565"`, then open `http://127.0.0.1:6001`.
