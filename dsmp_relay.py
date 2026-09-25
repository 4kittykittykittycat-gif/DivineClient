#!/usr/bin/env python3
"""
DivineSMP web relay: a small Wisp server for the DivineSMP browser client.

The browser can't open raw TCP connections, so the client talks to this relay
over a WebSocket (the Wisp protocol) and the relay opens the Minecraft
connection for it. Unlike a public relay it only connects to YOUR server
(and, if you allow it, Mojang's skin/sound download hosts), so nobody can use
it as a free proxy.

No extra packages needed: Python 3.8+ standard library only.

  python3 dsmp_relay.py                         # 127.0.0.1:6001, DivineSMP routes
  python3 dsmp_relay.py --port 6001 --route "*.divinesmp.org=127.0.0.1:25565"
  python3 dsmp_relay.py --static ../            # also serve the web client (local testing)

Put Caddy or nginx in front of it for HTTPS (see README.md): the client
expects wss://YOUR-SITE/wisp/.
"""
import argparse, asyncio, base64, fnmatch, hashlib, ipaddress, mimetypes, os, struct, sys, time

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MOJANG_HOSTS = ['textures.minecraft.net', 'resources.download.minecraft.net', 'piston-meta.mojang.com',
                'piston-data.mojang.com', 'launchermeta.mojang.com', 'launcher.mojang.com', 'libraries.minecraft.net']
# Wisp packet types and close reasons
CONNECT, DATA, CONTINUE, CLOSE, INFO = 1, 2, 3, 4, 5
R_VOLUNTARY, R_NETERR, R_INVALID, R_UNREACHABLE, R_TIMEOUT, R_REFUSED, R_BLOCKED = 0x02, 0x03, 0x41, 0x42, 0x43, 0x44, 0x48
BUF = 128              # packets the client may send before waiting for CONTINUE

def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)

class Config:
    def __init__(self, a):
        self.routes = []                                    # (pattern, host, port or None)
        for r in a.route:
            if '=' not in r: sys.exit('bad --route %r (want name=host:port)' % r)
            pat, dst = r.split('=', 1)
            h, _, p = dst.rpartition(':')
            if not h: h, p = p, ''
            self.routes.append((pat.lower().strip(), h.strip(), int(p) if p else None))
        self.mojang = not a.no_mojang
        self.max_streams = a.max_streams
        self.max_per_ip = a.max_per_ip
        self.proxy_protocol = a.proxy_protocol
        self.trust_proxy = not a.no_trust_proxy
        self.static = os.path.abspath(a.static) if a.static else None
        self.path = '/' + a.path.strip('/') + '/' if a.path.strip('/') else '/'

    def target(self, host, port):
        """Where a CONNECT to host:port may go, or None when it's not allowed."""
        h = host.lower().rstrip('.')
        for pat, th, tp in self.routes:
            key = h + ':' + str(port)
            if fnmatch.fnmatch(key, pat) or (':' not in pat and fnmatch.fnmatch(h, pat)):
                return th, (tp if tp else port)
        if self.mojang and h in MOJANG_HOSTS and port in (80, 443):
            return h, port
        return None

CFG = None
PER_IP = {}

# ---------------------------------------------------------------- websocket
async def read_frame(r):
    """One complete message (reassembling fragments): (opcode, payload)."""
    op0, parts = None, []
    while True:
        h = await r.readexactly(2)
        fin, op, masked, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x80, h[1] & 0x7F
        if n == 126: n = struct.unpack('>H', await r.readexactly(2))[0]
        elif n == 127: n = struct.unpack('>Q', await r.readexactly(8))[0]
        if n > (1 << 21): raise ConnectionError('frame too big')
        mask = await r.readexactly(4) if masked else None
        data = await r.readexactly(n)
        if mask:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data)) if n < 64 else _unmask(data, mask)
        if op >= 8:                                          # control frames can arrive between fragments
            return op, data
        if op != 0: op0 = op
        parts.append(data)
        if fin: return op0, b''.join(parts)

