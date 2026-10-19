"""Bind rebuilt repair sources to publication receipts before release assembly."""
import hashlib
import json
from pathlib import Path
import re


def checked_repair_images(task, config, publication, images):
    path = Path(task) / 'environment/IMAGE_BUILD_CONTRACT.json'
    if not path.exists():
        return None
    contract = json.loads(path.read_text())
    if contract.get('schema') != 'cowork-repair-image-contract-v1':
        raise ValueError('unsupported repair image contract')
    version = config['metadata']['task_version']
    if contract['task_version'] != version or publication['task_version'] != version:
        raise ValueError('publication does not describe the current repair version')
    if contract['baseline'] != config['metadata']['workplace_baseline_head']:
        raise ValueError('repair image baseline mismatch')
    for role, build in contract['roles'].items():
        context = (Path(task) / build['context']).resolve()
        if not context.is_relative_to(Path(task).resolve()):
            raise ValueError('image context escapes task')
        inventory = set()
        for relative in build['copy_roots']:
            source = context / relative
            paths = source.rglob('*') if source.is_dir() else [source]
            inventory.update(str(p.relative_to(context)) for p in paths if p.is_file())
        if inventory != set(build['inputs']):
            raise ValueError('repair image COPY input inventory changed')
        for relative, expected in build['inputs'].items():
            source = (context / relative).resolve()
            if not source.is_relative_to(context) or not source.is_file():
                raise ValueError('missing repair image source: ' + relative)
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise ValueError('repair image source changed: ' + relative)
        reference = images.get(role, '')
        if not re.fullmatch(r'harbor\.tokenwave\.us/release/[^@]+@sha256:[0-9a-f]{64}', reference):
            raise ValueError('repair publication requires a digest-pinned Harbor image')
        if reference == build['parent_image']:
            raise ValueError('retired parent image cannot run the repaired task')
        if publication.get(role, {}).get('build_image_id') != build['validated_image_id']:
            raise ValueError('publication does not attest the validated build image: ' + role)
    return contract


def bind_repair_compose(compose, images):
    """Keep current source service configuration when pinning published images."""
    for service_name, role in [('main', 'environment'), ('workplace', 'workplace')]:
        if service_name in compose['services']:
            service = compose['services'][service_name]
            service.pop('build', None)
            service['image'] = images[role]
            service['init'] = True
    return compose
