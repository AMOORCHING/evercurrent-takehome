import datetime as dt
import json
import re
from pathlib import Path

import pytest

DATA = Path(__file__).parent.parent / "data"

TASK_FIELDS = {"status_change": "status", "deadline_change": "deadline", "owner_change": "owner_id"}
STATUSES = {"open", "in_progress", "blocked", "done"}
CASE_COUNTS = {"X": 8, "R": 3, "I": 5, "K": 4, "A": 4, "N": 3, "L": 6}


@pytest.fixture(scope="module")
def seed():
    return json.loads((DATA / "graph_seed.json").read_text())


@pytest.fixture(scope="module")
def gold():
    return json.loads((DATA / "gold.json").read_text())


@pytest.fixture(scope="module")
def threads():
    """Thread ID to the graph user IDs who posted or reacted in it."""
    slack = json.loads((DATA / "slack.json").read_text())
    names = {u["id"]: u["name"] for u in slack["users"]}
    people: dict[str, set[str]] = {}
    for channel, messages in slack["messages"].items():
        for m in messages:
            thread = f"{channel}:{m.get('thread_ts', m['ts'])}"
            people.setdefault(thread, set()).add(names[m["user"]])
            for reaction in m.get("reactions", []):
                people[thread] |= {names[u] for u in reaction["users"]}
    return people


def _deltas(gold):
    return [(t, d) for t in gold["threads"] for d in t["deltas"]]


def test_every_gold_delta_references_a_real_thread_and_target(gold, threads, seed):
    tasks = {t["id"] for t in seed["tasks"]}
    requirements = {r["id"] for r in seed["requirements"]}
    users = {u["id"] for u in seed["users"]}

    assert {t["thread"] for t in gold["threads"]} == set(threads)
    for thread, d in _deltas(gold):
        assert thread["thread"] in threads, d["id"]
        assert set(d["affected"]) <= users, d["id"]
        if d["target_kind"] == "requirement":
            assert d["target_id"] in requirements, d["id"]
            assert d["type"] == "value_change", d["id"]
            continue
        assert d["target_id"] in tasks, d["id"]
        assert d["type"] in TASK_FIELDS, d["id"]
        for value in (d["old_value"], d["new_value"]):
            if d["type"] == "status_change":
                assert value in STATUSES, d["id"]
            elif d["type"] == "owner_change":
                assert value in users, d["id"]
            else:
                dt.date.fromisoformat(value)


def test_old_values_match_recorded_state(gold, seed):
    rows = {t["id"]: dict(t) for t in seed["tasks"]} | {r["id"]: dict(r) for r in seed["requirements"]}
    for _, d in sorted(_deltas(gold), key=lambda pair: pair[1]["ts"]):
        column = TASK_FIELDS.get(d["type"], "value")
        assert rows[d["target_id"]][column] == d["old_value"], d["id"]
        rows[d["target_id"]][column] = d["new_value"]


def test_affected_users_are_not_in_the_thread(gold, threads):
    for thread, d in _deltas(gold):
        assert not set(d["affected"]) & threads[thread["thread"]], d["id"]


def test_every_planted_case_appears_once(gold):
    scenario = (DATA / "SCENARIO.md").read_text()
    planted = re.findall(r"^- \*\*([XRIKANL]\d)\*\*", scenario, flags=re.MULTILINE)
    cases = [t["case"] for t in gold["threads"] if t["case"] and t["case"][0] != "B"]
    assert sorted(cases) == sorted(planted)
    for prefix, count in CASE_COUNTS.items():
        assert sum(c.startswith(prefix) for c in cases) == count, prefix

    for t in gold["threads"]:
        if t["case"] and t["case"].startswith("L"):
            assert t["deltas"] == [], t["case"]
        elif t["case"]:
            assert len(t["deltas"]) == 1, t["case"]


def test_noise_ratio(gold, capsys):
    total = len(gold["threads"])
    noise = sum(1 for t in gold["threads"] if not t["deltas"])
    with capsys.disabled():
        print(f"\nrealized noise ratio: {noise}/{total} = {noise / total:.1%}")
    assert 0.6 <= noise / total <= 0.8
