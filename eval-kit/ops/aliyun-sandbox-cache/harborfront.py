#!/usr/bin/env python3
"""Local TLS front for harbor.tokenwave.us (docker treats it as insecure registry, so a self-signed cert is fine).
Routes /v2/release/cowork-bench/NNN-* to the shared pull-through cache that owns row NNN; everything else to real Harbor."""
import http.client,json,os,re,socket,ssl,sys,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROWMAP=json.load(open(os.environ.get('HARBORFRONT_ROWMAP','/usr/local/etc/rowmap.json')))
REAL=sys.argv[1]           # real harbor IP
ctx_up=ssl.create_default_context()
RX=re.compile(r'^/v2/release/cowork-bench/(\d{3})-')
HOP={'connection','keep-alive','transfer-encoding','te','trailer','upgrade','proxy-authorization','proxy-authenticate'}
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a): pass
    def route(self):
        m=RX.match(self.path)
        if m and str(int(m.group(1))) in ROWMAP: return ROWMAP[str(int(m.group(1)))]['cache'],None
        if self.path in ('/v2','/v2/'): return list(ROWMAP.values())[0]['cache'],None
        return 'harbor.tokenwave.us',REAL
    def go(self):
        host,ip=self.route()
        n=int(self.headers.get('Content-Length') or 0); body=self.rfile.read(n) if n else None
        # keep repeated headers (docker sends one Accept line per manifest media type)
        hdr=[(k,v) for k,v in self.headers.items() if k.lower() not in HOP and k.lower() not in ('host','accept')]
        acc=self.headers.get_all('Accept') or []
        if acc: hdr.append(('Accept',', '.join(acc)))
        if host!='harbor.tokenwave.us': hdr=[(k,v) for k,v in hdr if k.lower()!='authorization']
        c=http.client.HTTPSConnection(ip or host,443,timeout=600,context=ctx_up)
        if ip: c.sock=ctx_up.wrap_socket(socket.create_connection((ip,443),timeout=600),server_hostname=host)
        c.putrequest(self.command,self.path,skip_host=True,skip_accept_encoding=True); c.putheader('Host',host)
        for k,v in hdr: c.putheader(k,v)
        if body is not None: c.putheader('Content-Length',str(len(body)))
        c.endheaders(body)
        r=c.getresponse()
        self.send_response(r.status)
        cl=None
        for k,v in r.getheaders():
            if k.lower() in HOP: continue
            if k.lower()=='content-length': cl=v
            if k.lower()=='location' and host!='harbor.tokenwave.us': v=v.replace('https://'+host,'https://harbor.tokenwave.us')
            self.send_header(k,v)
        if cl is None and self.command!='HEAD' and r.status not in (204,304): self.send_header('Connection','close'); self.close_connection=True
        self.end_headers()
        if self.command=='HEAD': r.close(); c.close(); return
        while True:
            b=r.read(1<<20)
            if not b: break
            self.wfile.write(b)
        c.close()
    do_GET=do_HEAD=do_POST=do_PUT=do_PATCH=do_DELETE=go
srv=ThreadingHTTPServer(('127.0.0.1',443),H); srv.daemon_threads=True
cx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); cx.load_cert_chain('/etc/harborfront/crt.pem','/etc/harborfront/key.pem')
srv.socket=cx.wrap_socket(srv.socket,server_side=True)
srv.serve_forever()
