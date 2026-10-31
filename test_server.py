"""Smoke test: start the server on a free port with a temp folder and check the routes and safety.

usage: python test_server.py      (exit code 0 = all checks passed)
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def get(url, rng=None):
    req = urllib.request.Request(url, headers={"Range": rng} if rng else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def main():
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "Trip 2025", "sub"))
    with open(os.path.join(tmp, "Trip 2025", "film.mp4"), "wb") as f:
        f.write(bytes(range(256)) * 4)
    with open(os.path.join(tmp, "Trip 2025", "notes.txt"), "w") as f:
        f.write("secret")
    with open(os.path.join(os.path.dirname(tmp), "outside.mp4"), "wb") as f:
        f.write(b"x")
    port = 18766
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "media_server.py"), "--port", str(port),
                          "--bind", "127.0.0.1", "--lang", "en", tmp], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(50):
            try:
                get(base + "/")
                break
            except OSError:
                time.sleep(0.1)
        key = os.path.basename(tmp).lower()
        checks = [
            ("index lists the folder", get(base + "/")[1].find(key.encode()) >= 0),
            ("folder page shows subfolder and video", all(s in get(base + "/%s/Trip%%202025" % key)[1]
                                                         for s in (b"sub", b"film.mp4"))),
            ("folder page hides non-video", b"notes.txt" not in get(base + "/%s/Trip%%202025" % key)[1]),
            ("player page", get(base + "/%s/play/Trip%%202025/film.mp4" % key)[0] == 200),
            ("range request", get(base + "/%s/file/Trip%%202025/film.mp4" % key, "bytes=10-19") == (206, bytes(range(10, 20)))),
            ("non-video file refused", get(base + "/%s/file/Trip%%202025/notes.txt" % key)[0] == 404),
            ("dot-dot refused", get(base + "/%s/file/..%%2Foutside.mp4" % key)[0] == 404),
            ("encoded traversal refused", get(base + "/%s/file/Trip%%202025%%2F..%%2F..%%2Foutside.mp4" % key)[0] == 404),
            ("unknown root refused", get(base + "/nope/")[0] == 404),
        ]
    finally:
        p.terminate()
    for name, ok in checks:
        print("[%s] %s" % ("ok" if ok else "FAIL", name))
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
