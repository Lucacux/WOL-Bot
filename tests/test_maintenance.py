import os
import tempfile
import unittest
from unittest.mock import patch

import config
from maintenance import LeaseConflict, acquire_lease, active_lease, release_lease


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "maintenance.json")
        self.path_patch = patch.object(config, "MAINTENANCE_FILE", self.path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_acquire_refresh_release(self):
        lease = acquire_lease("nas", "updates", 300, now=1000)
        self.assertEqual(lease.expires_at, 1300)
        self.assertEqual(active_lease("nas", now=1100).owner, "updates")

        refreshed = acquire_lease("nas", "updates", 600, now=1100)
        self.assertEqual(refreshed.expires_at, 1700)
        self.assertTrue(release_lease("nas", "updates", now=1200))
        self.assertIsNone(active_lease("nas", now=1200))

    def test_other_owner_cannot_steal_or_release(self):
        acquire_lease("media", "updates", 300, now=1000)
        with self.assertRaises(LeaseConflict):
            acquire_lease("media", "backup", 300, now=1001)
        self.assertFalse(release_lease("media", "backup", now=1002))
        self.assertEqual(active_lease("media", now=1002).owner, "updates")

    def test_expired_lease_is_cleaned(self):
        acquire_lease("nas", "updates", 10, now=1000)
        self.assertIsNone(active_lease("nas", now=1011))
