"""The settings YAML emitter: what it writes reads back identically here and
in a standard YAML parser."""
import math

import pytest
import yaml

from tess_core.config import YamlSubsetError, dump_yaml_subset, parse_yaml_subset

SETTINGS = {
    "schema_version": 1,
    "roles": {"conversation": {"profile": "codex-sol"}, "compaction": "codex-sol"},
    "gateway": {"add_dirs": ["~"], "personal_research_times": ["00:00", "06:00", "12:00"], "timezone": "America/Los_Angeles",
                "empty_list": [], "empty_map": {}, "port": 8420, "ratio": 0.25, "enabled": False, "nothing": None},
    "memory": {"data_stores": [{"name": "tess-agent", "type": "tess-core", "path": "/runtime/sessions",
                                "skip_copies_of": ["/a", "/b"]}, {"name": "second"}],
               "nested": [[1, 2], ["a"]], "personal": {"context_subjects": ["owner"], "ai_name": ""}},
    "awkward strings": ["- dash", "a: b", "x #y", "true", "yes", "Off", "0755", "12", "1.5e3", "", " pad", "line\nbreak",
                        'quote"s', "tab\there", "ünïcode", "#hash", "~", "null", "{}", "[]", "*star", "&amp", "!tag", "back\\slash"],
}


def test_settings_round_trip_through_this_parser_and_a_standard_one():
    text = dump_yaml_subset(SETTINGS)
    assert parse_yaml_subset(text) == SETTINGS
    assert yaml.safe_load(text) == SETTINGS
    assert "\t" not in text and all((len(line) - len(line.lstrip(" "))) % 2 == 0 for line in text.splitlines())


def test_lists_are_indented_under_their_key():
    assert dump_yaml_subset({"add_dirs": ["~", "/x"]}) == 'add_dirs:\n  - "~"\n  - /x\n'


@pytest.mark.parametrize("value", [{"x": math.nan}, {"x": math.inf}, {1: "non-string key"}, {"x": object()}, {"x": b"bytes"}])
def test_values_that_cannot_round_trip_are_refused(value):
    with pytest.raises(YamlSubsetError):
        dump_yaml_subset(value)
