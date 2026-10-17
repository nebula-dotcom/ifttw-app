"""Fallback screen: if the main server fails to start, show the cause in the app screen (WebView)."""
import html
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 5000


def show(where, err):
    text = f'[{where}]\n\n{err}\n\nPython {sys.version}'
    try:
        path = os.path.join(os.environ.get('ANDROID_PRIVATE') or '.', 'error.log')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S') + '\n' + text + '\n\n')
    except Exception:
        pass
    page = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            '<body style="background:#101010;color:#fff;font:13px monospace;padding:16px">'
            '<h3>IFTTW failed to start</h3><p>Take a screenshot of this screen and send it to the developer.</p>'
            '<pre style="white-space:pre-wrap;word-break:break-word">'
            + html.escape(text) + '</pre></body></html>').encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass

    try:
        ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever(poll_interval=3600)
    except OSError:                                    # the port is already in use
        pass
