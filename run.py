#!/usr/bin/env python3
"""PromptWaver entry point.

Examples
--------
    # preview in the browser, no hardware, local (offline) director (default)
    python run.py

    # drive the Helios, tuned for the CLUB RGB1000 rig with web interface
    python run.py --laser --pps 11000 --max-step 0.03 --invert-x

    # use Claude as the scene director
    ANTHROPIC_API_KEY=sk-... python run.py

    # run headless without web interface
    python run.py --headless

Then open http://localhost:8080 and type a keyword (e.g. "water flowing").
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import platform
import sys
import traceback


# --- crash log ---------------------------------------------------------------
#
# A packaged build that dies on startup closes its console faster than anyone
# can read the traceback, so the traceback has to go somewhere it survives.
# Everything below runs BEFORE the promptwaver imports, because an import
# failure (a missing bundled module, a broken native dependency) is exactly
# one of the failures being diagnosed and would otherwise never reach a
# handler defined later.

def _log_dir() -> str:
    """Where error.txt goes: beside the executable for a packaged build.

    NOT `sys._MEIPASS` — that is a temp directory PyInstaller deletes on exit,
    which is the one place a crash report must not be written. Falls back to
    the home directory if the app lives somewhere unwritable (Program Files, a
    read-only mount, /Applications).
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    if os.access(base, os.W_OK):
        return base
    return os.path.expanduser("~")


ERROR_LOG = os.path.join(_log_dir(), "error.txt")


def _note(line: str) -> None:
    """Append a breadcrumb. Opened and closed per line, and flushed, so the
    file is complete even if the process is killed rather than raising —
    a segfault in a native audio/MIDI library leaves no Python traceback at
    all, and then the LAST breadcrumb is the only evidence of how far it got.
    """
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now():%H:%M:%S}  {line}\n")
            fh.flush()
    except Exception:
        pass            # logging must never be the thing that breaks startup


def _start_log() -> None:
    try:
        with open(ERROR_LOG, "w", encoding="utf-8") as fh:
            fh.write(
                f"PromptWaver startup log — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"python   {sys.version.split()[0]} on {platform.platform()}\n"
                f"frozen   {bool(getattr(sys, 'frozen', False))}\n"
                f"exe      {sys.executable}\n"
                f"bundle   {getattr(sys, '_MEIPASS', '(not packaged)')}\n"
                f"cwd      {os.getcwd()}\n"
                f"argv     {sys.argv}\n\n"
                "If PromptWaver closed unexpectedly, send this whole file.\n\n")
    except Exception:
        pass


def _fatal(exc: BaseException) -> None:
    _note("FATAL — PromptWaver could not continue:")
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
            fh.flush()
    except Exception:
        pass
    print(f"\nPromptWaver failed to start. Details written to:\n  {ERROR_LOG}\n",
          file=sys.stderr)
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    # A packaged build launched from a file manager has no console left to read,
    # so hold the window open. Skipped when nothing is attached to stdin
    # (piped, service, CI) or it would hang forever.
    if getattr(sys, "frozen", False) and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close…")
        except Exception:
            pass


_start_log()
_note("starting imports")

try:
    from promptwaver.engine import Engine
    from promptwaver.web import run as run_web
    from promptwaver.director import local_scene
except BaseException as _e:        # noqa: BLE001 — the whole point is to catch everything
    _fatal(_e)
    raise SystemExit(1)

_note("imports ok")

logging.getLogger("aiohttp").setLevel(logging.ERROR)

HERE = os.path.dirname(os.path.abspath(__file__))


DEFAULT_WEB_PORT = 8080
PORT_SCAN = 12          # how far past the default to look for a free one


def _port_free(host: str, port: int) -> bool:
    """Can we actually bind here? Asked before the engine is built.

    aiohttp only discovers this at `site.start()`, by which point the render
    thread, the synth and the MIDI port are already running — so the failure
    arrived as a traceback after a successful-looking startup.
    """
    import socket
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as sk:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sk.bind((host, port))
            return True
        except OSError:
            return False


