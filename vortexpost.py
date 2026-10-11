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

v1.0.0 — full working version:
  file/request CRUD, local environments (env.json), local request history
  (history.jsonl), response copy, variable substitution.

Run: START_VORTEXPOST.bat
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

VERSION = "1.0.0"
ROOT = Path(__file__).parent
COLLECTIONS = ROOT / "collections"
ENV_FILE = ROOT / "env.json"
HISTORY_FILE = ROOT / "history.jsonl"
HISTORY_KEEP = 50
SEND_TIMEOUT = 30.0
USER_AGENT = f"VortexPost/{VERSION} (local-first)"
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

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
    index: int = 0


def parse_http(text: str, source: Path | None = None) -> list[Request]:
    """Parse REST-Client style .http text into requests (pure function)."""
    out: list[Request] = []
    parts = re.split(r"(?m)^###\s*(.*)$", text)
    chunks = parts[1:]
    for i in range(0, len(chunks), 2):
        name = chunks[i].strip() or "unnamed"
        block = chunks[i + 1] if i + 1 < len(chunks) else ""
        out.append(_parse_block(name, block, source, len(out)))
    if not out and text.strip():
        out.append(_parse_block("unnamed", text, source, 0))
    return out


def _parse_block(name: str, block: str, source: Path | None, idx: int) -> Request:
    lines = block.splitlines()
    method, url, start = "", "", 0
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
    in_headers, body = True, []
    for ln in lines[start:]:
        s = ln.strip()
        if in_headers and s and ":" in s and not s.startswith(("{", "[")):
            k, v = s.split(":", 1)
            req.headers.append((k.strip(), v.strip()))
        elif not s:
            in_headers = False
        else:
            body.append(ln)
    req.body = "\n".join(body).strip()
    return req


