#!/usr/bin/env python3
"""Write the stub's two fixtures from the Plow API's own models.

`message_received.json` is the one inbound message the stub delivers, made
from `ChatEvent` and `MessageResource`; `identity.json` is its answer to
`GET /v1/agents/cloud/me`, made from `AgentIdentity`, `LineResource` and
`ChatResource`. Making them here rather than typing them out is what keeps them
the shapes Plow sends. Run it from a Plow checkout's `api/` whenever those
models change, and commit what it writes:

    cd ~/plow/api && env PYTHONPATH=. DB_HOST=x DB_PORT=5432 DB_NAME=x DB_USER=x DB_PASSWORD=x \\
        CLOUD_AGENT_IMAGE_REGISTRY=x ANTHROPIC_API_KEY=x OAUTH_STATE_SECRET=x AGENT_INDEX_SERVICE_TOKEN=x \\
        CORS_ORIGINS='["http://x"]' WEB_BASE_URL=http://x PLOW100_BASE_URL=http://x STRIPE_SECRET_KEY=x \\
        STRIPE_WEBHOOK_SECRET=x PLOW100_STRIPE_SECRET_KEY=x PLOW100_STRIPE_WEBHOOK_SECRET=x \\
        .venv/bin/python /path/to/plow-agents/tests/regenerate-fixtures.py

The settings are placeholders: importing the models loads Plow's config, and
nothing here connects to anything.
"""

import json
import os
from datetime import UTC, datetime

from plow.assistant.router import AgentIdentity, AgentSummary
from plow.assistant.signup import Signup
from plow.chat.frames import ChatEvent, MessageReceivedBody
from plow.chat.resources import LineResource, MessageResource, MessageSenderMember
from plow.chat.routers.chats import ChatParticipantAgent, ChatParticipantMember, ChatResource

PACKAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "plow_agents")
AT = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def write(name: str, model) -> None:
    path = os.path.join(PACKAGE, name)
    with open(path, "w") as handle:
        json.dump(json.loads(model.model_dump_json()), handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")



message = MessageResource(
    uid="msg_check",
    chat_uid="cht_check",
    direction="inbound",
    body="plow-agents image check: reply with anything.",
    effect=None,
    status="received",
    sender=MessageSenderMember(uid="cpt_check", display_name="Owner", role="owner", provider_key="+15555550101"),
    attachments=[],
    mentions=None,
    created_at=AT,
).hiding_text_decorations()
event = ChatEvent(
    event_id="evt_check",
    event_type="message_received",
    created_at=AT,
    chat_id="cht_check",
    data=MessageReceivedBody(message=message),
)
write("message_received.json", event)

line = LineResource(uid="ln_check", provider_type="imessage", provider_key="+15555550100", display_name="plow-agents")
identity = AgentIdentity(
    line=line,
    chats=[ChatResource(
        uid="cht_check",
        status="active",
        trusted=True,
        provider_key="+15555550100",
        participants=[
            ChatParticipantMember(uid="cpt_check", status="active", display_name="Owner", role="owner",
                                  provider_type="imessage", provider_key="+15555550101", joined_at=AT),
            ChatParticipantAgent(line=line, relationship="self"),
        ],
        created_at=AT,
    )],
    mcp_url=None,
    signup=Signup(name="plow-agents", phrase="text this to start"),
    agent=AgentSummary(uid="agt_check", name="plow-agents", provider="exe:ghcr.io/you/plow-agents@sha256:" + "0" * 64, settings={}),
)
write("identity.json", identity)
