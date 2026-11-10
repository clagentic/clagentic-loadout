"""The consumer map: where it is found, how it is read, the `loadout-interface
consumers` verb, the disposition comparison, the consumers a new spec is filled
with, and the age advisory in doctor. Every map here is synthetic."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from clagentic_loadout.doctor import cli as doctor_cli
from clagentic_loadout.doctor.consumers_check import check_consumers_map
from clagentic_loadout.interface import verb as interface_verb
from clagentic_loadout.interface.consumers import (
    CONFIG_KEY_CONSUMERS_FILE,
    CONSUMERS_FILE_ENV_VAR,
    ConsumersFileError,
    consumers_of,
    load_consumers_file,
    locate_consumers,
    map_age,
    parse_consumers_document,
    resolve_consumers_location,
)
from clagentic_loadout.interface.notice import (
    PLACEHOLDER_DISPOSITION,
    has_disposition,
    mentions_repo,
    undisposed_consumers,
)
from clagentic_loadout.spec import verb as spec_verb
from tests._support.interface_fixtures import (
    BASE_INTERFACE,
    CONSUMERS_TEXT,
    RENAMED_INTERFACE,
    interface_repo,
    write_consumers,
)
from tests._support.roles_groups import profile_repo, write_user_config


@pytest.fixture(autouse=True)
def _no_ambient_map(monkeypatch):
    monkeypatch.delenv(CONSUMERS_FILE_ENV_VAR, raising=False)


def _consumers(capsys, config_root, *argv):
    capsys.readouterr()
    code = interface_verb.main(["consumers", *argv], config_root=config_root)
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out else None), captured.err


class TestLocation:
    def test_the_flag_wins_then_the_environment_then_the_config_setting(self, tmp_path):
        root = write_user_config(tmp_path / "cfg", f"rules:\n  {CONFIG_KEY_CONSUMERS_FILE}: /from/config\n")
        env = {CONSUMERS_FILE_ENV_VAR: "/from/env"}

        assert resolve_consumers_location("/from/flag", env, config_root=root).origin == "flag"
        from_env = resolve_consumers_location(None, env, config_root=root)
        assert (from_env.path, from_env.origin) == ("/from/env", "env")
        from_config = resolve_consumers_location(None, {}, config_root=root)
        assert (from_config.path, from_config.origin) == ("/from/config", "config")

    def test_with_nothing_set_there_is_no_location(self, tmp_path):
        location = resolve_consumers_location(None, {}, config_root=tmp_path / "none")

        assert location.path is None and location.origin == "none"


class TestParsing:
    def test_the_map_reads_producers_elements_and_confirmations(self, tmp_path):
        loaded = load_consumers_file(str(write_consumers(tmp_path)))

        assert loaded is not None and loaded.unconfirmed == 1
        assert sorted(loaded.producers) == ["other-producer", "some-repo"]
        assert loaded.producers["some-repo"][0].consumers[0].confirmed_at == "2026-10-01"

    def test_an_unquoted_date_is_read_as_a_date(self):
        loaded = parse_consumers_document(
            {
                "schema_version": 1,
                "producers": {"p": [{"element": "e", "consumers": [{"repo": "c", "confirmed_at": dt.date(2026, 1, 2)}]}]},
            },
            "m",
        )

        assert loaded.producers["p"][0].consumers[0].confirmed_at == "2026-01-02"

    @pytest.mark.parametrize(
        "document,fragment",
        [
            ([], "top level must be a mapping"),
            ({"schema_version": 3}, "schema_version must be 1"),
            ({"schema_version": 1, "producers": []}, "producers must be a mapping"),
            ({"schema_version": 1, "producers": {"p": "x"}}, "must be a list of elements"),
            (
                {"schema_version": 1, "producers": {"p": [{"element": "e", "consumers": [{"repo": "c", "confirmed_at": "soon"}]}]}},
                "must be an ISO date",
            ),
        ],
    )
    def test_a_map_that_departs_from_the_shape_is_refused_naming_where(self, document, fragment):
        with pytest.raises(ConsumersFileError) as exc:
            parse_consumers_document(document, "some/map.yaml")

        assert fragment in str(exc.value) and "some/map.yaml" in str(exc.value)

    def test_a_missing_file_is_none_not_an_error(self, tmp_path):
        assert load_consumers_file(str(tmp_path / "absent.yaml")) is None


class TestLookup:
    def _map(self, tmp_path):
        return load_consumers_file(str(write_consumers(tmp_path)))

    def test_a_verb_name_finds_the_verb_and_its_flags_for_its_own_producer_only(self, tmp_path):
        hits = consumers_of(self._map(tmp_path), "some-owner/some-repo", names=["tool-one"])

        assert [(h.consumer, h.element) for h in hits] == [
            ("app-one", "tool-one"),
            ("app-two", "tool-one"),
            ("app-one", "tool-one run --fast"),
        ]

    def test_a_path_glob_matches_the_files_the_map_lists(self, tmp_path):
        hits = consumers_of(self._map(tmp_path), "some-repo", paths=["config/*.yaml"])

        assert [(h.consumer, h.kind) for h in hits] == [("app-three", "file")]

    def test_nothing_named_finds_nothing(self, tmp_path):
        assert consumers_of(self._map(tmp_path), "some-repo", names=["tool-two"]) == []


class TestVerb:
    def test_two_consumers_of_an_element_are_printed_with_element_and_confirmed_at(self, tmp_path, capsys):
        write_consumers(tmp_path)

        code, result, _ = _consumers(
            capsys, tmp_path / "cfg", "--repo", "some-repo", "--element", "tool-one",
            "--consumers-file", str(tmp_path / "consumers.yaml"),
        )

        assert code == 0 and result["result"] == "consumers"
        direct = [c for c in result["consumers"] if c["element"] == "tool-one"]
        assert [(c["consumer"], c["confirmed_at"]) for c in direct] == [
            ("app-one", "2026-10-01"),
            ("app-two", "2026-09-01"),
        ]
        assert result["consumers_file_from"] == "flag"

    def test_the_elements_a_diff_changed_name_the_consumers(self, tmp_path, capsys):
        repo, base, head = interface_repo(tmp_path, BASE_INTERFACE, RENAMED_INTERFACE)
        write_consumers(tmp_path)

        code, result, _ = _consumers(
            capsys, tmp_path / "cfg", "--repo", "some-repo", "--repo-path", str(repo),
            "--base", base, "--head", head, "--consumers-file", str(tmp_path / "consumers.yaml"),
        )

        assert code == 0
        assert {c["consumer"] for c in result["consumers"]} == {"app-one", "app-two"}

    def test_no_map_configured_is_reported_and_exits_zero(self, tmp_path, capsys):
        code, result, err = _consumers(
            capsys, tmp_path / "cfg", "--repo", "some-repo", "--element", "tool-one"
        )

        assert code == 0
        assert result["result"] == "unavailable" and result["consumers"] == []
        assert "no consumer map is configured" in result["reason"]
        assert "unavailable" in err

    @pytest.mark.parametrize("content", [None, "schema_version: 7\n"])
    def test_a_missing_or_invalid_map_is_reported_and_exits_zero(self, tmp_path, capsys, content):
        target = tmp_path / "map.yaml"
        if content is not None:
            target.write_text(content, encoding="utf-8")

        code, result, _ = _consumers(
            capsys, tmp_path / "cfg", "--repo", "some-repo", "--element", "tool-one",
            "--consumers-file", str(target),
        )

        assert code == 0 and result["result"] == "unavailable"
        assert str(target) in result["reason"]

    def test_the_map_is_found_through_the_config_setting(self, tmp_path, capsys):
        path = write_consumers(tmp_path)
        root = write_user_config(tmp_path / "cfg", f"rules:\n  {CONFIG_KEY_CONSUMERS_FILE}: {path}\n")

        code, result, _ = _consumers(capsys, root, "--repo", "some-repo", "--element", "tool-one")

        assert code == 0 and result["consumers_file_from"] == "config" and result["consumers"]

    @pytest.mark.parametrize(
        "argv",
        [[], ["--base", "main"], ["--head", "main", "--element", "x"]],
        ids=["nothing named", "base alone", "head alone"],
    )
    def test_a_lookup_that_names_nothing_is_a_usage_error(self, tmp_path, capsys, argv):
        code, _result, err = _consumers(capsys, tmp_path / "cfg", "--repo", "some-repo", *argv)

        assert code == interface_verb.EXIT_USAGE and "loadout-interface" in err


class TestDisposition:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("task-123", True),
            ("tracked in some-ticket", True),
            ("none: the consumer is being retired", True),
            ("None - superseded by the new verb", True),
            ("none", False),
            ("none:", False),
            ("  ", False),
            ("", False),
            (PLACEHOLDER_DISPOSITION, False),
            ("nonexistent-ticket", True),
        ],
    )
    def test_a_disposition_is_a_work_item_or_none_with_a_reason(self, text, expected):
        assert has_disposition(text) is expected

    def test_a_repository_is_named_as_a_whole_word_before_any_element(self):
        assert mentions_repo("app-one", "app-one")
        assert mentions_repo("App-One: tool-one", "some-owner/app-one")
        assert not mentions_repo("app-one-extra", "app-one")
        assert not mentions_repo("app-two: app-one is also affected", "app-one")

    def test_one_consumer_with_a_disposition_and_one_without(self, tmp_path):
        loaded = load_consumers_file(str(write_consumers(tmp_path)))
        hits = consumers_of(loaded, "some-repo", names=["tool-one"])

        both = undisposed_consumers(
            hits, [("app-one: tool-one", "task-1"), ("app-two", "none: retiring")]
        )
        one_missing = undisposed_consumers(hits, [("app-one: tool-one", "task-1")])
        placeholder = undisposed_consumers(
            hits, [("app-one", "task-1"), ("app-two", PLACEHOLDER_DISPOSITION)]
        )

        assert both == []
        assert [(u.consumer, u.element) for u in one_missing] == [("app-two", "tool-one")]
        assert [u.consumer for u in placeholder] == ["app-two"]


class TestSpecNew:
    def _new(self, capsys, tmp_path, *argv):
        capsys.readouterr()
        code = spec_verb.main(["new", "--role", "any-role", *argv], config_root=tmp_path / "cfg")
        captured = capsys.readouterr()
        return code, (json.loads(captured.out) if code == 0 else None), captured.err

    def test_named_elements_fill_the_consumers_with_a_source_and_placeholder_dispositions(
        self, tmp_path, capsys
    ):
        repo = profile_repo(tmp_path, name="some-repo")
        path = write_consumers(tmp_path)

        code, draft, _ = self._new(
            capsys, tmp_path, "--repo-path", str(repo), "--element", "tool-one",
            "--consumers-file", str(path),
        )

        assert code == 0
        section = draft["consumers"]
        assert section["source"] == f"{path} (flag)" and section["note"] is None
        assert [i["consumer"] for i in section["items"]] == [
            "app-one: tool-one",
            "app-two: tool-one",
            "app-one: tool-one run --fast",
        ]
        assert {i["disposition"] for i in section["items"]} == {PLACEHOLDER_DISPOSITION}

    def test_a_path_names_the_consumers_of_the_files_it_matches(self, tmp_path, capsys):
        repo = profile_repo(tmp_path, name="some-repo")
        path = write_consumers(tmp_path)

        _code, draft, _ = self._new(
            capsys, tmp_path, "--repo-path", str(repo), "--path", "config/*",
            "--consumers-file", str(path),
        )

        assert [i["consumer"] for i in draft["consumers"]["items"]] == [
            "app-three: config/settings.yaml (keys)"
        ]

    def test_an_unavailable_map_leaves_the_section_empty_with_the_reason(self, tmp_path, capsys):
        repo = profile_repo(tmp_path, name="some-repo")

        code, draft, _ = self._new(
            capsys, tmp_path, "--repo-path", str(repo), "--element", "tool-one"
        )

        assert code == 0
        assert draft["consumers"]["items"] == [] and draft["consumers"]["source"] is None
        assert "consumers not filled: no consumer map is configured" in draft["consumers"]["note"]

    def test_naming_nothing_leaves_the_section_exactly_as_before(self, tmp_path, capsys):
        repo = profile_repo(tmp_path, name="some-repo")

        _code, draft, _ = self._new(
            capsys, tmp_path, "--repo-path", str(repo), "--consumers-file", str(write_consumers(tmp_path))
        )

        assert draft["consumers"] == {"source": None, "note": "no source yet", "items": []}

    def test_a_map_that_lists_none_says_so_in_the_note(self, tmp_path, capsys):
        repo = profile_repo(tmp_path, name="some-repo")

        _code, draft, _ = self._new(
            capsys, tmp_path, "--repo-path", str(repo), "--element", "tool-two",
            "--consumers-file", str(write_consumers(tmp_path)),
        )

        assert draft["consumers"]["items"] == []
        assert "lists none of the named elements" in draft["consumers"]["note"]


class TestDoctor:
    TODAY = dt.date(2026, 11, 10)

    def test_an_old_map_reports_its_age_as_an_advisory_and_stays_ok(self, tmp_path):
        old = CONSUMERS_TEXT.replace('"2026-10-01"', '"2025-01-01"').replace(
            '"2026-09-01"', '"2025-01-02"'
        ).replace('"2026-10-02"', '"2025-01-03"').replace('"2026-08-01"', '"2025-01-04"')
        path = write_consumers(tmp_path, old)
        root = write_user_config(tmp_path / "cfg", f"rules:\n  {CONFIG_KEY_CONSUMERS_FILE}: {path}\n")

        result = check_consumers_map(config_root=root, today=self.TODAY)

        assert result.ok
        assert "advisory" in result.summary and "older than the 90-day window" in result.summary
        assert "2025-01-01 to 2025-01-04" in result.summary

    def test_a_recent_map_reports_its_dates_with_no_advisory(self, tmp_path):
        path = write_consumers(tmp_path)
        root = write_user_config(tmp_path / "cfg", f"rules:\n  {CONFIG_KEY_CONSUMERS_FILE}: {path}\n")

        result = check_consumers_map(config_root=root, today=dt.date(2026, 10, 10))

        assert result.ok and "advisory" not in result.summary
        assert "2026-08-01 to 2026-10-02" in result.summary

    def test_a_map_with_some_old_confirmations_counts_them(self, tmp_path):
        path = write_consumers(tmp_path)
        root = write_user_config(tmp_path / "cfg", f"rules:\n  {CONFIG_KEY_CONSUMERS_FILE}: {path}\n")

        result = check_consumers_map(config_root=root, today=self.TODAY)

        assert result.ok and result.resolved["stale_entries"] == 1
        assert "1 of 5 confirmation(s)" in result.summary

    @pytest.mark.parametrize("content", [None, "schema_version: 7\n"])
    def test_an_unavailable_map_is_an_advisory_not_a_failure(self, tmp_path, content):
        target = tmp_path / "map.yaml"
        if content is not None:
            target.write_text(content, encoding="utf-8")
        root = write_user_config(tmp_path / "cfg", f"rules:\n  {CONFIG_KEY_CONSUMERS_FILE}: {target}\n")

        result = check_consumers_map(config_root=root, today=self.TODAY)

        assert result.ok and result.summary.startswith("advisory:")

    def test_nothing_configured_is_ok_with_an_advisory(self, tmp_path):
        result = check_consumers_map(config_root=tmp_path / "cfg", today=self.TODAY)

        assert result.ok and "no consumer map is configured" in result.summary

    def test_the_doctor_verb_reports_the_check_and_does_not_fail_on_it(self, tmp_path, capsys):
        rc = doctor_cli.main(["--config-root", str(tmp_path / "cfg")])

        out = capsys.readouterr().out
        assert "[OK  ] consumers_map" in out
        assert rc == doctor_cli.EXIT_OK


class TestMapAge:
    def test_age_counts_each_confirmation(self, tmp_path):
        loaded = load_consumers_file(str(write_consumers(tmp_path)))

        age = map_age(loaded, dt.date(2026, 11, 10), window_days=60)

        assert (age.entries, age.oldest, age.newest) == (5, "2026-08-01", "2026-10-02")
        assert age.stale_entries == 2 and not age.stale

    def test_nothing_configured_is_not_an_available_map(self, tmp_path):
        assert not locate_consumers(config_root=tmp_path / "none").available