def _resolve_port(host: str, requested: int | None) -> int:
    """The port to actually serve on, or exit with something readable.

    The overwhelmingly likely cause of a busy default port is another copy of
    PromptWaver already running — which is exactly what happened to the first
    packaged build anyone ran: the app was up and serving, the second launch
    failed, and the only evidence was `[Errno 98]` at the bottom of a
    fourteen-frame traceback.
    """
    if requested is not None:
        if _port_free(host, requested):
            return requested
        msg = (f"Port {requested} is already in use, so PromptWaver cannot start "
               f"there.\nIf that is another copy of PromptWaver, open "
               f"http://localhost:{requested} to use it, or close it first.\n"
               f"Otherwise pick a different port:  --web-port {requested + 1}")
        _note(f"port {requested} busy and explicitly requested — stopping")
        _note(msg)
        print(f"\n{msg}\n", file=sys.stderr)
        raise SystemExit(2)

    for port in range(DEFAULT_WEB_PORT, DEFAULT_WEB_PORT + PORT_SCAN):
        if _port_free(host, port):
            if port != DEFAULT_WEB_PORT:
                note = (f"port {DEFAULT_WEB_PORT} is in use (probably another "
                        f"copy of PromptWaver) — using {port} instead")
                _note(note)
                print(f"[promptwaver] {note}")
            return port
    msg = (f"Ports {DEFAULT_WEB_PORT}-{DEFAULT_WEB_PORT + PORT_SCAN - 1} are all in "
           f"use. Close whatever is using them, or pass --web-port <n>.")
    _note(msg)
    print(f"\n{msg}\n", file=sys.stderr)
    raise SystemExit(2)


def parse_args():
    ap = argparse.ArgumentParser(description="PromptWaver — immersive laser + synth instrument")
    ap.add_argument("--headless", action="store_true", help="run without the browser control surface")
    # default=None, not 8080, so `main` can tell "the user asked for this port"
    # from "nobody said" — see _resolve_port. Asking for a busy port is an
    # error; not asking and finding the default busy is not.
    ap.add_argument("--web-port", type=int, default=None,
                    help=f"web UI port (default {DEFAULT_WEB_PORT}; if that is "
                         f"taken and you did not ask for it, the next free one is used)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--laser", action="store_true", help="enable Helios DAC output")
    ap.add_argument("--no-audio", action="store_true", help="disable the synth")
    ap.add_argument("--pps", type=int, default=11000, help="points per second to the DAC")
    ap.add_argument("--max-step", type=float, default=0.03, help="max stroke step (normalized)")
    ap.add_argument("--invert-x", action="store_true")
    ap.add_argument("--keystone-h", type=float, default=0.0)
    ap.add_argument("--keystone-v", type=float, default=0.0)
    ap.add_argument("--fps", type=int, default=45)
    ap.add_argument("--diag", action="store_true",
                    help="enable perf/audio diagnostics instrumentation at startup (off by "
                         "default; also toggleable live in Settings)")
    ap.add_argument("--midi", default=None, metavar="HINT",
                    help="MIDI input port to use, matched by substring (e.g. --midi MPK). "
                         "Without this the saved port, else the first non-loopback "
                         "device, is picked automatically; change it live in Settings.")
    ap.add_argument("--list-midi", action="store_true", help="list MIDI input ports and exit")
    ap.add_argument("--model", default=None, help="override director model id")
    ap.add_argument("--scene", default="water flowing", help="initial keyword")
    # Kiosk mode is a runtime toggle (Settings > Kiosk) persisted in
    # settings.json; these only override it at startup, for an installation
    # that should boot straight into it.
    # A packaged build has no terminal, so it gets a window by default; a
    # checkout has one, so it does not. Either can be forced.
    ap.add_argument("--gui", dest="gui", action="store_true", default=None,
                    help="show the desktop window (default in packaged builds)")
    ap.add_argument("--no-gui", dest="gui", action="store_false",
                    help="never show the desktop window; run in the terminal only")
    ap.add_argument("--kiosk", dest="kiosk", action="store_true", default=None,
                    help="arm public kiosk mode at startup (overrides the saved setting)")
    ap.add_argument("--no-kiosk", dest="kiosk", action="store_false",
                    help="start with kiosk mode off (overrides the saved setting)")
    return ap.parse_args()


