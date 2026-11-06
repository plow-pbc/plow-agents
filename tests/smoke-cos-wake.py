"""The CoS wake delivers one routine line to the kitchen socket, and fails loudly with no kitchen."""
import json, os, pathlib, socket, subprocess, sys, tempfile, threading

WAKE = pathlib.Path(__file__).resolve().parent.parent / "agents/cos/wake.py"

with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "kitchen.sock")
    env = {**os.environ, "KITCHEN_SOCK": path, "PRIORITIES_FILE": "/d/so.md"}

    dead = subprocess.run([sys.executable, "-I", str(WAKE)], env=env, capture_output=True)
    assert dead.returncode != 0, "a wake with no kitchen listening must fail the unit"

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    got = []
    def accept():
        conn, _ = server.accept()
        got.append(conn.makefile().readline())
    t = threading.Thread(target=accept)
    t.start()
    subprocess.run([sys.executable, "-I", str(WAKE)], env=env, check=True, capture_output=True)
    t.join(5)

    msg = json.loads(got[0])
    assert msg["cook"] == "cos-priorities", msg
    assert str(WAKE.parent / "priorities/ROUTINE.md") in msg["summary"] and "/d/so.md" in msg["summary"], msg
print("ok: cos wake")
