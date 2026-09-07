# Shipping a Python server app as a double-clickable binary

Notes from packaging PromptWaver with PyInstaller, written to be **portable to
the other server-shaped tools** (laser-laser-laser, lightsaber). Nothing here is
specific to lasers — it applies to any Python app whose interface is a local web
page and whose user is not going to open a terminal.

Every number below was measured, not estimated. PromptWaver file references are
given so you can copy the implementation rather than re-derive it.

---

## The shape of the problem

A "run a local server, open a browser" app is lovely from a checkout and
baffling as a downloaded binary:

- Double-clicked from Finder/Explorer/Files it opens **no window at all**. To
  the user, nothing happened.
- There is **no way to quit** short of Task Manager or `pkill`.
- If it crashes on startup, the console (where there is one) **closes before
  the traceback can be read**.
- Run it twice and the second copy dies on a port clash, while the first is
  working fine — and the error blames the port, not the duplicate.

All four have to be solved or the binary is effectively undeliverable. They cost
me four round trips with a user who could see nothing at all.

---

## 1. A window, in tkinter

**Use tkinter.** It is in the standard library on Windows, macOS and Linux,
PyInstaller bundles it with no hook, and it adds nothing to the download.
pywebview, pystray or Qt each add a wheel per platform — for a window with four
labels and two buttons, that is a bad trade.

What the window needs, and nothing more:

- the URL **as a clickable link**, plus an *Open in browser* button
- a live status line proving it is alive (mine shows scene, fps, connected
  browser windows, whether the API is reachable)
- **Quit**, wired to the same path as closing the window

*(PromptWaver: `promptwaver/gui.py`, ~140 lines including the docstring.)*

### The threading constraint is the whole design

**Tk must own the main thread**, and on macOS this is enforced by Cocoa rather
than being a convention. Meanwhile the obvious server entry points —
`aiohttp.web.run_app`, `app.run()`, `uvicorn.run()` — are blocking and install
signal handlers, i.e. they assume they *are* the program.

So the server moves to a worker thread and the window keeps the main one. Never
the reverse: putting Tk on a thread appears to work on Linux and fails on macOS.

For aiohttp that means dropping to the runner API on a private loop:

```python
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
runner = web.AppRunner(make_app(...))
loop.run_until_complete(runner.setup())
loop.run_until_complete(web.TCPSite(runner, host, port).start())
loop.run_forever()                      # stop with loop.call_soon_threadsafe(loop.stop)
```

Two rules that follow:

- **Build the same app object in both paths.** If the windowed and headless
  builds construct their servers differently they will drift, and only one of
  them gets tested. *(PromptWaver: `web.ServerHandle` and `run()` share
  `make_app`.)*
- **Wait for the bind before showing the window.** A window saying "running"
  over a server that failed to start is worse than no window.

### Tk is not thread-safe either

Never touch a widget from the engine/server threads. Refresh the status line by
polling from Tk's own `after()` timer, which is on the main thread by
construction.

Poll **cheap attributes**, not the app's full state payload — PromptWaver's
`engine.state()` assembles the entire websocket broadcast and is already built
20x a second for the browser; the window needs four numbers.

### Degrade, never block startup

- **no tkinter** (a stripped Linux Python) → run in the console, say so
- **tkinter but no display** (SSH, headless box) → keep serving headless

A missing window must never be the reason the tool won't start.

### Platform notes

| | |
|---|---|
| Windows / macOS | Tk ships with Python. Nothing to do. |
| Linux | Tk is a **separate package**. Install `python3-tk` in CI *before* PyInstaller runs, or the Linux binary silently falls back to console-only. |
| macOS | Launched apps can open *behind* the current window — `lift()` then `-topmost` briefly on `darwin`. |
| All | `webbrowser.open(url)` works everywhere. |

Verify it actually bundled — check the build's `Analysis-00.toc` for `_tkinter`
(the C extension, not just the Python package) and check
`warn-<name>.txt` for missing-module warnings.

---

## 2. PyInstaller bundles no data unless you say so

This is the bug that made PromptWaver's binaries **never once work**, across
every release from v0.70.0 onward.

```
pyinstaller --onefile run.py          # ships CODE ONLY
```

No templates, no static files, no docs, no starter data. The failure is not a
friendly "file not found" either — aiohttp's `add_static` raises
`ValueError: '...' does not exist` while the app is still being *constructed*,
so the process dies before printing anything.

```bash
--add-data "pkg/web/static:pkg/web/static"    # Linux/macOS  ':'
--add-data "pkg/web/static;pkg/web/static"    # Windows      ';'
```