def main():
    args = parse_args()
    if args.list_midi:
        from promptwaver.midi import MidiInput
        names = MidiInput.list_ports()
        if not names:
            print("no MIDI input ports found"
                  if MidiInput.available()
                  else "mido not installed — pip install mido python-rtmidi")
        for n in names:
            print(n)
        return
    # Before the engine: building it starts the render thread, the synth and
    # the MIDI port, and none of that should happen only to die on a bind.
    web_port = args.web_port
    if not args.headless:
        web_port = _resolve_port(args.host, args.web_port)
        _note(f"port {web_port} is free")

    # Writable data lives beside the executable, never in the bundle — see
    # promptwaver/paths.py. On a packaged build's first run the shipped scene
    # library is copied out so there is something to play.
    from promptwaver.paths import data_dir, seed_scenes
    library_dir = os.path.join(data_dir(), "scenes")
    seeded = seed_scenes(library_dir)
    if seeded:
        print(f"[promptwaver] installed {seeded} scenes into {library_dir}")
    _note(f"building engine (laser={args.laser} audio={not args.no_audio}) "
          f"library={library_dir} seeded={seeded}")
    engine = Engine(
        library_dir=library_dir,
        cache_dir=os.path.join(library_dir, "generated"),
        fps=args.fps, pps=args.pps, max_step=args.max_step,
        invert_x=args.invert_x, keystone_h=args.keystone_h,
        keystone_v=args.keystone_v, enable_laser=args.laser,
        enable_audio=not args.no_audio, model=args.model,
        enable_diagnostics=args.diag, midi_port=args.midi,
    )
    _note("engine built; installing the initial scene")
    engine._install_spec(local_scene(args.scene))
    _note("starting render thread")
    engine.start()
    _note(f"engine running — output={engine.output.name} "
          f"director={'claude' if engine.director.online else 'local'}")
    print(f"[promptwaver] engine running — output={engine.output.name} "
          f"director={'claude' if engine.director.online else 'local'}")

    # Kiosk: the CLI flag wins if given, otherwise whatever was last saved. Done
    # after start() because arming needs the mic stream already open — that's
    # where AudioAnalysis learns its samplerate and so how big a buffer to take.
    from promptwaver import settings as pw_settings
    want_kiosk = args.kiosk if args.kiosk is not None else bool(
        pw_settings.get("kiosk_enabled", False))
    if want_kiosk:
        ok, detail = engine.set_kiosk(True)
        print(f"[promptwaver] kiosk: {detail}")
        if not ok:
            print("[promptwaver] kiosk NOT armed — running as a normal instrument")
    if not args.headless:
        print(f"[promptwaver] open http://localhost:{web_port}")
        if engine.kiosk.enabled:
            print(f"[promptwaver] kiosk screen: http://localhost:{web_port}/kiosk")
        want_gui = args.gui if args.gui is not None else bool(getattr(sys, "frozen", False))
        url = f"http://localhost:{web_port}"

        if want_gui:
            from promptwaver import gui
            if not gui.available():
                _note("tkinter unavailable — falling back to the console")
                print("[promptwaver] no desktop window available (tkinter missing); "
                      "press Ctrl-C to stop")
                want_gui = False

        if want_gui:
            # Server on a worker thread, Tk on the main one — Tk requires the
            # main thread and macOS enforces it. See promptwaver/gui.py.
            from promptwaver.web import serve_in_thread
            _note(f"starting threaded web server on {args.host}:{web_port}")
            handle = serve_in_thread(engine, host=args.host, port=web_port)
            if handle.error is not None:
                raise handle.error
            _note("web server listening; opening the window")
            import promptwaver as _pw
            shown = gui.run(engine, handle, url, _pw.__version__,
                            on_quit=lambda: (_note("quit from the window"),
                                             handle.stop(), engine.stop()))
            if not shown:
                # Tk imported but could not open a display — keep serving
                # rather than exiting, and say how to stop.
                _note("no display; serving headless until Ctrl-C")
                print(f"[promptwaver] no display for the window — serving at {url}; "
                      f"press Ctrl-C to stop")
                try:
                    while True:
                        import time
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
                finally:
                    handle.stop()
                    engine.stop()
            _note("window closed")
        else:
            _note(f"starting web server on {args.host}:{web_port}")
            try:
                run_web(engine, host=args.host, port=web_port)
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                engine.stop()
    else:
        print("[promptwaver] running headless; Ctrl-C to stop")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            engine.stop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise                       # argparse --help/errors are not crashes
    except BaseException as _e:     # noqa: BLE001
        _fatal(_e)
        raise SystemExit(1)
    else:
        _note("exited normally")
