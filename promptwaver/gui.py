"""A small desktop window for the packaged builds.

PromptWaver is a server: the interface is a browser page. Double-clicked from
Finder/Explorer/Files it therefore opens nothing, gives no way to quit short of
Task Manager or `pkill`, and — on a crash — closes its console faster than
anyone can read it. This window is the missing shell: where to click, whether
anything is connected, and a Quit button.

Deliberately **tkinter**: it is in the standard library on Windows, macOS and
Linux, so it adds no dependency to a build that is already 160MB, and
PyInstaller bundles it without a hook. pywebview/pystray/Qt would each be a new
wheel per platform for a window with four labels on it.

Two platform constraints shape the design, and both point the same way:

* **Tk must own the main thread.** On macOS that is not a convention, it is a
  hard requirement — Cocoa rejects UI calls from anywhere else. So the aiohttp
  server runs on a worker thread (`web.ServerHandle`) and Tk keeps the main
  one, never the other way round.
* **Tk is not thread-safe.** Nothing here touches a widget from the engine or
  server threads; the status line is refreshed by polling from Tk's own
  `after()` timer, which runs on the main thread by construction.

If tkinter is missing (a stripped Linux Python without `python3-tk`), `run()`
returns False and the caller falls back to the console. A missing window must
never be the reason the instrument won't start.
"""

from __future__ import annotations

import sys
import threading
import webbrowser

POLL_MS = 1000


def available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def run(engine, handle, url: str, version: str, on_quit=None) -> bool:
    """Show the window and block until it is closed. False if Tk is unusable.

    `handle` is a `web.ServerHandle`; `on_quit` is called once, on the main
    thread, before the window goes away.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return False

    try:
        root = tk.Tk()
    except Exception:
        # A build with tkinter present but no display (headless server, SSH
        # without X, a broken DISPLAY) — not an error, just no window.
        return False

    root.title(f"PromptWaver {version}")
    root.minsize(400, 250)
    try:
        root.configure(bg="#0b0d10")
    except Exception:
        pass

    quitting = threading.Event()

    def do_quit():
        if quitting.is_set():
            return
        quitting.set()
        try:
            if on_quit is not None:
                on_quit()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="PromptWaver is running",
              font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
    ttk.Label(frm, text="The interface is a web page — open it in your browser.",
              wraplength=380, foreground="#666").pack(anchor="w", pady=(2, 10))

    link = ttk.Label(frm, text=url, foreground="#0a58ca", cursor="hand2",
                     font=("TkDefaultFont", 11, "underline"))
    link.pack(anchor="w")
    link.bind("<Button-1>", lambda _e: webbrowser.open(url))

    status = ttk.Label(frm, text="starting…", foreground="#666")
    status.pack(anchor="w", pady=(10, 0))

    row = ttk.Frame(frm)
    row.pack(anchor="w", pady=(16, 0))
    ttk.Button(row, text="Open in browser",
               command=lambda: webbrowser.open(url)).pack(side="left")
    ttk.Button(row, text="Quit", command=do_quit).pack(side="left", padx=(8, 0))

    def refresh():
        if quitting.is_set():
            return
        try:
            # Cheap attributes rather than engine.state(): that assembles the
            # whole broadcast payload and is already built 20x a second for
            # the browser. This only needs four numbers.
            perf = engine.perf.summary()
            scene = "—"
            if getattr(engine.scenes, "current", None) is not None:
                scene = engine.scenes.current.spec.name or "—"
            n = handle.clients() if handle is not None else 0
            status.config(text=(
                f"{scene}   ·   {perf.get('fps', 0):.0f} fps   ·   "
                f"{n} browser window{'' if n == 1 else 's'} connected   ·   "
                f"{'Claude' if engine.director.online else 'offline director'}"))
        except Exception:
            pass        # a status line must never take the app down
        root.after(POLL_MS, refresh)

    root.protocol("WM_DELETE_WINDOW", do_quit)
    root.after(200, refresh)
    # Bring it in front on macOS, where a freshly launched app can open behind
    # whatever the user was already looking at.
    if sys.platform == "darwin":
        try:
            root.lift()
            root.attributes("-topmost", True)
            root.after(600, lambda: root.attributes("-topmost", False))
        except Exception:
            pass
    root.mainloop()
    return True
