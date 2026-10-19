"""Audited Docker default masks absent from the original host profiles.

Only the host's exact CPU thermal directories and latency_stats are eligible.
The image, original profile and task gate are unchanged. Runtime receipts bind
the selected defaults, the actual mount graph and kernel object identities.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

REVISION = 'docker-default-mask-compatibility-v1'
CPU = re.compile(r'/sys/devices/system/cpu/cpu[0-9]+/thermal_throttle')
LATENCY = '/proc/latency_stats'
KERNEL_OPTIONAL_MASKS = ('/proc/acpi', '/proc/scsi', '/sys/devices/virtual/powercap')
FIXED_MASKS = (
    '/proc/acpi', '/proc/kcore', '/proc/keys', '/proc/latency_stats',
    '/proc/timer_list', '/proc/interrupts', '/sys/firmware',
    '/sys/devices/virtual/powercap', '/proc/scsi',
)


def require(condition, message):
    if not condition:
        raise RuntimeError('Docker platform mask: ' + message)


def host_evidence():
    base = Path('/sys/devices/system/cpu')
    require(base.resolve(strict=True) == base, 'noncanonical host CPU directory')
    paths = []
    for path in sorted(base.glob('cpu[0-9]*/thermal_throttle')):
        info = path.lstat()
        require(CPU.fullmatch(str(path)) and path.resolve(strict=True) == path
                and stat.S_ISDIR(info.st_mode) and (info.st_uid, info.st_gid) == (0, 0),
                'invalid host thermal directory')
        paths.append(str(path))
    require(len(paths) <= 4096, 'host CPU count exceeds bound')
    latency = Path(LATENCY)
    present = latency.exists()
    if present:
        info = latency.lstat()
        require(latency.resolve(strict=True) == latency and stat.S_ISREG(info.st_mode)
                and (info.st_uid, info.st_gid) == (0, 0), 'invalid host latency_stats file')
    return paths, present


def approved_from_evidence(cpus, latency_present, defaults):
    require(isinstance(cpus, list) and isinstance(defaults, list)
            and all(isinstance(p, str) for p in cpus + defaults), 'invalid path evidence')
    require(len(set(cpus)) == len(cpus) and len(set(defaults)) == len(defaults),
            'duplicate path evidence')
    require(len(cpus) <= 4096 and all(CPU.fullmatch(p) for p in cpus),
            'unapproved host CPU path')
    selected = {p for p in defaults if CPU.fullmatch(p)}
    require(selected == set(cpus), 'Docker masks differ from the exact host CPU set')
    require(type(latency_present) is bool, 'invalid latency_stats evidence')
    if latency_present:
        require(LATENCY in defaults, 'host latency_stats is not masked by Docker')
        selected.add(LATENCY)
    rows = []
    for target in sorted(selected):
        rows.append(dict(target=target, root='/null' if target == LATENCY else '/',
                         options='rw,nosuid' if target == LATENCY else 'ro,relatime',
                         filesystem='tmpfs', source='tmpfs'))
    return rows


def approved_mounts(info):
    cpus, latency = host_evidence()
    return approved_from_evidence(cpus, latency, info['HostConfig'].get('MaskedPaths'))


def configured_masked_paths():
    """Use the clean approval's masks and the current host's exact CPU set."""
    cpus, _ = host_evidence()
    return list(FIXED_MASKS) + cpus


def absent_kernel_masks(info):
    """Only omit optional kernel directories that do not exist on this host."""
    require(info['HostConfig'].get('MaskedPaths') == configured_masked_paths(),
            'Docker mask configuration differs from the approved profile')
    rows = parse_mountinfo(Path('/proc/self/mountinfo').read_bytes())
    absent = []
    for target in KERNEL_OPTIONAL_MASKS:
        path = Path(target)
        if os.path.lexists(path):
            continue
        parent = path.parent
        metadata = parent.stat()
        require(parent.resolve(strict=True) == parent
                and stat.S_ISDIR(metadata.st_mode)
                and (metadata.st_uid, metadata.st_gid) == (0, 0),
                'optional kernel mask has an untrusted parent: ' + target)
        ancestors = [row for row in rows
                     if path == Path(row['target']) or path.is_relative_to(row['target'])]
        mount = max(ancestors, key=lambda row: len(row['target']))
        expected_fs = 'proc' if target.startswith('/proc/') else 'sysfs'
        require(mount['filesystem'] == expected_fs,
                'optional kernel mask is outside its kernel filesystem: ' + target)
        absent.append(target)
    return absent


def omit_absent_kernel_masks(mounts, info):
    absent = absent_kernel_masks(info)
    for target in absent:
        expected = dict(target=target, root='/', options='ro,relatime',
                        filesystem='tmpfs', source='tmpfs')
        require([row for row in mounts if row['target'] == target] == [expected],
                'optional kernel mask differs from the signed approval: ' + target)
    return [row for row in mounts if row['target'] not in absent]


def parse_mountinfo(raw):
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)

    rows = []
    for line in raw.decode().splitlines():
        before, separator, after = line.partition(' - ')
        left, right = before.split(), after.split()
        require(separator and len(left) >= 6 and len(right) >= 3, 'malformed mount table')
        rows.append(dict(target=unescape(left[4]), root=unescape(left[3]),
                         options=left[5], optional=left[6:],
                         filesystem=right[0], source=unescape(right[1])))
    require(rows, 'empty mount table')
    return rows


def validate_metadata(target, info, *, children=(), null_info=None):
    require((info.st_uid, info.st_gid) == (0, 0), 'mask object is not root-owned: ' + target)
    if target == LATENCY:
        require(stat.S_ISCHR(info.st_mode) and os.major(info.st_rdev) == 1
                and os.minor(info.st_rdev) == 3, 'latency mask is not /dev/null')
        require(null_info is not None and stat.S_ISCHR(null_info.st_mode)
                and (info.st_dev, info.st_ino, info.st_rdev) ==
                (null_info.st_dev, null_info.st_ino, null_info.st_rdev),
                'latency mask does not bind the container /dev/null inode')
    else:
        require(CPU.fullmatch(target) and stat.S_ISDIR(info.st_mode),
                'thermal mask is not an exact directory: ' + target)
        require(not children, 'thermal mask is nonempty: ' + target)


def verify(info, raw, expected=None):
    current = approved_mounts(info)
    if expected is not None:
        require(current == expected, 'approved platform defaults changed after creation')
    pid = info['State'].get('Pid', 0)
    require(info['State'].get('Running') and type(pid) is int and pid > 0,
            'container has no running admission process')
    root = Path('/proc') / str(pid) / 'root'
    rows = parse_mountinfo(raw)
    absent = absent_kernel_masks(info)
    for target in absent:
        require(not os.path.lexists(root / target.lstrip('/')),
                'kernel mask absent on host exists inside the container: ' + target)
        require(not any(row['target'] == target or row['target'].startswith(target + '/')
                        for row in rows),
                'unexpected mount below absent kernel mask: ' + target)
    by_target = {}
    for row in rows:
        by_target.setdefault(row['target'], []).append(row)
    objects = []
    for wanted in current:
        target = wanted['target']
        observed = by_target.get(target, [])
        require(observed == [dict(wanted, optional=[])],
                'mask mount is missing, stacked, propagated or altered: ' + target)
        path = root / target.lstrip('/')
        metadata = path.lstat()
        # lstat deliberately rejects final-component symlinks. Kernel mount
        # table matching above rules out ancestor mounts or bind substitutions.
        children = list(path.iterdir()) if stat.S_ISDIR(metadata.st_mode) else []
        null_info = (root / 'dev/null').lstat() if target == LATENCY else None
        validate_metadata(target, metadata, children=children, null_info=null_info)
        objects.append(dict(target=target, uid=metadata.st_uid, gid=metadata.st_gid,
                            mode=metadata.st_mode, device=metadata.st_dev,
                            inode=metadata.st_ino, rdev=metadata.st_rdev))
    return dict(schema=REVISION, container=info['Id'], pid=pid,
                adapter_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                docker_masked_paths=info['HostConfig']['MaskedPaths'],
                absent_kernel_masks=absent,
                additional_mounts=current, objects=objects,
                mountinfo_sha256=hashlib.sha256(raw).hexdigest())
