"""A localhost page that runs ingest on a folder path.

The form posts paths and a brief. It has no file input. Video bytes are not
accepted. The process reads the folder on disk and prints where the FCPXML went.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .errors import ConductorError
from .ingest import ingest

HOST = "127.0.0.1"
_MAX_BODY = 64 * 1024


class UIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class App:
    def __init__(self) -> None:
        self.files: dict[str, Path] = {}

    def register(self, path: Path) -> str:
        resolved = path.resolve()
        token = hashlib.blake2b(str(resolved).encode(), digest_size=8).hexdigest()
        self.files[token] = resolved
        return token


def serve(port: int = 8765) -> None:
    """Listen on 127.0.0.1 until Ctrl-C. Other hosts are not bound."""
    if port < 1 or port > 65535:
        raise ConductorError(f"port {port} is not valid")
    server = make_server(port)
    url = f"http://{HOST}:{server.server_address[1]}"
    print(f"cut-conductor: ingest page at {url}")
    print("  Type a folder path, or drop a file:// path. Video is not uploaded.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ncut-conductor: stopped")
    finally:
        server.server_close()


def make_server(port: int = 0) -> ThreadingHTTPServer:
    app = App()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                _send(self, 200, "text/html; charset=utf-8", page_html().encode("utf-8"))
                return
            if parsed.path == "/pick":
                chosen = pick_directory()
                if chosen:
                    body = _json({"path": chosen})
                else:
                    body = _json(
                        {
                            "error": (
                                "This machine did not open a folder dialog. "
                                "Type the absolute path. Nothing was uploaded."
                            )
                        }
                    )
                _send(self, 200, "application/json", body)
                return
            prefix = "/download/"
            if parsed.path.startswith(prefix):
                token = parsed.path[len(prefix) :]
                target = app.files.get(token)
                if target is None or not target.is_file():
                    _send(self, 404, "text/plain; charset=utf-8", b"not found\n")
                    return
                data = target.read_bytes()
                _send(self, 200, _content_type(target), data, download=target.name)
                return
            _send(self, 404, "text/plain; charset=utf-8", b"not found\n")

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/ingest":
                _send(self, 404, "text/plain; charset=utf-8", b"not found\n")
                return
            try:
                form = _read_form(self)
                result = _run(form)
            except UIError as exc:
                _send(self, exc.status, "text/html; charset=utf-8", _error_page(exc.message))
                return
            except ConductorError as exc:
                _send(self, 400, "text/html; charset=utf-8", _error_page(str(exc)))
                return
            links = []
            for label, path in _outputs(result):
                token = app.register(path)
                links.append((label, path, f"/download/{token}"))
            _send(self, 200, "text/html; charset=utf-8", _result_page(result, links))

        def log_message(self, fmt: str, *args) -> None:
            return

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True

    return Server((HOST, port), Handler)


def page_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>jevid ingest</title>
<style>
  body { font: 16px/1.45 ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 40rem; color: #1c1c1c; }
  h1 { font-size: 1.4rem; }
  label { display: block; margin-top: 1rem; font-weight: 600; }
  input[type=text], textarea { width: 100%; box-sizing: border-box; font: inherit; padding: 0.4rem; }
  textarea { min-height: 6rem; }
  button { font: inherit; margin-top: 1rem; padding: 0.4rem 0.8rem; }
  #drop { margin-top: 1rem; padding: 1rem; border: 1px dashed #888; background: #f6f6f6; }
  .note { color: #444; }
  .warn { color: #7a2e0e; }
</style>
</head>
<body>
<h1>Clips to FCPXML</h1>
<p class="note">Point this at a folder on this machine. The page sends the path, the brief, and nothing else. Video is not uploaded. Final Cut is not controlled.</p>
<div id="drop">Drop a <code>file://</code> path here, or type the folder below. A dropped clip uses its parent folder. The browser is not asked for the file bytes.</div>
<p id="drop-note" class="warn"></p>
<form method="post" action="/ingest">
  <label for="media">Folder path</label>
  <input id="media" name="media" type="text" required placeholder="/absolute/path/to/clips" autocomplete="off">
  <button type="button" id="choose">Choose folder…</button>
  <label for="brief">Brief</label>
  <textarea id="brief" name="brief" placeholder="Assemble these clips in filename order. Keep what serves the story, lose dead air."></textarea>
  <label for="transcript">Transcript path (optional, SRT or WebVTT)</label>
  <input id="transcript" name="transcript" type="text" autocomplete="off">
  <label for="taste">Taste JSON path (optional)</label>
  <input id="taste" name="taste" type="text" autocomplete="off">
  <label for="durations">Durations JSON path (optional)</label>
  <input id="durations" name="durations" type="text" autocomplete="off">
  <label for="out_dir">Output directory</label>
  <input id="out_dir" name="out_dir" type="text" value="out/ingest">
  <label><input name="apply" type="checkbox" value="1"> Also apply high-confidence mechanical cuts (confidence at least 0.80, mechanical pass only). Writes a separate applied FCPXML. Does not change the clips.</label>
  <button type="submit">Build FCPXML</button>
</form>
<script>
const media = document.getElementById("media");
const note = document.getElementById("drop-note");
const drop = document.getElementById("drop");
drop.addEventListener("dragover", (event) => { event.preventDefault(); });
drop.addEventListener("drop", (event) => {
  event.preventDefault();
  const uri = event.dataTransfer.getData("text/uri-list");
  const text = event.dataTransfer.getData("text/plain");
  const chosen = firstPath(uri) || firstPath(text);
  if (!chosen) {
    note.textContent = "The browser did not share a folder path, and nothing was uploaded. Type the absolute path.";
    return;
  }
  media.value = chosen;
  note.textContent = "";
});
document.getElementById("choose").addEventListener("click", async () => {
  note.textContent = "";
  const response = await fetch("/pick");
  const body = await response.json();
  if (body.path) {
    media.value = body.path;
    return;
  }
  note.textContent = body.error || "Type the absolute path.";
});
function firstPath(raw) {
  if (!raw) return "";
  const line = raw.split(/\\r?\\n/).map((item) => item.trim()).find((item) => item && !item.startsWith("#"));
  if (!line) return "";
  let path = "";
  if (line.startsWith("file://")) path = decodeURIComponent(line.slice("file://".length));
  else if (line.startsWith("/")) path = line;
  if (!path) return "";
  const slash = path.lastIndexOf("/");
  const base = slash >= 0 ? path.slice(slash + 1) : path;
  if (/\\.(mov|mp4|m4v|mxf|avi|mkv|mts|m2ts)$/i.test(base)) {
    return slash > 0 ? path.slice(0, slash) : path;
  }
  return path;
}
</script>
</body>
</html>
"""