**The separator differs by platform**, which is why the workflow needs two build
steps rather than one matrix step. Relative source paths resolve against
`--specpath` (default: the working directory) — fine in CI, but pass absolute
paths if you set `--specpath` explicitly.

Mirror the source tree in the destination so code that finds files relative to
its own package keeps working unchanged.

---

## 3. A frozen app has TWO roots, and mixing them is silent data loss

| | what it is | use for |
|---|---|---|
| `sys._MEIPASS` | PyInstaller's unpack dir | **read-only** assets shipped in the binary |
| `dirname(sys.executable)` | beside the app | **everything written at runtime** |

`_MEIPASS` is **deleted when the process exits.** PromptWaver wrote
`settings.json` and its scene library there, so a packaged build discarded the
API key and every saved scene on close — and because it also crashed on startup
(§2), nobody found out for months.

Both collapse to the repo root from a checkout, which is exactly why this is
invisible in development.

```python
def bundle_dir():                       # read-only, shipped
    return getattr(sys, "_MEIPASS", REPO_ROOT)

def data_dir():                         # writable, persists
    if not getattr(sys, "frozen", False):
        return REPO_ROOT
    beside = os.path.dirname(os.path.abspath(sys.executable))
    if os.access(beside, os.W_OK):
        return beside
    return os.path.join(os.path.expanduser("~"), ".yourapp")   # Program Files, /Applications
```

*(PromptWaver: `promptwaver/paths.py`.)*

**Seed writable data from the bundle on first run** — copy the starter library
out of `bundle_dir()` into `data_dir()`, adding only files that aren't there so
a user's edits and deletions survive upgrades.

---

## 4. `error.txt` — the only diagnostic you get from the field

A packaged app that dies has no console to print to. Write a log **beside the
executable** (not in `_MEIPASS`, which is deleted):

- a **breadcrumb per startup step**, opened/closed and flushed per line
- the **full traceback** on any unhandled exception
- environment header: python version, platform, `frozen`, `_MEIPASS`, cwd, argv
- hold the console open (`input()`) on a frozen build, guarded by
  `sys.stdin.isatty()` so piped/service runs don't hang

Two things that make it actually work:

- **Install it above your own imports.** An import failure — a missing bundled
  module, a broken native dependency — is one of the things you are diagnosing,
  and a handler defined later never runs.
- **Breadcrumbs matter more than the traceback.** A segfault inside a native
  audio/MIDI/USB library leaves *no* Python traceback at all, and then the last
  breadcrumb is the only evidence of how far it got.

This paid for itself immediately: the first log I got back showed the app
reaching "starting web server" and failing on `[Errno 98]` — which told me the
binary was fine and the user had simply launched it twice.

*(PromptWaver: top of `run.py`.)*

---

## 5. Check the port before doing expensive setup

Servers discover a busy port late — aiohttp at `site.start()`, by which point
PromptWaver had already started its render thread, synth and MIDI port. The user
sees a successful-looking startup and then a fourteen-frame traceback ending in
`[Errno 98] address already in use`.

Bind-test the port first, and distinguish two cases:

- **the user named a port** → busy is an error; say so in one sentence and exit
  non-zero, no traceback
- **nobody named one** → busy is not an error; scan on to the next free port and
  announce which you took

Make the default `None` rather than the port number, so you can tell those apart.
And name the likely cause — for a single-instance desktop tool it is almost
always *another copy already running*, so say that and give the URL to reach it.

---

## 6. Checklist before shipping a binary

Build locally first — CI is a slow way to discover any of this.

- [ ] Runs from a **clean directory** (not the repo), double-clicked, not just from a shell
- [ ] Every route/page loads, including static assets
- [ ] Writable data lands **beside the exe** and survives a restart
- [ ] Starter data seeds on first run; a second run doesn't clobber user edits
- [ ] The window opens, the link works, **Quit actually stops the server and the worker threads** (assert the socket refuses afterwards and the threads are dead)
- [ ] A forced failure (occupied port) produces a readable message *and* an `error.txt`
- [ ] `--no-gui` / headless still works for servers and CI
- [ ] Optional heavy dependencies: decide **in** or **out**, and make the in-app message match. PromptWaver leaves `faster-whisper` out (it pulls `av`, `onnxruntime`, `ctranslate2` — a test bundle went past 500MB against a ~160MB release) and tells a packaged user to run from source rather than to `pip install` into a frozen app, which cannot work.
