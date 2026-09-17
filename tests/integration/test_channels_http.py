"""Reaching the platform from somewhere that is not VS Code.

These go through the real routes, because the interesting failures are at the
edges rather than in the gateway: a provider that posts a form instead of JSON,
a handshake that arrives before any message, a webhook anyone who learns the URL
could call.

That last one is the reason these exist. A webhook endpoint that trusted its own
payload would let whoever found the URL start runs that write files into
somebody's working tree, and the payload is exactly where an attacker has full
control.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def project_id(api_client, repo_copy) -> str:
    return api_client.post(
        "/api/projects",
        json={"name": "channels", "repository_path": str(repo_copy), "base_url": "http://127.0.0.1:59999"},
    ).json()["id"]


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def test_a_webhook_without_a_key_is_refused(api_client) -> None:
    """The payload says who is speaking. It never says what they may do.

    Slack's body names a Slack user; treating that as authorisation would mean
    anyone who learns the URL can start runs. Whoever wires the webhook up
    supplies the key, exactly as the extension does.
    """
    response = api_client.post(
        "/api/channels/slack",
        json={"event": {"text": "automate https://shop.test", "user": "U1", "channel": "C1"}},
        headers={"X-API-Key": ""},
    )
    assert response.status_code in (401, 403)


def test_an_unknown_channel_says_which_ones_exist(api_client) -> None:
    response = api_client.post("/api/channels/carrier-pigeon", json={"text": "hi"})
    assert response.status_code == 404
    assert "slack" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Provider quirks
# --------------------------------------------------------------------------- #
def test_slacks_handshake_is_answered(api_client) -> None:
    """Slack will not enable an integration whose endpoint cannot answer this.

    It arrives before any message ever does, so failing it means the channel
    never works at all.
    """
    response = api_client.post(
        "/api/channels/slack",
        json={"type": "url_verification", "challenge": "abc123xyz"},
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123xyz"}


def test_a_form_encoded_body_is_read(api_client, project_id) -> None:
    """Slack slash commands and Twilio both post forms, not JSON.

    Accepting only JSON would reject half the integrations this is for, with a
    422 that explains nothing.
    """
    response = api_client.post(
        "/api/channels/whatsapp",
        data={"From": "whatsapp:+447700900000", "Body": "hello"},
    )
    assert response.status_code == 200
    assert response.json()["text"]["body"]


def test_a_malformed_payload_is_a_client_error_not_a_crash(api_client) -> None:
    response = api_client.post(
        "/api/channels/slack",
        content="{not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# The same brain, reached from elsewhere
# --------------------------------------------------------------------------- #
def test_a_greeting_over_slack_is_answered_and_starts_nothing(api_client, project_id) -> None:
    response = api_client.post(
        "/api/channels/slack",
        json={"event": {"text": "<@U0BOT> hi", "user": "U1", "channel": "C1"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["blocks"][0]["text"]["text"]
    assert "run_" not in body["text"]


def test_a_url_over_the_generic_channel_starts_an_autopilot_run(api_client, project_id) -> None:
    response = api_client.post(
        "/api/channels",
        json={
            "channel": "api",
            "text": "https://shop.example.com",
            "conversation": "conv-1",
            "project_id": project_id,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "run"
    assert body["autopilot"] is True
    assert body["target_url"] == "https://shop.example.com"
    assert body["run_id"].startswith("run_")


def test_ci_gets_a_machine_shaped_answer(api_client, project_id) -> None:
    response = api_client.post(
        "/api/channels/ci",
        json={
            "instruction": "https://shop.example.com",
            "project_id": project_id,
            "commit": "abc123",
            "branch": "main",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["run_id"].startswith("run_")


def test_a_conversation_remembers_its_project(api_client, project_id) -> None:
    """The second message needs no project, because the first one settled it.

    A binding that evaporates between messages is a binding that gets re-asked
    for every day.
    """
    first = api_client.post(
        "/api/channels",
        json={"channel": "slack", "text": "hello", "conversation": "conv-sticky",
              "project_id": project_id},
    )
    assert first.status_code == 200

    second = api_client.post(
        "/api/channels",
        json={"channel": "slack", "text": "https://shop.example.com", "conversation": "conv-sticky"},
    )
    body = second.json()
    assert body["kind"] == "run", json.dumps(body)
    assert not body["error"]


def test_the_channel_list_is_discoverable(api_client) -> None:
    body = api_client.get("/api/channels").json()

    assert set(body["webhooks"]) >= {"slack", "teams", "whatsapp", "voice", "ci"}
    assert body["webhooks"]["voice"]["voice"] is True
    assert body["webhooks"]["ci"]["conversational"] is False