def pick_directory() -> str | None:
    """Ask the OS for a folder. Returns None when there is no display or no dialog."""
    if sys.platform == "linux" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return None
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "try:\n"
        "    root.attributes('-topmost', True)\n"
        "except tk.TclError:\n"
        "    pass\n"
        "path = filedialog.askdirectory()\n"
        "print(path or '')\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = completed.stdout.strip()
    return path or None


def _run(form: dict[str, str]):
    media = _blank(form.get("media"))
    if not media:
        raise ConductorError("a folder path is required")
    apply = form.get("apply") == "1"
    accept = _accept(form.get("accept"))
    if apply and accept:
        return ingest(
            media,
            brief=_blank(form.get("brief")),
            transcript_path=_blank(form.get("transcript")),
            taste_path=_blank(form.get("taste")),
            durations_path=_blank(form.get("durations")),
            out_dir=_blank(form.get("out_dir")) or "out/ingest",
            apply=True,
            accept=accept,
        )
    if apply:
        raw = _blank(form.get("min_confidence")) or "0.8"
        try:
            floor = float(raw)
        except ValueError as exc:
            raise ConductorError(f"min confidence is not a number: {raw}") from exc
        passes = [_blank(form.get("pass")) or "mechanical"]
        return ingest(
            media,
            brief=_blank(form.get("brief")),
            transcript_path=_blank(form.get("transcript")),
            taste_path=_blank(form.get("taste")),
            durations_path=_blank(form.get("durations")),
            out_dir=_blank(form.get("out_dir")) or "out/ingest",
            apply=True,
            min_confidence=floor,
            passes=passes,
        )
    return ingest(
        media,
        brief=_blank(form.get("brief")),
        transcript_path=_blank(form.get("transcript")),
        taste_path=_blank(form.get("taste")),
        durations_path=_blank(form.get("durations")),
        out_dir=_blank(form.get("out_dir")) or "out/ingest",
    )