def _unmask(data, mask):
    n = len(data)
    m = int.from_bytes((mask * ((n // 4) + 1))[:n], 'big')
    return (int.from_bytes(data, 'big') ^ m).to_bytes(n, 'big')

def frame(op, payload):
    n = len(payload)
    if n < 126: h = bytes([0x80 | op, n])
    elif n < 65536: h = bytes([0x80 | op, 126]) + struct.pack('>H', n)
    else: h = bytes([0x80 | op, 127]) + struct.pack('>Q', n)
    return h + payload

# ---------------------------------------------------------------- one browser
class Session:
    def __init__(self, reader, writer, ip):
        self.r, self.w, self.ip = reader, writer, ip
        self.streams = {}                                   # sid -> [tcp writer, packets since CONTINUE, task]
        self.lock = asyncio.Lock()

    async def send(self, t, sid, payload=b''):
        async with self.lock:
            self.w.write(frame(2, bytes([t]) + struct.pack('<I', sid) + payload))
            await self.w.drain()

    async def run(self):
        await self.send(CONTINUE, 0, struct.pack('<I', BUF))
        try:
            while True:
                op, data = await read_frame(self.r)
                if op == 8: break
                if op == 9:
                    async with self.lock: self.w.write(frame(10, data)); await self.w.drain()
                    continue
                if op != 2 or len(data) < 5: continue
                t, sid, pl = data[0], struct.unpack('<I', data[1:5])[0], data[5:]
                if t == CONNECT: await self.connect(sid, pl)
                elif t == DATA: await self.data(sid, pl)
                elif t == CLOSE: self.close_stream(sid)
                elif t == INFO: pass                        # we answer as a Wisp v1 server
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            for sid in list(self.streams): self.close_stream(sid)

    async def connect(self, sid, pl):
        if len(pl) < 3 or sid in self.streams: return
        kind, port, host = pl[0], struct.unpack('<H', pl[1:3])[0], pl[3:].decode('utf-8', 'replace')
        if kind != 1:
            await self.send(CLOSE, sid, bytes([R_INVALID])); return
        if len(self.streams) >= CFG.max_streams:
            await self.send(CLOSE, sid, bytes([R_BLOCKED])); return
        dst = CFG.target(host, port)
        if not dst:
            log(self.ip, 'blocked', host, port)
            await self.send(CLOSE, sid, bytes([R_BLOCKED])); return
        try:
            tr, tw = await asyncio.wait_for(asyncio.open_connection(dst[0], dst[1]), 10)
        except asyncio.TimeoutError:
            await self.send(CLOSE, sid, bytes([R_TIMEOUT])); return
        except ConnectionRefusedError:
            await self.send(CLOSE, sid, bytes([R_REFUSED])); return
        except OSError:
            await self.send(CLOSE, sid, bytes([R_UNREACHABLE])); return
        try:
            s = tw.get_extra_info('socket')
            if s is not None:
                import socket
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)     # no Nagle delay: packets go out at once
        except OSError:
            pass
        if CFG.proxy_protocol:
            loc = tw.get_extra_info('sockname') or ('0.0.0.0', 0)
            fam = 'TCP6' if ':' in self.ip else 'TCP4'
            tw.write(('PROXY %s %s %s %d %d\r\n' % (fam, self.ip, loc[0], 0, loc[1])).encode())
        if dst[1] != 80 or host not in MOJANG_HOSTS:
            log(self.ip, 'connected to', host + ':' + str(port), '->', '%s:%d' % dst)
        task = asyncio.ensure_future(self.pump(sid, tr))
        self.streams[sid] = [tw, 0, task]

    async def pump(self, sid, tr):
        why = R_VOLUNTARY
        try:
            while True:
                d = await tr.read(65536)
                if not d: break
                await self.send(DATA, sid, d)
        except (ConnectionError, OSError):
            why = R_NETERR
        if sid in self.streams:
            self.streams.pop(sid)[0].close()
            try: await self.send(CLOSE, sid, bytes([why]))
            except (ConnectionError, OSError): pass

    async def data(self, sid, pl):
        st = self.streams.get(sid)
        if not st: return
        st[0].write(pl)
        try: await st[0].drain()
        except (ConnectionError, OSError): self.close_stream(sid); return
        st[1] += 1
        if st[1] >= BUF // 2:
            st[1] = 0
            await self.send(CONTINUE, sid, struct.pack('<I', BUF))

    def close_stream(self, sid):
        st = self.streams.pop(sid, None)
        if st:
            st[2].cancel()
            try: st[0].close()
            except OSError: pass

# ---------------------------------------------------------------- http
async def handle(reader, writer):
    peer = (writer.get_extra_info('peername') or ('?', 0))[0]
    ip = peer
    try:
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 15)
    except Exception:
        writer.close(); return
    lines = head.decode('latin-1').split('\r\n')
    try: method, path, _ = lines[0].split(' ', 2)
    except ValueError: writer.close(); return
    hd = {}
    for l in lines[1:]:
        if ':' in l: k, v = l.split(':', 1); hd[k.strip().lower()] = v.strip()
    # behind Caddy / nginx on this machine: the real player IP is in X-Forwarded-For
    if CFG.trust_proxy and hd.get('x-forwarded-for'):
        try:
            if ipaddress.ip_address(peer).is_loopback: ip = hd['x-forwarded-for'].split(',')[0].strip()
        except ValueError: pass
    if hd.get('upgrade', '').lower() != 'websocket':
        await http_plain(writer, method, path); return
    if not path.split('?')[0].startswith(CFG.path) and CFG.path != '/':
        writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n'); await writer.drain(); writer.close(); return
    if PER_IP.get(ip, 0) >= CFG.max_per_ip:
        writer.write(b'HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\n\r\n'); await writer.drain(); writer.close(); return
    key = hd.get('sec-websocket-key', '')
    acc = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
    writer.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  'Sec-WebSocket-Accept: %s\r\n\r\n' % acc).encode())
    await writer.drain()
    PER_IP[ip] = PER_IP.get(ip, 0) + 1
    try:
        await Session(reader, writer, ip).run()
    finally:
        PER_IP[ip] -= 1
        if PER_IP[ip] <= 0: PER_IP.pop(ip, None)
        try: writer.close()
        except OSError: pass

