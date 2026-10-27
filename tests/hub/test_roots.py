"""The hub's copy of the node-root rule.

Pure path arithmetic: the hub cannot see the node's filesystem, so it cannot
resolve symlinks and refuses anything it would have to guess about. The agent
re-checks all of this against the real directory.
"""

from __future__ import annotations

import pytest

from minemanager_hub import roots


class TestNormalise:
    @pytest.mark.parametrize("raw, want", [
        ("/srv/minecraft", "/srv/minecraft"),
        ("/srv/minecraft/", "/srv/minecraft"),
        ("/srv//minecraft", "/srv/minecraft"),
        ("/srv/./minecraft", "/srv/minecraft"),
        ("  /srv/minecraft  ", "/srv/minecraft"),
    ])
    def test_it_cleans_up_a_path(self, raw, want):
        assert roots.normalise(raw) == want

    @pytest.mark.parametrize("raw", ["", "   ", "srv/minecraft", "./x", "/srv/../etc", "/a\0b"])
    def test_it_refuses_what_it_cannot_vouch_for(self, raw):
        with pytest.raises(roots.RootError):
            roots.normalise(raw)


class TestCheckUnder:
    def test_a_child_is_accepted(self):
        assert roots.check_under("/srv/mc", "/srv/mc/survival") == "/srv/mc/survival"

    def test_a_grandchild_is_accepted(self):
        assert roots.check_under("/srv/mc", "/srv/mc/a/b") == "/srv/mc/a/b"

    @pytest.mark.parametrize("path", [
        "/srv/mc",              # the root itself: it would see every sibling
        "/srv",                 # a parent
        "/etc",                 # elsewhere entirely
        "/srv/mcother",         # a prefix match that is not a child
    ])
    def test_anything_else_is_refused(self, path):
        with pytest.raises(roots.RootError):
            roots.check_under("/srv/mc", path)

    def test_no_node_root_enforces_nothing(self):
        assert roots.check_under(None, "/opt/legacy") == "/opt/legacy"


class TestServerDir:
    def test_it_joins_the_name_onto_the_root(self):
        assert roots.server_dir("/srv/mc", "survival") == "/srv/mc/survival"

    def test_it_refuses_a_name_that_climbs_out(self):
        """Belt and braces: ServerCreate already rejects these."""
        with pytest.raises(roots.RootError):
            roots.server_dir("/srv/mc", "../etc")
