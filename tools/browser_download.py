"""Receive one Quilter ZIP on loopback, without the browser's Save dialog.

Run with --out <new.zip>, then execute the printed code in the authenticated
browser automation page before clicking Complete Board / Download normally.
No browser credentials or signed download URLs leave the browser.
"""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
from pathlib import Path
import secrets
import time
import zipfile

ORIGIN = "https://app.quilter.ai"
MAX_BYTES = 50_000_000


def validate_archive(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_BYTES:
            raise ValueError("Unpacked candidate exceeds size limit")
        if len([name for name in archive.namelist() if name.endswith(".kicad_pcb")]) != 1:
            raise ValueError("Expected exactly one candidate PCB")
        if archive.testzip() is not None:
            raise ValueError("Corrupt candidate archive")


def browser_code(endpoint):
    return """await page.evaluate(() => {
  const create = URL.createObjectURL;
  const click = HTMLAnchorElement.prototype.click;
  const urls = new Set();
  const restore = () => {
    URL.createObjectURL = create;
    HTMLAnchorElement.prototype.click = click;
  };
  window.__quilterDownload = {status: 'waiting', cancel: restore};
  URL.createObjectURL = function(file) {
    const url = create.call(URL, file);
    if (file instanceof Blob && file.type === 'application/zip') {
      urls.add(url);
      window.__quilterDownload.status = 'saving';
      fetch(ENDPOINT, {
        method: 'POST', headers: {'Content-Type': 'application/zip'}, body: file
      }).then(async response => {
        const result = await response.text();
        window.__quilterDownload = {
          status: response.ok ? 'saved' : 'error', result, cancel: restore
        };
      }).catch(error => {
        window.__quilterDownload = {status: 'error', result: String(error), cancel: restore};
      }).finally(restore);
    }
    return url;
  };
  HTMLAnchorElement.prototype.click = function() {
    if (this.download && urls.has(this.href)) return;
    return click.call(this);
  };
  return {installed: true};
});""".replace("ENDPOINT", json.dumps(endpoint))


def receive(out, port, timeout):
    token = "/" + secrets.token_urlsafe(24)
    saved = False

    class Handler(BaseHTTPRequestHandler):
        def cors(self):
            self.send_header("Access-Control-Allow-Origin", ORIGIN)
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ready")

        def do_OPTIONS(self):
            self.send_response(204)
            self.cors()
            self.end_headers()

        def do_POST(self):
            nonlocal saved
            if self.headers.get("Origin") != ORIGIN or self.path != token:
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if not 0 < size <= MAX_BYTES:
                self.send_error(413)
                return
            data = self.rfile.read(size)
            try:
                validate_archive(data)
                with out.open("xb") as target:
                    target.write(data)
            except (zipfile.BadZipFile, ValueError, OSError) as error:
                self.send_response(400)
                self.cors()
                self.end_headers()
                self.wfile.write(str(error).encode())
                return
            saved = True
            result = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            self.send_response(200)
            self.cors()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            print("SAVED", out, json.dumps(result), flush=True)

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        server.timeout = 1
        endpoint = f"http://127.0.0.1:{server.server_port}{token}"
        print("READY; execute in the browser automation tool:\n" + browser_code(endpoint), flush=True)
        end = time.monotonic() + timeout
        while not saved and time.monotonic() < end:
            server.handle_request()
    if not saved:
        raise TimeoutError("No candidate received; restore browser hooks with __quilterDownload.cancel()")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("--out must be a new file")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    receive(args.out, args.port, args.timeout)


if __name__ == "__main__":
    main()
