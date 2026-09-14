#!/usr/bin/env python3
"""Write `src/plow_agents/message_received.json` from the Plow API's own models.

The stub delivers that file as its one inbound message. Making it here, from
`ChatEvent` and `MessageResource`, rather than typing it out, is what keeps it
the shape a real socket sends. Run it from a Plow checkout's `api/` whenever
those models change, and commit what it writes:

    cd ~/plow/api && PYTHONPATH=. DB_HOST=x DB_PORT=5432 DB_NAME=x DB_USER=x DB_PASSWORD=x \\
        CLOUD_AGENT_IMAGE_REGISTRY=x .venv/bin/python /path/to/plow-agents/tests/regenerate-message-received.py

The DB settings are placeholders: importing the models loads Plow's config,
and nothing here connects to anything.
"""

import json
import os
from datetime import UTC, datetime

from plow.chat.frames import ChatEvent, MessageReceivedBody
from plow.chat.resources import MessageResource, MessageSenderMember

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "plow_agents", "message_received.json")
AT = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

message = MessageResource(
    uid="msg_check",
    chat_uid="cht_check",
    direction="inbound",
    body="plow-agents image check: reply with anything.",
    effect=None,
    status="received",
    sender=MessageSenderMember(uid="cpt_check", display_name="Owner", role="owner", provider_key="+15555550101"),
    attachments=[],
    created_at=AT,
).hiding_text_decorations()
event = ChatEvent(
    event_id="evt_check",
    event_type="message_received",
    created_at=AT,
    chat_id="cht_check",
    data=MessageReceivedBody(message=message),
)
with open(OUT, "w") as handle:
    json.dump(json.loads(event.model_dump_json()), handle, indent=2)
    handle.write("\n")
print(f"wrote {OUT}")
