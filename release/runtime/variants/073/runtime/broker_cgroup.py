"""Broker-owned cgroup v2 slices that outlive Docker's transient child scope."""

import os
from pathlib import Path
import re
import subprocess
import time


CGROUP_ROOT = Path('/sys/fs/cgroup')


def _run(*arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f'{arguments[0]} failed: {(result.stderr or result.stdout)[-1000:]}')
    return result


def _stop_unit(unit):
    state = _run('systemctl', 'show', unit, '--property=ActiveState', '--value').stdout.strip()
    if state in ('active', 'activating', 'deactivating'):
        _run('systemctl', 'stop', unit)


class BrokerSlice:
    def __init__(self, run_id, role):
        if os.geteuid() != 0 or not re.fullmatch(r'cw-\d{3}-(cowork|oracle)-[0-9a-f]{12}', run_id):
            raise RuntimeError('Broker cgroup requires a root-owned solver run')
        if role not in ('solver', 'verifier'):
            raise RuntimeError('Unknown broker cgroup role')
        self.name = 'cw' + run_id[3:].replace('-', '') + role + '.slice'
        self.hold = self.name.removesuffix('.slice') + '-hold.service'
        self.path = CGROUP_ROOT / self.name
        if not any(line.split(' - ')[-1].startswith('cgroup2 ') and line.split()[4] == str(CGROUP_ROOT)
                   for line in Path('/proc/self/mountinfo').read_text().splitlines()):
            raise RuntimeError('Cgroup v2 is not mounted at the broker root')
        _run('systemd-run', '--unit=' + self.hold.removesuffix('.service'),
             '--slice=' + self.name, '--property=Delegate=no', '/usr/bin/sleep', '36000')
        self._check_owner()

    @classmethod
    def attach(cls, run_id, role):
        """Reopen a previously created broker slice during finish or stop."""
        if os.geteuid() != 0 or not re.fullmatch(r'cw-\d{3}-(cowork|oracle)-[0-9a-f]{12}', run_id):
            raise RuntimeError('Broker cgroup requires a root-owned solver run')
        if role not in ('solver', 'verifier'):
            raise RuntimeError('Unknown broker cgroup role')
        self = cls.__new__(cls)
        self.name = 'cw' + run_id[3:].replace('-', '') + role + '.slice'
        self.hold = self.name.removesuffix('.slice') + '-hold.service'
        self.path = CGROUP_ROOT / self.name
        self._check_owner()
        return self

    def _check_owner(self):
        metadata = self.path.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise RuntimeError('Solver cgroup is delegated or writable outside the broker')

    def release_hold(self):
        _stop_unit(self.hold)
        self._check_owner()

    def events(self):
        self._check_owner()
        return dict(line.split() for line in (self.path / 'cgroup.events').read_text().splitlines())

    def wait_empty(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.events()
            if events.get('populated') == '0':
                return events
            time.sleep(0.1)
        raise RuntimeError('Solver cgroup remains populated after container stop')

    def close(self):
        _stop_unit(self.hold)
        if self.path.exists():
            self.wait_empty()
            _stop_unit(self.name)
