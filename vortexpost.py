"""VortexPost — a local-first, git-native HTTP client (Postman replacement).

Invariants (from the replacement brief):
  1. Offline-first   — no account, no login, no telemetry; the only network
                       traffic is the request the user pressed Send on.
  2. Secrets local   — collections are plain `.http` files on disk; there is
                       no code path that uploads anything, ever.
  3. Team sharing    — the `collections/` folder *is* the sharing mechanism
                       (git, zip, scp — no server component).
  4. Git-native      — files follow the REST-Client `.http` convention:
                       `###` separates requests, `METHOD URL`, headers, blank
                       line, body. Diffs read like code review.
  5. Light           — stdlib `urllib` only + customtkinter UI. No async
                       runtime, no Electron, no background workers.

Run: START_VORTEXPOST.bat
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageTk

COLLECTIONS = Path(__file__).parent / "collections"
SEND_TIMEOUT = 30.0
USER_AGENT = "VortexPost/1.0 (local-first)"

# ------------------------------------------------------------- .http ----
@dataclass
class Request:
    """One parsed request block from a .http file."""
    name: str = "unnamed"
    method: str = "GET"
    url: str = ""
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: str = ""
    source_file: Path | None = None
    index: int = 0  # position among ### blocks in the file


def parse_http(text: str, source: Path | None = None) -> list[Request]:
    """Parse REST-Client style .http text into requests (ownership: pure fn)."""
    requests: list[Request] = []
    # split on ### markers, keeping the marker line as the name carrier
    parts = re.split(r"(?m)^###\s*(.*)$", text)
    # parts[0] = preamble before first ###
    chunks = parts[1:]
    for i in range(0, len(chunks), 2):
        name = chunks[i].strip() or "unnamed"
        block = chunks[i + 1] if i + 1 < len(chunks) else ""
        requests.append(_parse_block(name, block, source, len(requests)))
    if not requests and text.strip():
        requests.append(_parse_block("unnamed", text, source, 0))
    return requests


def _parse_block(name: str, block: str, source: Path | None, idx: int) -> Request:
    lines = block.splitlines()
    # find the METHOD URL line (first non-empty, non-comment, non-variable line)
    start = 0
    method, url = "", ""
    for j, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("@"):
            continue
        m = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)", s, re.I)
        if m:
            method, url = m.group(1).upper(), m.group(2)
            start = j + 1
            break
    req = Request(name=name, method=method or "GET", url=url,
                  source_file=source, index=idx)
    # headers until first blank line
    body_lines: list[str] = []
    in_headers = True
    for ln in lines[start:]:
        s = ln.strip()
        if in_headers and s and ":" in s and not s.startswith("{") and not s.startswith("["):
            k, v = s.split(":", 1)
            req.headers.append((k.strip(), v.strip()))
        elif not s:
            in_headers = False
        else:
            body_lines.append(ln)
    req.body = "\n".join(body_lines).strip()
    return req


def serialize_http(reqs: list[Request]) -> str:
    """Requests back to .http text (round-trip → git-stable formatting)."""
    out: list[str] = []
    for r in reqs:
        out.append(f"### {r.name}")
        out.append(f"{r.method} {r.url}")
        for k, v in r.headers:
            out.append(f"{k}: {v}")
        if r.body:
            out.append("")
            out.append(r.body)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def substitute_variables(text: str, env: dict[str, str]) -> str:
    """Replace {{name}} from local environment (values never leave disk)."""
    def rep(m: re.Match) -> str:
        return env.get(m.group(1), m.group(0))
    return re.sub(r"\{\{(\w+)\}\}", rep, text)


# ------------------------------------------------------------- engine ----
@dataclass
class Response:
    status: int
    reason: str
    ms: int
    size: int
    headers: list[tuple[str, str]]
    body: str
    error: str = ""


def send_request(req: Request, env: dict[str, str] | None = None) -> Response:
    """Execute one request. THE ONLY network call in the whole program."""
    env = env or {}
    url = substitute_variables(req.url, env)
    headers = {k: substitute_variables(v, env) for k, v in req.headers}
    headers.setdefault("User-Agent", USER_AGENT)
    body = substitute_variables(req.body, env).encode("utf-8") if req.body else None

    data = body if req.method in ("POST", "PUT", "PATCH") else None
    r = urllib.request.Request(url, data=data, headers=headers, method=req.method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=SEND_TIMEOUT) as resp:
            raw = resp.read()
            ms = int((time.perf_counter() - t0) * 1000)
            return Response(
                status=resp.status, reason=resp.reason, ms=ms, size=len(raw),
                headers=list(resp.getheaders()),
                body=_pretty(raw, resp.headers.get("Content-Type", "")),
            )
    except urllib.error.HTTPError as e:
        raw = e.read()
        ms = int((time.perf_counter() - t0) * 1000)
        return Response(e.code, e.reason or "", ms, len(raw),
                        list(e.headers.items()) if e.headers else [],
                        _pretty(raw, ""), error="HTTP error")
    except Exception as e:  # noqa: BLE001 — surface everything to the UI
        ms = int((time.perf_counter() - t0) * 1000)
        return Response(0, "-", ms, 0, [], "", error=str(e))


def _pretty(raw: bytes, ctype: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary: {len(raw)} bytes, content-type: {ctype or 'unknown'}>"
    if "json" in ctype or text.lstrip().startswith(("{", "[")):
        try:
            return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return text


# ----------------------------------------------------------- the app -----
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

BG = "#0b1410"
PANEL = "#12211a"
GREEN = "#22c55e"
DIM = "#7fa893"
CORAL = "#ff8b75"


class VortexPost(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("VortexPost — local-first HTTP client")
        self.geometry("1280x800")
        self.configure(fg_color=BG)
        self.requests: list[Request] = []
        self.current: Request | None = None
        self.env: dict[str, str] = {}

        # ---- top bar: the invariant on screen
        top = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=48)
        top.pack(fill="x")
        ctk.CTkLabel(top, text="⚡ VortexPost", font=("Consolas", 17, "bold"),
                     text_color=GREEN).pack(side="left", padx=14, pady=8)
        ctk.CTkLabel(top, text="● offline-first · no account · no telemetry · "
                               "your collections are plain .http files in git",
                     text_color=DIM, font=("Consolas", 11)).pack(side="left")
        self.save_btn = ctk.CTkButton(top, text="Save to .http", width=120,
                                      fg_color="#1f6feb", command=self.save_file)
        self.save_btn.pack(side="right", padx=10)

        # ---- left: collections
        side = ctk.CTkFrame(self, width=270, fg_color=PANEL, corner_radius=0)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        ctk.CTkLabel(side, text="COLLECTIONS", font=("Consolas", 12, "bold"),
                     text_color=DIM).pack(padx=12, pady=(12, 4), anchor="w")
        self.listbox = ctk.CTkTextbox(side, fg_color=BG, text_color=GREEN,
                                      font=("Consolas", 13))
        self.listbox.pack(fill="both", expand=True, padx=10, pady=10)
        self.listbox.bind("<Double-Button-1>", self._on_pick)
        self.listbox.bind("<Button-1>", self._on_click_move)
        ctk.CTkButton(side, text="Reload folder", fg_color="#253528",
                      command=self.load_collections).pack(padx=10, pady=(0, 10), fill="x")

        # ---- center: request + response
        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        row = ctk.CTkFrame(mid, fg_color=PANEL, corner_radius=12)
        row.pack(fill="x")
        self.method = ctk.CTkOptionMenu(row, values=["GET", "POST", "PUT", "PATCH", "DELETE"],
                                        width=100, fg_color=BG)
        self.method.pack(side="left", padx=10, pady=10)
        self.url = ctk.CTkEntry(row, placeholder_text="https://api.example.com/endpoint")
        self.url.pack(side="left", fill="x", expand=True, padx=6, pady=10)
        self.send = ctk.CTkButton(row, text="SEND ▶", width=110, fg_color=GREEN,
                                  text_color="#06120a", font=("Consolas", 14, "bold"),
                                  command=self.on_send)
        self.send.pack(side="left", padx=10)

        ctk.CTkLabel(mid, text="HEADERS / BODY (.http block — editable)",
                     text_color=DIM, font=("Consolas", 11)).pack(anchor="w", pady=(8, 2))
        self.editor = ctk.CTkTextbox(mid, fg_color="#081009", font=("Consolas", 13))
        self.editor.pack(fill="both", expand=True)

        ctk.CTkLabel(mid, text="RESPONSE", text_color=DIM,
                     font=("Consolas", 11)).pack(anchor="w", pady=(8, 2))
        self.meta = ctk.CTkLabel(mid, text="— not sent yet —", text_color=DIM,
                                 font=("Consolas", 12, "bold"), anchor="w")
        self.meta.pack(fill="x")
        self.out = ctk.CTkTextbox(mid, fg_color="#060d09", font=("Consolas", 13))
        self.out.pack(fill="both", expand=True, pady=(4, 0))

        # status footer
        self.status = ctk.CTkLabel(self, text="ready · everything stays on this machine",
                                   text_color=DIM, font=("Consolas", 11))
        self.status.pack(side="bottom", pady=4)

        self.load_collections()

    # ------------------------------------------------ actions
    def load_collections(self) -> None:
        COLLECTIONS.mkdir(exist_ok=True)
        self.requests = []
        for f in sorted(COLLECTIONS.rglob("*.http")):
            self.requests.extend(parse_http(f.read_text(encoding="utf-8"), f))
        self.listbox.delete("1.0", "end")
        for i, r in enumerate(self.requests):
            self.listbox.insert("end", f"{r.method:<7} {r.name}\n")
        self.status.configure(
            text=f"loaded {len(self.requests)} requests from "
                 f"{len(list(COLLECTIONS.rglob('*.http')))} file(s) · local only")

    def _on_click_move(self, _e: object) -> None:
        # move insertion cursor without selecting (ctk textbox quirk)
        pass

    def _on_pick(self, _e: object) -> object:
        idx = int(float(self.listbox.index("insert").split(".")[0])) - 1
        if 0 <= idx < len(self.requests):
            self.select(self.requests[idx])
        return "break"

    def select(self, r: Request) -> None:
        self.current = r
        self.method.set(r.method)
        self.url.delete(0, "end")
        self.url.insert(0, r.url)
        self.editor.delete("1.0", "end")
        head = "".join(f"{k}: {v}\n" for k, v in r.headers)
        self.editor.insert("1.0", head + (("\n" + r.body) if r.body else ""))
        self.out.delete("1.0", "end")
        self.meta.configure(text="— not sent yet —", text_color=DIM)
        self.status.configure(text=f"editing: {r.name} ({r.source_file})")

    def on_send(self) -> None:
        headers: list[tuple[str, str]] = []
        body_parts: list[str] = []
        in_headers = True
        for ln in self.editor.get("1.0", "end").splitlines():
            s = ln.strip()
            if in_headers and s and ":" in s and not s.startswith("{") and not s.startswith("["):
                k, v = s.split(":", 1)
                headers.append((k.strip(), v.strip()))
            elif not s:
                in_headers = False
            else:
                body_parts.append(ln)
        req = Request(name=self.current.name if self.current else "adhoc",
                      method=self.method.get(), url=self.url.get().strip(),
                      headers=headers, body="\n".join(body_parts).strip(),
                      source_file=self.current.source_file if self.current else None)

        self.status.configure(text="sending… (one socket, no queue, no sync)")
        self.update_idletasks()
        resp = send_request(req, self.env)
        self.render(req, resp)

    def render(self, req: Request, resp: Response) -> None:
        if resp.error and resp.status == 0:
            color, label = CORAL, f"FAILED — {resp.error}"
        elif 200 <= resp.status < 300:
            color, label = GREEN, f"{resp.status} {resp.reason}"
        elif resp.status >= 400:
            color, label = CORAL, f"{resp.status} {resp.reason}"
        else:
            color, label = "#f5c542", f"{resp.status} {resp.reason}"
        self.meta.configure(
            text=f"{label}   ·   {resp.ms} ms   ·   {resp.size} bytes   ·   {req.method} {req.url}",
            text_color=color)
        hdr = "\n".join(f"{k}: {v}" for k, v in resp.headers[:14])
        self.out.delete("1.0", "end")
        self.out.insert("end", (hdr + "\n\n" if hdr else "") + resp.body)
        self.out.see("1.0")
        self.status.configure(text=f"done · {resp.size} bytes in {resp.ms} ms · "
                                   f"nothing was uploaded anywhere")

    def save_file(self) -> None:
        """Write the edited block back → git diff shows exactly this change."""
        if not self.current or not self.current.source_file:
            self.status.configure(text="no file backing this request (adhoc)")
            return
        f: Path = self.current.source_file
        reqs = parse_http(f.read_text(encoding="utf-8"), f)
        r = self.current
        r.method = self.method.get()
        r.url = self.url.get().strip()
        r.headers, body_parts, in_h = [], [], True
        for ln in self.editor.get("1.0", "end").splitlines():
            s = ln.strip()
            if in_h and s and ":" in s and not s.startswith("{") and not s.startswith("["):
                k, v = s.split(":", 1)
                r.headers.append((k.strip(), v.strip()))
            elif not s:
                in_h = False
            else:
                body_parts.append(ln)
        r.body = "\n".join(body_parts).strip()
        reqs[r.index] = r
        f.write_text(serialize_http(reqs), encoding="utf-8")
        self.status.configure(text=f"saved → {f.name} (commit it: git diff shows "
                                   f"exactly your change)")


def main() -> None:
    app = VortexPost()
    app.mainloop()


if __name__ == "__main__":
    main()
