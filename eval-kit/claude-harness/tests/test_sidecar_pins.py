"""Sidecar source expectations come from the evaluated release, never stale pins."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
COMMON = next((p for p in (PACKAGE / 'common', PACKAGE.parent / 'common') if p.is_dir()), PACKAGE / 'common')
sys.path.insert(0, str(PACKAGE / 'runner'))
sys.path.insert(1, str(COMMON))
import sidecar_budget  # noqa: E402
from sidecar_manifest import ADAPTER, NATIVE_HELPER, NOTIFICATION, sha256_file  # noqa: E402

WS = 'environment/workplace-service/'


class SidecarPins(unittest.TestCase):
    def setUp(self):
        temporary = PACKAGE / 'tests' / '.tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.release = Path(self.temp.name) / 'release'
        self.release.mkdir()
        self.entry = {'row': 7, 'task_version': 'fixture-v1', 'path': 'tasks/007-fixture',
                      'images': {'workplace': 'registry.invalid/w@sha256:' + 'd' * 64},
                      'files': {WS + 'harbor_dynamic_colleague_adapter.py': 'a' * 64,
                                WS + 'runtime_daemon.py': 'b' * 64}}
        (self.release / 'MANIFEST.json').write_text(json.dumps({'release': 'fixture', 'tasks': [self.entry]}))
        self.manifest_sha = sha256_file(self.release / 'MANIFEST.json')
        environment = patch.dict(os.environ, {'COWORK_RELEASE_ROOT': str(self.release)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_stale_pins_fall_back_to_manifest_hashes(self):
        with patch.object(sidecar_budget.pins, 'PINNED_MANIFEST_SHA256', 'f' * 64), \
             patch.object(sidecar_budget.pins, 'EXPECTED_ADAPTER_SHA256', {7: 'c' * 64}), \
             patch.object(sidecar_budget.pins, 'EXPECTED_NOTIFICATION_SHA256', {7: 'c' * 64}):
            self.assertEqual(sidecar_budget.expected_for(self.entry), ('a' * 64, 'b' * 64))

    def test_current_pins_win_for_their_own_manifest(self):
        with patch.object(sidecar_budget.pins, 'PINNED_MANIFEST_SHA256', self.manifest_sha), \
             patch.object(sidecar_budget.pins, 'EXPECTED_ADAPTER_SHA256', {7: 'c' * 64}), \
             patch.object(sidecar_budget.pins, 'EXPECTED_NOTIFICATION_SHA256', {7: 'e' * 64}):
            self.assertEqual(sidecar_budget.expected_for(self.entry), ('c' * 64, 'e' * 64))

    def test_native_budget_rows_are_derived_from_shipped_helper(self):
        self.assertIsNone(sidecar_budget.native_for(self.entry))
        entry = dict(self.entry, files=dict(self.entry['files'], **{WS + 'colleague_output_budget.py': '9' * 64}))
        native = sidecar_budget.native_for(entry)
        self.assertEqual(native['files'], {ADAPTER: 'a' * 64, NOTIFICATION: 'b' * 64, NATIVE_HELPER: '9' * 64})

    def test_regen_script_writes_release_bound_pins_and_check_detects_staleness(self):
        output = Path(self.temp.name) / 'pins.py'
        script = PACKAGE / 'tools' / 'regen_sidecar_pins.py'
        run = lambda *extra: subprocess.run([sys.executable, '-B', str(script), str(self.release),
                                             '--output', str(output), *extra],
                                            capture_output=True, text=True, timeout=60)
        result = run()
        self.assertEqual(result.returncode, 0, result.stderr)
        namespace = {}
        exec(compile(output.read_text(), str(output), 'exec'), namespace)
        self.assertEqual(namespace['PINNED_MANIFEST_SHA256'], self.manifest_sha)
        self.assertEqual(namespace['EXPECTED_ADAPTER_SHA256'], {7: 'a' * 64})
        self.assertEqual(run('--check').returncode, 0)
        (self.release / 'MANIFEST.json').write_text(json.dumps({'release': 'rebuilt', 'tasks': [self.entry]}))
        self.assertEqual(run('--check').returncode, 1)


if __name__ == '__main__':
    unittest.main()
