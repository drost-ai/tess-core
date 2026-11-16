from __future__ import annotations

import os
import stat
import types
import unittest
from unittest import mock

from tess_core.daemon import security


def _directory(mode: int, gid: int = 4242) -> os.stat_result:
    return os.stat_result((stat.S_IFDIR | mode, 1, 1, 1, os.getuid(), gid, 0, 0, 0, 0))


def _accounts(*, members: list[str], others_with_primary_group: bool):
    me = types.SimpleNamespace(pw_name="me", pw_uid=os.getuid(), pw_gid=4242)
    other = types.SimpleNamespace(pw_name="other", pw_uid=os.getuid() + 1, pw_gid=4242 if others_with_primary_group else 9999)
    grp = types.SimpleNamespace(getgrgid=lambda gid: types.SimpleNamespace(gr_mem=members))
    pwd = types.SimpleNamespace(getpwuid=lambda uid: me, getpwall=lambda: [me, other])
    return mock.patch.dict("sys.modules", {"grp": grp, "pwd": pwd})


class DirectoryWritableByOthersTests(unittest.TestCase):
    """A directory is unsafe when someone other than its owner can write to
    it. On systems where each user has a group of their own and a umask of
    002, a group-writable directory is not that."""

    def setUp(self) -> None:
        security._PRIVATE_GROUPS.clear()
        self.addCleanup(security._PRIVATE_GROUPS.clear)

    def test_owner_only_and_group_readable_directories_are_safe(self) -> None:
        for mode in (0o700, 0o750, 0o755):
            self.assertFalse(security._writable_by_others(_directory(mode)), oct(mode))

    def test_world_writable_is_never_safe(self) -> None:
        with _accounts(members=[], others_with_primary_group=False):
            for mode in (0o777, 0o757, 0o702):
                self.assertTrue(security._writable_by_others(_directory(mode)), oct(mode))

    def test_group_writable_is_safe_only_when_the_group_is_the_users_alone(self) -> None:
        with _accounts(members=[], others_with_primary_group=False):
            self.assertFalse(security._writable_by_others(_directory(0o775)))
        security._PRIVATE_GROUPS.clear()
        with _accounts(members=["me"], others_with_primary_group=False):
            self.assertFalse(security._writable_by_others(_directory(0o775)))
        security._PRIVATE_GROUPS.clear()
        with _accounts(members=["me", "colleague"], others_with_primary_group=False):
            self.assertTrue(security._writable_by_others(_directory(0o775)))
        security._PRIVATE_GROUPS.clear()
        with _accounts(members=[], others_with_primary_group=True):
            self.assertTrue(security._writable_by_others(_directory(0o775)))

    def test_a_group_that_cannot_be_looked_up_counts_as_shared(self) -> None:
        def missing(gid):
            raise KeyError(gid)

        grp = types.SimpleNamespace(getgrgid=missing)
        pwd = types.SimpleNamespace(getpwuid=lambda uid: types.SimpleNamespace(pw_name="me", pw_uid=uid, pw_gid=1), getpwall=lambda: [])
        with mock.patch.dict("sys.modules", {"grp": grp, "pwd": pwd}):
            self.assertTrue(security._writable_by_others(_directory(0o775)))


if __name__ == "__main__":
    unittest.main()
