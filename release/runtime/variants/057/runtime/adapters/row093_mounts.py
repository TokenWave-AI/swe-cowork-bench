"""Compare the actual row 093 mount graph with a pinned platform profile."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

import engine


PROFILE_SHA256 = '29342613ed09053839548b632665b77791340203ff1cb3d6e1841653d0a18e4d'
OPTIONAL_SYSTEM_TARGETS = frozenset({
    '/proc/acpi', '/proc/scsi', '/sys/firmware', '/sys/devices/virtual/powercap',
})


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


def parse_mountinfo(content):
    rows = []
    for line in content.decode().splitlines():
        before, separator, after = line.partition(' - ')
        fields, detail = before.split(), after.split()
        if not separator or len(fields) < 6 or len(detail) < 3:
            raise RuntimeError('Malformed mountinfo in broker observation')
        rows.append({'root': _unescape(fields[3]), 'target': _unescape(fields[4]),
                     'options': fields[5], 'optional': fields[6:],
                     'filesystem': detail[0], 'source': _unescape(detail[1])})
    if not rows:
        raise RuntimeError('Empty mountinfo in broker observation')
    return rows


def _host_source(path, host_mounts):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.exists():
        raise RuntimeError('Noncanonical or missing host mount source: ' + str(path))
    candidates = [row for row in host_mounts if path.is_relative_to(row['target'])]
    if not candidates:
        raise RuntimeError('Host source is outside known host mounts: ' + str(path))
    parent = max(candidates, key=lambda row: len(row['target']))
    root = str(PurePosixPath(parent['root']) / path.relative_to(parent['target']).as_posix())
    return root, parent['filesystem'], parent['source']


def _profile():
    path = engine.ROOT / 'runtime/approvals/row093-platform.json'
    if path.is_symlink() or engine.digest(path) != PROFILE_SHA256:
        raise RuntimeError('Row 093 platform approval changed')
    profile = json.loads(path.read_text())
    if (profile.get('schema') != 'row093-docker-mount-profile-v1'
            or len(profile.get('system', {})) != 25
            or profile.get('verifier_root_options') != 'rw,relatime'
            or profile.get('verifier_writable_system_targets') !=
            ['/', '/etc/resolv.conf', '/etc/hostname', '/etc/hosts']):
        raise RuntimeError('Invalid row 093 platform approval')
    return profile


def expected_mounts(container_id, cgroup_slice, docker_root, mounts, phase,
                    optional_targets=()):
    profile = _profile()
    if (profile['cgroup_driver'] != 'systemd' or phase not in ('solver', 'verifier')
            or not re.fullmatch(r'[0-9a-f]{64}', container_id)):
        raise RuntimeError('Unsupported Docker cgroup identity')
    optional_targets = set(optional_targets)
    if not optional_targets <= OPTIONAL_SYSTEM_TARGETS:
        raise RuntimeError('Unexpected optional platform mount')
    host_mounts = parse_mountinfo(Path('/proc/self/mountinfo').read_bytes())
    managed = {}
    for name in ('resolv.conf', 'hostname', 'hosts'):
        managed[name] = _host_source(Path(docker_root) / 'containers' / container_id / name, host_mounts)
    expected = {}
    for target, (root, options, filesystem, source) in profile['system'].items():
        if target in OPTIONAL_SYSTEM_TARGETS and target not in optional_targets:
            continue
        if root == '@cgroup':
            root = '/' + cgroup_slice + '/docker-' + container_id + '.scope'
        elif root.startswith('@docker/'):
            name = root.removeprefix('@docker/')
            root, host_fs, host_source = managed[name]
            filesystem = host_fs if filesystem == '@docker-fs' else filesystem
            source = host_source if source == '@docker-device' else source
        expected[target] = {'root': root, 'target': target, 'options': options, 'optional': [],
                            'filesystem': filesystem, 'source': source}
    if phase == 'verifier':
        for target in profile['verifier_writable_system_targets']:
            expected[target]['options'] = profile['verifier_root_options']
    for mount in mounts:
        target = mount.target
        if target in profile['system']:
            raise RuntimeError('Row 093 user mount overlaps the platform profile')
        root, filesystem, source = _host_source(mount.source, host_mounts)
        expected[target] = {'root': root, 'target': target,
                            'options': 'ro,relatime' if mount.read_only else 'rw,relatime',
                            'optional': [], 'filesystem': filesystem, 'source': source}
    return expected


def inspect_mounts(pid, container_id, cgroup_slice, docker_root, mounts, phase,
                   platform_mounts=()):
    if pid <= 0:
        raise RuntimeError('Row 093 container has no admission PID')
    raw = (Path('/proc') / str(pid) / 'mountinfo').read_bytes()
    actual = parse_mountinfo(raw)
    optional_targets = {row['target'] for row in actual} & OPTIONAL_SYSTEM_TARGETS
    expected = expected_mounts(container_id, cgroup_slice, docker_root, mounts, phase,
                               optional_targets)
    for mount in platform_mounts:
        if mount['target'] in expected:
            raise RuntimeError('Platform extension overlaps the original mount profile')
        expected[mount['target']] = dict(mount, optional=[])
    seen = {}
    violations = []
    for row in actual:
        target = row['target']
        if target in seen:
            violations.append('stacked mount: ' + target)
        seen[target] = row
        if expected.get(target) != row:
            violations.append('unapproved final mount: ' + target)
    for target in expected:
        if target not in seen:
            violations.append('missing final mount: ' + target)
    return hashlib.sha256(raw).hexdigest(), tuple(sorted(set(violations))), actual
