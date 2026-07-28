#!/usr/bin/env python3.11
"""TLS reverse proxy: 8443 (HTTPS, Let's Encrypt IP cert) -> 127.0.0.1:8790 (console).

Only two paths need to be public:
  /oauth/x/callback  (X OAuth callback, no admin cookie)
  /api/x-write/*     (admin-session + CSRF BFF, already enforced by the console)
Everything else (/, /static/*, /login, /api/x/*) is also proxied so the operator
can open the full console over HTTPS in a browser.

Cert reloads on every connection (short-lived 6.67d IP cert renews frequently),
so renewals take effect without restarting this service.
"""
import http.server
import ssl
import socket
import urllib.request
import urllib.error
import urllib.parse
import logging
import os
import sys

CERT_DIR = "/etc/letsencrypt/live/124.221.229.187"
CERT = os.path.join(CERT_DIR, "fullchain.pem")
KEY = os.path.join(CERT_DIR, "privkey.pem")
UPSTREAM = "http://127.0.0.1:8790"
LISTEN = ("0.0.0.0", 8443)
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("x-gateway")


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version = "x-gateway"
    sys_version = ""

    def _proxy(self, method):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path
        url = UPSTREAM + path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP
                   and k.lower() != "host"}
        headers["Host"] = "127.0.0.1:8790"
        req = urllib.request.Request(url, data=body if method != "GET" else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read(50 * 1024 * 1024)
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in HOP_BY_HOP or k.lower() == "transfer-encoding":
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            payload = e.read(50 * 1024 * 1024)
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() in HOP_BY_HOP or k.lower() == "transfer-encoding":
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            LOG.warning("upstream error %s %s: %s", method, path, e)
            payload = b'{"ok":false,"error":{"code":"gateway_unavailable","message":"console unavailable"}}'
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def do_GET(self): self._proxy("GET")
    def do_POST(self): self._proxy("POST")
    def do_HEAD(self): self._proxy("HEAD")
    def do_OPTIONS(self): self._proxy("OPTIONS")

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args if args else fmt)


class RenewalAwareSSLServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        # Re-read the cert/key on every accept so renewals apply without restart.
        sock, addr = self.socket.accept()
        try:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(CERT, KEY)
            ssl_sock = ctx.wrap_socket(sock, server_side=True)
            return ssl_sock, addr
        except Exception as e:
            LOG.error("TLS handshake failed from %s: %s", addr, e)
            try: sock.close()
            except Exception: pass
            raise


def main():
    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        print(f"certificate not found: {CERT}", file=sys.stderr)
        sys.exit(1)
    server = RenewalAwareSSLServer(LISTEN, ProxyHandler)
    LOG.info("x-gateway listening on %s:%s -> %s", *LISTEN, UPSTREAM)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
