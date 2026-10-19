"""Verified task-local overlays for immutable release images.

Only declared files are projected; private workplace data never enters main.
The release manifest binds the plan and every input by content hash.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bindings(task, entry, role, *, oracle=False):
    task = Path(task)
    plan_name = 'environment/runtime-files.json'
    plan = task / plan_name
    if not plan.exists():
        return [], []
    records = entry.get('files', {})

    def checked(relative):
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or not relative.startswith('environment/'):
            raise RuntimeError('Invalid task runtime source')
        source = task
        for part in path.parts:
            source = source / part
            if source.is_symlink():
                raise RuntimeError('Task runtime source contains a symlink')
        if not source.is_file() or records.get(relative) != sha(source):
            raise RuntimeError('Task runtime file differs from release manifest: ' + relative)
        return source

    data = json.loads(checked(plan_name).read_text())
    if data.get('schema') != 'cowork-task-runtime-files-v1':
        raise RuntimeError('Invalid task runtime file schema')
    mounts, commands, destinations = [], [], set()
    for record in data['files']:
        source = checked(record['source'])
        target = record['target']
        dest = PurePosixPath(target)
        kind = record['role']
        if (kind not in ('environment', 'workplace') or not dest.is_absolute()
                or str(dest) != target or '..' in dest.parts or ',' in target or '\n' in target
                or not target.startswith(('/task/', '/opt/task-runtime/', '/opt/tokenwave-original/authoring/', '/run/cowork-runtime/'))
                or (kind == 'environment' and target.startswith('/opt/tokenwave-original/'))
                or (kind, target) in destinations):
            raise RuntimeError('Invalid task runtime destination')
        destinations.add((kind, target))
        if kind == role and not (oracle and target == '/task/README.md'):
            mounts.append((source, target, True))
    for command in data.get('prepare', []):
        if (command['role'] != 'environment' or command['argv'][:1] != ['python3']
                or len(command['argv']) != 2 or not command['argv'][1].startswith('/run/cowork-runtime/')
                or ('environment', command['argv'][1]) not in destinations):
            raise RuntimeError('Invalid task runtime preparation command')
        if role == command['role']:
            commands.append(command['argv'])
    return mounts, commands


def project_compose(task, compose):
    """Preserve verified projections when regenerating publication compose files."""
    plan = Path(task) / 'environment/runtime-files.json'
    if not plan.exists():
        return compose
    data = json.loads(plan.read_text())
    for record in data['files']:
        service = 'main' if record['role'] == 'environment' else 'workplace'
        source = './' + str(PurePosixPath(record['source']).relative_to('environment'))
        mount = source + ':' + record['target'] + ':ro'
        volumes = compose['services'][service].setdefault('volumes', [])
        if mount not in volumes:
            volumes.append(mount)
    for command in data.get('prepare', []):
        # argv has already been constrained to python3 + one fixed mounted path.
        prepare = {'command': command['argv'], 'user': 'root'}
        hooks = compose['services']['main'].setdefault('post_start', [])
        if prepare not in hooks:
            hooks.append(prepare)
    return compose
