#!/usr/bin/python3
"""Wake the CoS sous with the priority nudge routine beside this file. A dead socket fails the unit."""
import json, os, pathlib, socket, time

routine = pathlib.Path(__file__).resolve().parent / "priorities/ROUTINE.md"
sock, room = os.environ["KITCHEN_SOCK"], os.environ["PRIORITIES_ROOM"]
text = f"Priority nudge run (scheduled). Follow {routine} against {room}."
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock)
s.sendall(json.dumps({"cook": "cos-priorities", "summary": text,
                      "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}).encode() + b"\n")
s.close()
print(f"cos-priorities: wake sent to {sock}")