def _outputs(result) -> list[tuple[str, Path]]:
    rows = [("Starter FCPXML", result.starter)]
    report = result.report
    if report.out_fcpxml:
        rows.append(("Shadow FCPXML (markers)", report.out_fcpxml))
    if report.out_applied:
        rows.append(("Applied FCPXML", report.out_applied))
    if report.out_markdown:
        rows.append(("Report", report.out_markdown))
    if report.out_json:
        rows.append(("JSON", report.out_json))
    return rows


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length > _MAX_BODY:
        raise UIError(413, "This page accepts a folder path and a brief, not media bytes.")
    raw = handler.rfile.read(length) if length else b""
    ctype = handler.headers.get("Content-Type") or ""
    if "multipart/" in ctype.lower():
        raise UIError(400, "This page does not accept file uploads. Type the folder path.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UIError(400, "The form was not UTF-8 text.") from exc
    parsed = parse_qs(text, keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


def _result_page(result, links: list[tuple[str, Path, str]]) -> bytes:
    from html import escape

    items = []
    for label, path, href in links:
        items.append(
            "<li>"
            f"<strong>{escape(label)}</strong><br>"
            f"<code>{escape(str(path.resolve()))}</code><br>"
            f'<a href="{escape(href)}">Download</a>'
            "</li>"
        )
    warnings = "".join(f"<li>{escape(item)}</li>" for item in result.warnings)
    warning_block = f"<h2>Duration limits</h2><ul>{warnings}</ul>" if warnings else ""
    body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>FCPXML ready</title></head>
<body style="font: 16px/1.45 ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 40rem;">
<h1>FCPXML ready</h1>
<p>Open the starter or the shadow file in Final Cut with File → Import → XML. Import creates a new event. It does not edit the clips.</p>
<p>{escape(result.report.payload["ingest"]["relink"])}</p>
<ul>
{"".join(items)}
</ul>
{warning_block}
<p><a href="/">Back</a></p>
</body>
</html>
"""
    return body.encode("utf-8")


def _error_page(message: str) -> bytes:
    from html import escape

    body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Ingest failed</title></head>
<body style="font: 16px/1.45 ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 40rem;">
<h1>Ingest failed</h1>
<p>{escape(message)}</p>
<p><a href="/">Back</a></p>
</body>
</html>
"""
    return body.encode("utf-8")


def _send(
    handler: BaseHTTPRequestHandler,
    status: int,
    content_type: str,
    body: bytes,
    download: str | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if download:
        handler.send_header("Content-Disposition", f'attachment; filename="{download}"')
    handler.end_headers()
    handler.wfile.write(body)


def _content_type(path: Path) -> str:
    if path.suffix == ".fcpxml":
        return "application/xml"
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".md":
        return "text/markdown; charset=utf-8"
    return "text/plain; charset=utf-8"


def _json(payload: dict) -> bytes:
    import json

    return (json.dumps(payload) + "\n").encode("utf-8")


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _accept(value: str | None) -> list[str] | None:
    text = _blank(value)
    if not text:
        return None
    ids = [part.strip() for part in text.split(",") if part.strip()]
    return ids or None