async def http_plain(writer, method, path):
    """Health check, or the web client itself with --static (handy for testing)."""
    p = path.split('?')[0]
    body, ctype, code = b'DivineSMP relay is running.\n', 'text/plain', '200 OK'
    if CFG.static and method in ('GET', 'HEAD'):
        rel = os.path.normpath(p.lstrip('/')) if p.strip('/') else 'index.html'
        full = os.path.abspath(os.path.join(CFG.static, rel))
        if full.startswith(CFG.static) and os.path.isfile(full):
            with open(full, 'rb') as f: body = f.read()
            ctype = 'application/manifest+json' if full.endswith('.webmanifest') else (mimetypes.guess_type(full)[0] or 'application/octet-stream')
        elif p not in ('/', '/health'):
            body, ctype, code = b'not found\n', 'text/plain', '404 Not Found'
    writer.write(('HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n'
                  % (code, ctype, len(body))).encode() + (body if method != 'HEAD' else b''))
    try: await writer.drain()
    except OSError: pass
    writer.close()

async def main():
    global CFG
    ap = argparse.ArgumentParser(description='DivineSMP web relay (Wisp server that only connects to your server)')
    ap.add_argument('--host', default='127.0.0.1', help='address to listen on (default 127.0.0.1: only Caddy/nginx on this machine can reach it)')
    ap.add_argument('--port', type=int, default=6001)
    ap.add_argument('--path', default='/wisp/', help='WebSocket path (default /wisp/)')
    ap.add_argument('--route', action='append', default=[], help='NAME[:PORT]=HOST:PORT, e.g. "*.divinesmp.org=127.0.0.1:25565". Repeatable.')
    ap.add_argument('--no-mojang', action='store_true', help="don't allow skin / sound downloads from Mojang through the relay")
    ap.add_argument('--max-streams', type=int, default=6, help='connections one browser tab may open (default 6)')
    ap.add_argument('--max-per-ip', type=int, default=6, help='browser tabs per IP address (default 6)')
    ap.add_argument('--proxy-protocol', action='store_true', help='send a PROXY v1 header with the player IP (only if your proxy expects it!)')
    ap.add_argument('--no-trust-proxy', action='store_true', help='ignore X-Forwarded-For from a local reverse proxy')
    ap.add_argument('--static', default=None, help='also serve the web client files from this folder (for testing)')
    a = ap.parse_args()
    if not a.route:
        a.route = ['divinesmp.org=127.0.0.1:25565', '*.divinesmp.org=127.0.0.1:25565']
    CFG = Config(a)
    srv = await asyncio.start_server(handle, a.host, a.port, limit=1 << 16)
    log('DivineSMP relay on %s:%d%s' % (a.host, a.port, CFG.path))
    for pat, h, p in CFG.routes: log('  route %s -> %s:%s' % (pat, h, p or '(same port)'))
    if CFG.mojang: log('  Mojang skin/sound downloads allowed')
    async with srv:
        await srv.serve_forever()

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
