---
name: verify-in-app
description: Launch PromptWaver in a real browser and confirm a change actually renders, with this repo's specific traps — the welcome modal, hidden .val elements, transient auto-hiding UI, buffered websocket state, and crossfade-delayed scene loads. Use whenever a change touches web/static/*.html, the engine's state() payload, or anything the user would see on screen, and whenever asked to screenshot or "check it works".
---

# Verifying a change in the running app

There is no test suite. The only way to know a UI change works is to run the
app and look at it. This is that procedure, plus the traps that have each
cost a wasted round trip at least once.

## 1. Launch

```bash
.venv/bin/python -u run.py --web-port 8098 --no-audio   # drop --no-audio if testing sound
```

Launch it with the **background flag on the Bash tool**, not a shell `&` — a
backgrounded shell job gets reaped when the tool call returns and the server
dies a few seconds later, which looks exactly like a crash.

**Use port 8098, never 8080.** The user usually has their own instance running
on 8080. Scope every cleanup to your own port:

```bash
pkill -f "run.py --web-port 8098"     # never a bare pkill -f run.py
```

Wait for it properly rather than sleeping a fixed amount:

```bash
until curl -s -o /dev/null --max-time 2 http://localhost:8098/; do sleep 2; done
```

`-u` matters: without it stdout is buffered and a traceback never reaches the
log file, so a dead server looks silent rather than broken.

## 2. Drive it

Playwright is installed but **its browsers are not** — use system Chrome:

```python
b = await pw.chromium.launch(channel="chrome")
```

Skeleton that works:

```python
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="chrome")
        pg = await b.new_page(viewport={"width": 1600, "height": 1000})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto("http://localhost:8098/", wait_until="networkidle")
        await pg.wait_for_timeout(1500)
        # The welcome modal covers the whole UI until dismissed.
        try:
            await pg.click("text=Click to begin", timeout=3000)
        except Exception:
            pass
        await pg.wait_for_timeout(600)
        ...
        print("pageerrors:", errs or "none")
        await b.close()

asyncio.run(main())
```

Always register the `pageerror` handler and print it. A JS exception otherwise
fails silently and you debug the wrong thing.

## 3. The traps

Symptom first — that's what you'll actually observe.

| Symptom | Cause | Fix |
|---|---|---|
| Text is set in JS but renders blank | `body.hide-values` collapses `.val` to zero width | Don't use `class="val"` for text meant to be read; style a plain element |
| Element screenshot times out, "element is not visible" | It's inside a collapsed `<details>` accordion, or genuinely hidden | `document.getElementById('acc-x').open = true` first, or check the element is actually in the layout you think |
| An element you targeted is nowhere on screen | Name doesn't imply location — `#apistatus` is **not** in the header | Grep the markup for the id before targeting it; the header's visible text is `#meta` |
| Transient UI captured as blank | It auto-hides on a timer that expired during the wait | Freeze it: `await pg.evaluate("()=>clearTimeout(_costTimer)")` before the shot |
| Canvas screenshot empty or wrong element | The preview canvas is `#view` | — |
| Canvas is black even though a scene is loaded | The engine starts **paused**; `_last_frame` is empty until Start | Click `#master-toggle`, or set `engine.active = True` in-process |
| State read after a `sleep` is stale | The websocket broadcasts ~20Hz, so a 6s sleep leaves ~120 buffered messages and the *first* one you read is 6s old | Drain to the newest before asserting |
| A scene switch appears not to happen | Loads are async and the crossfade keeps `scenes.current` on the **outgoing** scene until it completes | Wait ~5s, or set `crossfade` to 0 first |
| Panel shows the previous scene's data | Same crossfade lag | Same |

Draining the websocket properly:

```python
async def latest(ws, settle=1.5):
    st, end = None, asyncio.get_event_loop().time() + settle
    while asyncio.get_event_loop().time() < end:
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=0.3)
        except asyncio.TimeoutError:
            break
        d = json.loads(m.data)
        s = d.get("state", d)
        if isinstance(s, dict) and "layers" in s:
            st = s
    return st
```

## 4. Verifying without a browser

Often enough. Driving the engine in-process is faster and avoids all of the
above — but `web.run_app` needs the main thread, so you cannot start the
server in a thread. Use the engine directly:

```python
e = Engine(library_dir="scenes", cache_dir="scenes/generated",
           enable_laser=False, enable_audio=False)
e.set_param("crossfade", 0.0)          # no crossfade lag in tests
e._install_spec(spec); e.start(); e.set_active(True)
```

`set_active(True)`, not `e.active = True` — the latter skips the audio unmute,
so the synth renders silence and every sound-derived reading is zero.

## 5. Cleanup — non-negotiable

- **Stop your server**, scoped to your port. Leave the user's 8080 alone.
- **Delete throwaway scenes.** `scenes/` is tracked; a generated scene dirties
  the working tree. Name them `__something` so they're obvious, then remove.
- **Generating costs real money** (~1–4¢ a scene). Don't generate to test
  something a saved scene already demonstrates.
- **Regenerating audio overwrites the target scene's soundscape** — that is a
  destructive write to the user's file. Use a throwaway scene, or copy first.
- Check `git status` before finishing and confirm every remaining change is
  one you meant to make.
