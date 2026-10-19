"""Verify versioned, public task overlays shared by solving and clean replay."""
import hashlib
import json
from pathlib import Path, PurePosixPath


def checked_task_files(task):
    return _checked_files(task, 'public-runtime.json', 'public-task-runtime-v1', '/task', {'.md', '.py', '.cjs'})


def checked_workplace_files(task):
    return _checked_files(task, 'workplace-runtime.json', 'workplace-task-runtime-v1', '/usr/local/bin', {'.py'})


def _checked_files(task, filename, schema, parent, suffixes):
    task = Path(task)
    manifest = task / filename
    if not manifest.exists():
        return []
    if manifest.is_symlink():
        raise RuntimeError('Public runtime manifest cannot be a symlink')
    payload = json.loads(manifest.read_text())
    if payload.get('schema') != schema:
        raise RuntimeError('Invalid public task runtime manifest')
    mounts = []
    seen = set()
    for entry in payload['files']:
        relative, target = PurePosixPath(entry['path']), PurePosixPath(entry['target'])
        if (not relative.parts or relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'environment'
                or any(c in str(relative) or c in str(target) for c in [',', '\n', '\r'])
                or target.parent != PurePosixPath(parent) or target.suffix not in suffixes
                or str(target) in seen):
            raise RuntimeError('Invalid public runtime binding')
        source = task
        for part in relative.parts:
            source /= part
            if source.is_symlink():
                raise RuntimeError('Public runtime source cannot be a symlink')
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != entry['sha256']:
            raise RuntimeError('Public runtime source hash mismatch: ' + str(relative))
        seen.add(str(target))
        mounts.append((source, str(target), True))
    return mounts


def bind_compose_files(compose, task):
    """Use the same checked public files when regenerating standalone compose."""
    task = Path(task)
    def destination(value):
        if isinstance(value, dict):
            return value.get('target')
        parts = str(value).split(':')
        return parts[1] if len(parts) > 1 else parts[0]
    for service, overlays in [('main', checked_task_files(task)), ('workplace', checked_workplace_files(task))]:
        if not overlays:
            continue
        targets = {target for _, target, _ in overlays}
        volumes = compose['services'].setdefault(service, {}).setdefault('volumes', [])
        volumes[:] = [value for value in volumes if destination(value) not in targets]
        volumes.extend('./' + str(source.relative_to(task / 'environment'))
                       + ':' + target + ':ro' for source, target, _ in overlays)
