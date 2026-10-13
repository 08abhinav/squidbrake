"""Usage stats: always on, said once in a terminal, never from the hooks or scripts, never more than the command name."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pilot  # noqa: E402
import telemetry  # noqa: E402


@pytest.fixture()
def sent(tmp_path, monkeypatch):
    monkeypatch.setenv("SQUIDBRAKE_HOME", str(tmp_path))
    monkeypatch.delenv("KEYS_PATH", raising=False)
    monkeypatch.delenv("SQUIDBRAKE_REF", raising=False)
    for var in ("SQUIDBRAKE_TELEMETRY", "DO_NOT_TRACK", "CI"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(telemetry, "POSTHOG_KEY", "phc_test")
    bodies = []
    monkeypatch.setattr(telemetry, "_post", bodies.append)
    monkeypatch.setattr(telemetry, "_interactive", lambda: True)
    monkeypatch.setattr(telemetry.threading, "Thread", RunNow)
    monkeypatch.setattr(pilot, "_post", lambda server, path, body: joins.append((path, body["code"])) or {})
    joins.clear()
    return bodies


joins = []   # what the community pilot was told: ("/v1/pilot/join" or "/v1/pilot/leave", code)


class RunNow:   # sends straight away, so a test can look at what went out
    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)

    def join(self, _timeout=None):
        pass


def answers(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda _q: next(it))


def test_first_run_says_what_is_sent_once_and_only_the_command_name_goes(sent, monkeypatch, capsys):
    answers(monkeypatch, "")   # the one optional question (where from): skipped
    telemetry.maybe(["undo", "SECRET-FILE-ID", "--path", "/home/me/secret.txt"], "9.9.9")
    out = capsys.readouterr().out
    assert "Never your commands, files, prompts, rules or keys" in out and "?" not in out.split("where did you hear")[0]
    assert len(sent) == 1
    body = json.dumps(sent[0])
    assert sent[0]["event"] == "cli_command" and sent[0]["properties"]["command"] == "undo"
    assert "SECRET" not in body and "secret.txt" not in body
    assert telemetry.load()["enabled"] is True
    assert joins == [("/v1/pilot/join", telemetry.COMMUNITY_CODE)]        # the gateway's counts too

    answers(monkeypatch)                                                  # a second question would raise
    telemetry.maybe(["doctor"], "1")
    assert "Never your commands" not in capsys.readouterr().out            # said once
    assert len(sent) == 2


def test_there_is_no_yes_no_question_and_no_off_switch(sent, monkeypatch, capsys):
    answers(monkeypatch, "n")                                             # "n" to the where-from question: skipped
    telemetry.maybe(["doctor"], "1")
    assert telemetry.load()["enabled"] is True and len(sent) == 1
    assert telemetry.main(["off"], "1") == 2 and telemetry.load()["enabled"] is True
    assert "usage: squidbrake telemetry status" in capsys.readouterr().out
    assert telemetry.main(["status"], "1") == 0
    assert "Anonymous usage stats: on" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["hook"], ["agent-hook", "cursor"], ["shell-guard", "check", "rm -rf /"], ["proxy"]])
def test_hooks_never_say_or_send(sent, monkeypatch, argv):
    answers(monkeypatch)
    telemetry.maybe(argv, "1")
    assert sent == [] and telemetry.load() == {}


def test_scripts_ci_and_do_not_track_never_say_or_send(sent, monkeypatch):
    answers(monkeypatch)
    monkeypatch.setattr(telemetry, "_interactive", lambda: False)
    telemetry.maybe(["doctor"], "1")
    monkeypatch.setattr(telemetry, "_interactive", lambda: True)
    telemetry.maybe(["connect", "all", "--yes"], "1")
    for var, val in (("CI", "true"), ("DO_NOT_TRACK", "1"), ("SQUIDBRAKE_TELEMETRY", "0")):
        monkeypatch.setenv(var, val)
        telemetry.maybe(["doctor"], "1")
        monkeypatch.delenv(var)
    assert sent == [] and telemetry.load() == {}


def test_without_a_posthog_key_only_counts_are_shared(sent, monkeypatch):
    monkeypatch.setattr(telemetry, "POSTHOG_KEY", "")
    answers(monkeypatch, "")
    telemetry.maybe(["doctor"], "1")
    assert sent == []
    assert joins == [("/v1/pilot/join", telemetry.COMMUNITY_CODE)]
    assert (telemetry._pilot_dir() / "community-asked").exists()          # connect all has nothing left to ask


def test_a_real_pilot_is_never_moved_to_the_community_code(sent, monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pilot.json").write_text(json.dumps({"code": "acme-123abc", "server": "https://x",
                                                              "install_id": "a" * 32}), encoding="utf-8")
    answers(monkeypatch, "")
    telemetry.maybe(["doctor"], "1")
    assert joins == [] and pilot.load(tmp_path / "data")["code"] == "acme-123abc"


def test_register_asks_first_and_sends_the_email(sent, monkeypatch):
    answers(monkeypatch, "n")
    telemetry.register(["me@acme.com"], "1")
    assert sent == []
    answers(monkeypatch, "")
    telemetry.register(["me@acme.com", "--company", "Acme"], "1")
    assert sent[0]["event"] == "$identify"
    assert sent[0]["properties"]["$set"]["email"] == "me@acme.com"
    assert sent[0]["properties"]["$set"]["company"] == "Acme"


def test_where_they_heard_about_it_is_one_word_from_a_list(sent, monkeypatch):
    joined = []
    monkeypatch.setattr(pilot, "_post", lambda server, path, body: joined.append(body) or {})
    answers(monkeypatch, "1")                                             # LinkedIn
    telemetry.maybe(["doctor"], "1")
    assert telemetry.load()["ref"] == "linkedin"
    assert sent[-1]["properties"]["ref"] == "linkedin" and sent[-1]["properties"]["$set_once"] == {"ref": "linkedin"}
    assert joined[0]["ref"] == "linkedin"                                # the counts carry it too


def test_a_named_link_answers_it_and_free_text_is_never_kept(sent, monkeypatch):
    monkeypatch.setenv("SQUIDBRAKE_REF", "whatsapp")
    answers(monkeypatch)                                                  # no where-from question at all
    telemetry.maybe(["setup"], "1")
    assert telemetry.load()["ref"] == "whatsapp"
    assert telemetry.ref_from_env() == "whatsapp"
    monkeypatch.setenv("SQUIDBRAKE_REF", "My Name; rm -rf ~/")
    assert telemetry.ref_from_env() is None                             # only a short lowercase word
    answers(monkeypatch, "maybe 3")
    assert telemetry._ask_source() is None                               # not a number from the list: nothing


def test_the_email_is_never_asked_on_the_first_run(sent, monkeypatch):
    answers(monkeypatch, "")                                              # where-from only; a 2nd input would raise
    telemetry.maybe(["doctor"], "1")
    assert "email" not in telemetry.load() and sent[0]["event"] == "cli_command"


def test_a_join_that_failed_is_retried_once_a_day_and_never_after_leaving(sent, monkeypatch, tmp_path):
    calls = []

    def flaky(server, path, body):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("offline")
        return {}
    monkeypatch.setattr(pilot, "_post", flaky)
    answers(monkeypatch, "")                                              # skip where-from
    telemetry.maybe(["doctor"], "1")
    assert telemetry.load()["counts_joined"] is False                    # the join failed (offline)
    telemetry.maybe(["doctor"], "1")                                     # the same day: not tried again
    assert calls == ["/v1/pilot/join"]
    monkeypatch.setattr(telemetry, "_today", lambda: "2099-01-01")
    telemetry.maybe(["doctor"], "1")                                     # the next day: tried again, and it works
    assert telemetry.load()["counts_joined"] is True and pilot.load(tmp_path / "data")
    pilot.leave(tmp_path / "data")                                       # they leave: never joined again
    n = len(calls)
    telemetry.maybe(["doctor"], "1")
    assert pilot.load(tmp_path / "data") is None and "/v1/pilot/join" not in calls[n:]


def test_status_tells_an_earlier_no_apart(sent, monkeypatch, capsys):
    """Someone who said no to an older version keeps sending nothing, and status says so instead of "on"."""
    telemetry._save({"id": "x", "enabled": False, "asked": "2026-10-01"})
    telemetry.maybe(["doctor"], "1")
    assert sent == []
    telemetry.main(["status"], "1")
    assert "off (you said no" in capsys.readouterr().out