def serialize_http(reqs: list[Request]) -> str:
    """Requests → .http text (round-trip stable → git diffs stay clean)."""
    out: list[str] = []
    for r in reqs:
        out.append(f"### {r.name}")
        out.append(f"{r.method} {r.url}")
        out.extend(f"{k}: {v}" for k, v in r.headers)
        if r.body:
            out.append("")
            out.append(r.body)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def substitute_variables(text: str, env: dict[str, str]) -> str:
    """Replace {{name}} from the selected local environment."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: env.get(m.group(1), m.group(0)), text)


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
            return Response(resp.status, resp.reason or "",
                            int((time.perf_counter() - t0) * 1000), len(raw),
                            list(resp.getheaders()),
                            _pretty(raw, resp.headers.get("Content-Type", "")))
    except urllib.error.HTTPError as e:
        raw = e.read()
        return Response(e.code, e.reason or "",
                        int((time.perf_counter() - t0) * 1000), len(raw),
                        list(e.headers.items()) if e.headers else [],
                        _pretty(raw, ""), error="HTTP error")
    except Exception as e:  # noqa: BLE001 — surface everything to the UI
        return Response(0, "-", int((time.perf_counter() - t0) * 1000), 0, [],
                        "", error=str(e))


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


# ------------------------------------------------- local-only stores ----
def load_envs() -> dict[str, dict[str, str]]:
    """Git-trackable environments: env.json — values never uploaded."""
    if not ENV_FILE.is_file():
        return {}
    try:
        data = json.loads(ENV_FILE.read_text(encoding="utf-8"))
        return {str(k): {str(a): str(b) for a, b in v.items()}
                for k, v in data.items() if isinstance(v, dict)}
    except (json.JSONDecodeError, OSError):
        return {}


def append_history(req: Request, resp: Response) -> None:
    """Append-only local history, capped at HISTORY_KEEP entries."""
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "method": req.method,
           "url": req.url, "status": resp.status, "ms": resp.ms,
           "size": resp.size, "error": resp.error}
    try:
        if HISTORY_FILE.is_file():
            lines = [l for l in HISTORY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        else:
            lines = []
        lines.append(json.dumps(rec, ensure_ascii=False))
        HISTORY_FILE.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n",
                                encoding="utf-8")
    except OSError:
        pass


def load_history() -> list[dict]:
    if not HISTORY_FILE.is_file():
        return []
    try:
        rows = [json.loads(l) for l in
                HISTORY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        return rows[::-1]
    except (json.JSONDecodeError, OSError):
        return []


# ----------------------------------------------------------- the app -----
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

BG = "#0b1410"
PANEL = "#12211a"
GREEN = "#22c55e"
DIM = "#7fa893"
CORAL = "#ff8b75"
YELLOW = "#f5c542"


class VortexPost(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"VortexPost {VERSION} — local-first HTTP client")
        self.geometry("1320x860")
        self.configure(fg_color=BG)
        self.requests: list[Request] = []
        self.history: list[dict] = load_history()
        self.envs: dict[str, dict[str, str]] = load_envs()
        self.env: dict[str, str] = {}
        self.current: Request | None = None
        self.last_resp: Response | None = None

        self._build_top()
        self._build_side()
        self._build_mid()
        self.reload_all()

    # ---------------------------------------------------------- layout
    def _build_top(self) -> None:
        top = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=48)
        top.pack(fill="x")
        ctk.CTkLabel(top, text="⚡ VortexPost", font=("Consolas", 17, "bold"),
                     text_color=GREEN).pack(side="left", padx=14, pady=8)
        ctk.CTkLabel(top, text=f"v{VERSION} · offline-first · no account · no telemetry · "
                               "collections are plain .http files in git",
                     text_color=DIM, font=("Consolas", 11)).pack(side="left")
        ctk.CTkLabel(top, text="ENV:", text_color=DIM,
                     font=("Consolas", 11)).pack(side="right", padx=(8, 2))
        self.env_pick = ctk.CTkOptionMenu(top, values=["(none)"], width=170,
                                          fg_color=BG, button_color="#253528",
                                          command=self._env_changed)
        self.env_pick.pack(side="right", padx=6, pady=8)
        ctk.CTkButton(top, text="Save to .http", width=120, fg_color="#1f6feb",
                      command=self.save_file).pack(side="right", padx=10)

    def _build_side(self) -> None:
        side = ctk.CTkFrame(self, width=300, fg_color=PANEL, corner_radius=0)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        ctk.CTkLabel(side, text="COLLECTIONS", font=("Consolas", 12, "bold"),
                     text_color=DIM).pack(padx=12, pady=(12, 4), anchor="w")
        self.listbox = ctk.CTkTextbox(side, fg_color=BG, text_color=GREEN,
                                      font=("Consolas", 13), wrap="none")
        self.listbox.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.listbox.bind("<Double-Button-1>", self._on_pick)

        row = ctk.CTkFrame(side, fg_color="transparent")
        row.pack(fill="x", padx=10)
        for label, cmd in (("＋ File", self.new_file), ("＋ Req", self.new_request),
                           ("－ Del", self.delete_request)):
            ctk.CTkButton(row, text=label, width=76, fg_color="#253528",
                          command=cmd).pack(side="left", padx=3, pady=2)

        ctk.CTkLabel(side, text="HISTORY (local)", font=("Consolas", 12, "bold"),
                     text_color=DIM).pack(padx=12, pady=(10, 4), anchor="w")
        self.hist_box = ctk.CTkTextbox(side, fg_color=BG, text_color=YELLOW,
                                       font=("Consolas", 12), wrap="none")
        self.hist_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.hist_box.bind("<Double-Button-1>", self._on_hist_pick)

    def _build_mid(self) -> None:
        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        row = ctk.CTkFrame(mid, fg_color=PANEL, corner_radius=12)
        row.pack(fill="x")
        self.method = ctk.CTkOptionMenu(row, values=METHODS, width=100, fg_color=BG)
        self.method.pack(side="left", padx=10, pady=10)
        self.url = ctk.CTkEntry(row, placeholder_text="https://api.example.com/endpoint")
        self.url.pack(side="left", fill="x", expand=True, padx=6, pady=10)
        ctk.CTkButton(row, text="SEND ▶", width=110, fg_color=GREEN,
                      text_color="#06120a", font=("Consolas", 14, "bold"),
                      command=self.on_send).pack(side="left", padx=10)

        ctk.CTkLabel(mid, text="HEADERS / BODY (.http block — editable)",
                     text_color=DIM, font=("Consolas", 11)).pack(anchor="w", pady=(8, 2))
        self.editor = ctk.CTkTextbox(mid, fg_color="#081009", font=("Consolas", 13))
        self.editor.pack(fill="both", expand=True)

        r2 = ctk.CTkFrame(mid, fg_color="transparent")
        r2.pack(fill="x", pady=(8, 2))
        ctk.CTkLabel(r2, text="RESPONSE", text_color=DIM,
                     font=("Consolas", 11)).pack(side="left")
        ctk.CTkButton(r2, text="Copy body", width=100, fg_color="#253528",
                      command=self.copy_response).pack(side="right")
        self.meta = ctk.CTkLabel(mid, text="— not sent yet —", text_color=DIM,
                                 font=("Consolas", 12, "bold"), anchor="w")
        self.meta.pack(fill="x")
        self.out = ctk.CTkTextbox(mid, fg_color="#060d09", font=("Consolas", 13))
        self.out.pack(fill="both", expand=True, pady=(4, 0))

        self.status = ctk.CTkLabel(self, text="ready · everything stays on this machine",
                                   text_color=DIM, font=("Consolas", 11))
        self.status.pack(side="bottom", pady=4)

    # ------------------------------------------------------ data load
    def reload_all(self) -> None:
        COLLECTIONS.mkdir(exist_ok=True)
        self.requests = []
        for f in sorted(COLLECTIONS.rglob("*.http")):
            self.requests.extend(parse_http(f.read_text(encoding="utf-8"), f))
        self.listbox.delete("1.0", "end")
        for r in self.requests:
            self.listbox.insert("end", f"{r.method:<7} {r.name}\n")

        self.history = load_history()
        self.hist_box.delete("1.0", "end")
        for h in self.history[:HISTORY_KEEP]:
            st = h.get("status") or "ERR"
            self.hist_box.insert(
                "end", f"{str(st):>3}  {h.get('method', '?'):<6} "
                       f"{h.get('ms', 0):>5}ms  {str(h.get('url', ''))[:44]}\n")

        self.envs = load_envs()
        names = ["(none)"] + sorted(self.envs)
        self.env_pick.configure(values=names)
        if self.env_pick.get() not in names:
            self.env_pick.set(names[0])
        self._env_changed(self.env_pick.get())

        nfiles = len(list(COLLECTIONS.rglob("*.http")))
        self.status.configure(text=f"loaded {len(self.requests)} requests · "
                                   f"{nfiles} file(s) · envs: {max(0, len(names) - 1)} · local only")

    def _env_changed(self, name: str) -> None:
        self.env = dict(self.envs.get(name, {})) if name != "(none)" else {}
        self.status.configure(text=f"environment '{name}' · {len(self.env)} variable(s) · "
                                   f"values live in env.json on this machine")

    # ---------------------------------------------------- interactions
    def _selected_index(self) -> int:
        try:
            return int(float(self.listbox.index("insert").split(".")[0])) - 1
        except ValueError:
            return -1

    def _on_pick(self, _e: object) -> str:
        i = self._selected_index()
        if 0 <= i < len(self.requests):
            self.select(self.requests[i])
        return "break"

    def _on_hist_pick(self, _e: object) -> str:
        try:
            i = int(float(self.hist_box.index("insert").split(".")[0])) - 1
        except ValueError:
            return "break"
        if 0 <= i < len(self.history):
            h = self.history[i]
            self.current = None
            self.method.set(h.get("method", "GET"))
            self.url.delete(0, "end")
            self.url.insert(0, h.get("url", ""))
            self.editor.delete("1.0", "end")
            self.meta.configure(text=f"history · {h.get('ts')} · {h.get('status')} · "
                                     f"{h.get('ms')} ms  (press Send to repeat)",
                                text_color=YELLOW)
            self.status.configure(text="history entry loaded · Send to re-run")
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
        self.status.configure(text=f"editing: {r.name}  ({r.source_file})")

    def _editor_parts(self) -> tuple[list[tuple[str, str]], str]:
        headers, body, in_h = [], [], True
        for ln in self.editor.get("1.0", "end").splitlines():
            s = ln.strip()
            if in_h and s and ":" in s and not s.startswith(("{", "[")):
                k, v = s.split(":", 1)
                headers.append((k.strip(), v.strip()))
            elif not s:
                in_h = False
            else:
                body.append(ln)
        return headers, "\n".join(body).strip()

    def on_send(self) -> None:
        headers, body = self._editor_parts()
        req = Request(name=self.current.name if self.current else "adhoc",
                      method=self.method.get(), url=self.url.get().strip(),
                      headers=headers, body=body,
                      source_file=self.current.source_file if self.current else None)
        self.status.configure(text="sending… (one socket, no queue, no sync)")
        self.update_idletasks()
        resp = send_request(req, self.env)
        append_history(req, resp)
        self.last_resp = resp
        self.render(req, resp)

    def render(self, req: Request, resp: Response) -> None:
        if resp.error and resp.status == 0:
            color, label = CORAL, f"FAILED — {resp.error}"
        elif 200 <= resp.status < 300:
            color, label = GREEN, f"{resp.status} {resp.reason}"
        elif resp.status >= 400:
            color, label = CORAL, f"{resp.status} {resp.reason}"
        else:
            color, label = YELLOW, f"{resp.status} {resp.reason}"
        self.meta.configure(
            text=f"{label}   ·   {resp.ms} ms   ·   {resp.size} bytes   ·   "
                 f"{req.method} {req.url}", text_color=color)
        hdr = "\n".join(f"{k}: {v}" for k, v in resp.headers[:14])
        self.out.delete("1.0", "end")
        self.out.insert("end", (hdr + "\n\n" if hdr else "") + resp.body)
        self.out.see("1.0")
        self.reload_all()
        self.status.configure(text=f"done · {resp.size} bytes in {resp.ms} ms · "
                                   f"nothing was uploaded anywhere")

    def copy_response(self) -> None:
        text = self.out.get("1.0", "end").rstrip()
        if not text:
            self.status.configure(text="nothing to copy")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text=f"copied {len(text)} chars to clipboard")

    # -------------------------------------------------------- file CRUD
    def _target_file(self) -> Path | None:
        files = sorted(COLLECTIONS.rglob("*.http"))
        if self.current and self.current.source_file:
            return self.current.source_file
        return files[0] if files else None

    def new_file(self) -> None:
        name = ctk.CTkInputDialog(title="New file",
                                  text="File name inside collections/ (e.g. auth.http):").get()
        if not name:
            return
        name = name.strip().replace("\\", "/").lstrip("/")
        if not name.endswith(".http"):
            name += ".http"
        if "/" in name:
            (COLLECTIONS / name).parent.mkdir(parents=True, exist_ok=True)
        f = COLLECTIONS / name
        if f.exists():
            messagebox.showerror("VortexPost", f"{name} already exists")
            return
        f.write_text("### New request\nGET https://example.com/api\n"
                     "Accept: application/json\n", encoding="utf-8")
        self.reload_all()
        self.status.configure(text=f"created collections/{name} · commit it to share")

    def new_request(self) -> None:
        f = self._target_file()
        if f is None:
            self.new_file()
            return
        name = ctk.CTkInputDialog(title="New request",
                                  text=f"Request name (added to {f.name}):").get()
        if not name:
            return
        name = name.strip()
        reqs = parse_http(f.read_text(encoding="utf-8"), f)
        reqs.append(Request(name=name, method="GET", url="https://example.com/api",
                            headers=[("Accept", "application/json")], source_file=f))
        f.write_text(serialize_http(reqs), encoding="utf-8")
        self.reload_all()
        self.status.configure(text=f"added '{name}' → {f.name} (git diff will show it)")

    def delete_request(self) -> None:
        i = self._selected_index()
        if not (0 <= i < len(self.requests)):
            messagebox.showinfo("VortexPost", "Double-click a request in the list first.")
            return
        r = self.requests[i]
        if not r.source_file:
            return
        if not messagebox.askyesno("Delete", f"Delete '{r.name}' from {r.source_file.name}?"):
            return
        f = r.source_file
        reqs = parse_http(f.read_text(encoding="utf-8"), f)
        kept = [x for x in reqs if not (x.name == r.name and x.index == r.index)]
        f.write_text(serialize_http(kept) if kept else "", encoding="utf-8")
        self.current = None
        self.reload_all()
        self.status.configure(text=f"deleted '{r.name}' from {f.name}")

    def save_file(self) -> None:
        """Write the edited block back → git diff shows exactly this change."""
        if not self.current or not self.current.source_file:
            self.status.configure(text="no file backing this request (history/adhoc) — "
                                       "use ＋ Req to persist it")
            return
        f = self.current.source_file
        reqs = parse_http(f.read_text(encoding="utf-8"), f)
        r = self.current
        r.method = self.method.get()
        r.url = self.url.get().strip()
        r.headers, r.body = self._editor_parts()
        reqs[r.index] = r
        f.write_text(serialize_http(reqs), encoding="utf-8")
        self.reload_all()
        self.status.configure(text=f"saved → {f.name} · git diff shows exactly your change")


def main() -> None:
    VortexPost().mainloop()


if __name__ == "__main__":
    main()
