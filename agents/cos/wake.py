#!/usr/bin/python3
"""Wake the CoS sous with the priority nudge routine. A dead socket fails the unit."""
import json, os, socket, time

sock, routine, data = (os.environ[k] for k in ("KITCHEN_SOCK", "PRIORITIES_ROUTINE", "PRIORITIES_FILE"))
text = f"Priority nudge run (scheduled). Follow {routine} against {data}."
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock)
s.sendall(json.dumps({"cook": "cos-priorities", "summary": text,
                      "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}).encode() + b"\n")
s.close()
print(f"cos-priorities: wake sent to {sock}")
