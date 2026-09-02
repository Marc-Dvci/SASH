"""Record the walkthrough as an MP4.

Playwright drives the product's own guided walkthrough -- the same code a visitor
runs at ``/?demo=1`` -- against a real SASH server holding the real seeded
database. The beat durations from ``narrate.py`` are injected as
``window.__SASH_TIMING`` before the page scripts run, so the spotlight and the
voice share one clock.

Nothing on screen is added afterwards. Every caption, every cursor move and every
number is rendered by the product, and the buttons the walkthrough presses are
the buttons a person presses.

    python demo_video/narrate.py     # first: voice + timing.json
    python demo_video/record.py      # then: this
    python demo_video/record.py http://127.0.0.1:8000/     # against a server you started
"""

from __future__ import annotations

import json
import pathlib
import shutil
import socket
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for path in (str(ROOT), str(ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

VIEWPORT = (1600, 900)
TAIL_SECONDS = 2.6
OUT = HERE / "sash-demo.mp4"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_server(port: int, db: str) -> subprocess.Popen:
    """The real product, on the real seeded tape."""
    command = [
        sys.executable,
        "-c",
        "import sys; sys.path.insert(0, r'{root}'); sys.path.insert(0, r'{src}');"
        "from sash.cli import main;"
        "main(['--db', r'{db}', 'demo', '--port', '{port}'])".format(
            root=ROOT, src=ROOT / "src", db=db, port=port
        ),
    ]
    # The server's output goes to a file, never to a pipe. Seeding prints a
    # progress bar, a pipe nobody reads fills after a few kilobytes, and the
    # server then blocks forever writing to its own stdout.
    log = HERE / "_server.log"
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, text=True)
    import urllib.request

    deadline = time.time() + 900
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "the server exited:\n" + log.read_text(encoding="utf-8")[-3000:]
            )
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2):
                return process
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    raise RuntimeError("the server did not come up")


def probe_duration(path: pathlib.Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(
            f"ffmpeg failed ({result.returncode}):\n{(result.stderr or '')[-4000:]}"
        )


def record(url: str, timing: dict) -> pathlib.Path:
    from playwright.sync_api import sync_playwright

    raw_dir = HERE / "_raw"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True)

    span = timing["total"] + TAIL_SECONDS
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--force-color-profile=srgb", "--hide-scrollbars"]
        )
        context = browser.new_context(
            viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
            device_scale_factor=1,
            record_video_dir=str(raw_dir),
            record_video_size={"width": VIEWPORT[0], "height": VIEWPORT[1]},
        )
        page = context.new_page()
        # Injected before any page script, so the walkthrough is paced to the
        # voice from its very first beat rather than catching up to it.
        page.add_init_script(
            "window.__SASH_TIMING = "
            + json.dumps(timing["beats"])
            + "; window.__SASH_LEAD = "
            + json.dumps(timing["lead"])
            + ";"
        )
        page.goto(url + "?demo=1", wait_until="networkidle", timeout=120_000)
        started = time.time()
        # Wait for the walkthrough to finish, but never past its own budget.
        while time.time() - started < span:
            if page.evaluate("() => !!window.__SASH_DONE"):
                break
            page.wait_for_timeout(400)
        page.wait_for_timeout(int(TAIL_SECONDS * 1000))
        video = page.video
        context.close()
        browser.close()
        source = pathlib.Path(video.path())
    return source


def mux(video: pathlib.Path, audio: pathlib.Path, out: pathlib.Path) -> None:
    """Pad or trim the audio to the video, fade both ends, encode H.264/AAC."""
    v_len = probe_duration(video)
    run_ffmpeg([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video),
        "-i", str(audio),
        "-filter_complex",
        f"[0:v]fade=t=in:st=0:d=0.5,fade=t=out:st={max(v_len - 0.7, 0):.2f}:d=0.7[v];"
        f"[1:a]apad,atrim=0:{v_len:.2f},afade=t=out:st={max(v_len - 0.7, 0):.2f}:d=0.7[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ])


def main() -> int:
    timing_path = HERE / "timing.json"
    narration = HERE / "narration.wav"
    if not timing_path.exists() or not narration.exists():
        print("run demo_video/narrate.py first", file=sys.stderr)
        return 1
    timing = json.loads(timing_path.read_text(encoding="utf-8"))

    argument = sys.argv[1] if len(sys.argv) > 1 else "local"
    server = None
    if argument == "local":
        port = free_port()
        db = str(ROOT / "demo.sqlite")
        print(f"starting SASH on {port} against {db}")
        server = start_server(port, db)
        url = f"http://127.0.0.1:{port}/"
    else:
        url = argument if argument.endswith("/") else argument + "/"

    try:
        print(f"recording {url} for {timing['total']:.0f}s")
        raw = record(url, timing)
    finally:
        if server:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover
                server.kill()

    mux(raw, narration, OUT)
    length = probe_duration(OUT)
    print(f"wrote {OUT}  ({length:.1f}s)")
    if length > 180:
        print("OVER THREE MINUTES. Trim a beat in web/demo.js.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
