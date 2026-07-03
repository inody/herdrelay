from herdr_discord_bridge.herdr_client import normalize_targets


def test_normalize_targets_from_result_agents():
    targets = normalize_targets(
        {
            "ok": True,
            "result": {
                "agents": [
                    {
                        "pane_id": "1-1",
                        "agent_name": "codex",
                        "agent_status": "blocked",
                        "workspace": {"label": "bridge"},
                        "pane": {"foreground_cwd": "/tmp/bridge"},
                    }
                ]
            },
        },
        preferred_kind="agent",
    )

    assert len(targets) == 1
    assert targets[0].target == "1-1"
    assert targets[0].agent_name == "codex"
    assert targets[0].status == "blocked"
    assert targets[0].workspace_label == "bridge"
    assert targets[0].cwd == "/tmp/bridge"


def test_normalize_targets_from_plain_pane_list():
    targets = normalize_targets(
        [
            {
                "pane_id": "1-2",
                "label": "logs",
                "agent_status": "idle",
            }
        ],
        preferred_kind="pane",
    )

    assert targets[0].target == "1-2"
    assert targets[0].label == "logs"
    assert targets[0].kind == "pane"

