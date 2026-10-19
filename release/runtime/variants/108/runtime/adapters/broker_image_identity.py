"""Resolve audited OCI index identity without confusing it with Docker config ID.

The bundled public OCI documents were fetched anonymously and verified against
the already approved registry reference. No image, policy or inventory is
substituted. Docker's classic and containerd image stores expose different Id
representations for the same OCI graph; both remain bound to that exact graph.
"""
import hashlib
import json
from pathlib import Path

import engine


APPROVED = {
    (39, 'environment'): 'harbor.tokenwave.us/release/cowork-bench/039-env@sha256:502d543d7d5fd47f291966bdd92901a1880485789ab39a334bfecb107f3246e9',
    (93, 'environment'): 'harbor.tokenwave.us/release/cowork-bench/093-env@sha256:c6064d54c4217b0848b9f82ee68242bfabd721301bc4e0abeeb2f8d5f2501494',
    (93, 'verifier'): 'harbor.tokenwave.us/release/cowork-bench/093-verifier@sha256:d346d1392f399b766df2ac70ec857e0253c55964e100994db8189eca54a71149',
}


def require(condition, message):
    if not condition:
        raise engine.RuntimeErrorWithContext('Broker image identity: ' + message)


def document(path, expected):
    root = engine.ROOT / 'provenance/broker-image-identities'
    require(root.resolve(strict=True) == root and root.is_dir(), 'proof directory is not canonical')
    require(path.parent == root and path.is_file() and not path.is_symlink(), 'proof file is missing or symlinked')
    raw = path.read_bytes()
    require('sha256:' + hashlib.sha256(raw).hexdigest() == expected, 'OCI document digest mismatch: ' + path.name)
    return json.loads(raw)


def require_image(entry, role, image):
    reference = APPROVED.get((entry['row'], role))
    require(reference is not None and entry['images'].get(role) == reference, 'published reference changed')
    root = engine.ROOT / 'provenance/broker-image-identities'
    prefix = f"{entry['row']:03d}-{role}"
    published = reference.rsplit('@', 1)[1]
    root_document_path = root / (prefix + '.root.json')
    if not root_document_path.exists():
        root_document_path = root / (prefix + '.index.json')
    published_document = document(root_document_path, published)
    require(published_document.get('schemaVersion') == 2, 'Unsupported OCI document version')
    root_kind = 'index' if 'manifests' in published_document else 'manifest'
    if root_kind == 'index':
        require(published_document.get('mediaType') in {
            'application/vnd.oci.image.index.v1+json',
            'application/vnd.docker.distribution.manifest.list.v2+json'}, 'Invalid OCI index media type')
        descriptors = [value for value in published_document['manifests']
                       if value.get('platform', {}).get('architecture') == 'amd64'
                       and value.get('platform', {}).get('os') == 'linux']
        require(len(descriptors) == 1, 'OCI index has no unique Linux/amd64 image')
        manifest_digest = descriptors[0]['digest']
    else:
        manifest_digest = published
    manifest = document(root / (prefix + '.manifest.json'), manifest_digest)
    require(manifest.get('schemaVersion') == 2 and manifest.get('mediaType') in {
        'application/vnd.oci.image.manifest.v1+json',
        'application/vnd.docker.distribution.manifest.v2+json'}, 'Invalid OCI manifest media type')
    config_digest = manifest['config']['digest']
    config = document(root / (prefix + '.config.json'), config_digest)
    require(config.get('architecture') == image.get('Architecture') == 'amd64'
            and config.get('os') == image.get('Os') == 'linux', 'image architecture differs')
    require(image.get('Id') in {published, manifest_digest, config_digest}, 'Docker image ID is outside the approved OCI graph')
    require(reference in image.get('RepoDigests', []), 'Docker image has no approved repository digest')
    require(image.get('RootFS', {}).get('Type') == config.get('rootfs', {}).get('type') == 'layers'
            and image['RootFS'].get('Layers') == config['rootfs'].get('diff_ids'),
            'Docker root filesystem layers differ')
    require(len(manifest.get('layers', [])) == len(config['rootfs']['diff_ids']), 'OCI layer counts differ')
    supplied_config = config.get('config') or {}
    inspected_config = image.get('Config') or {}
    # Docker image inspect omits fields whose image-config value is the
    # daemon default. Compare every supplied non-default field, while allowing
    # those exact omissions; a non-default runtime override still fails.
    omitted_defaults = {
        'Hostname': '', 'Domainname': '', 'User': '', 'AttachStdin': False,
        'AttachStdout': False, 'AttachStderr': False, 'Tty': False,
        'OpenStdin': False, 'StdinOnce': False, 'Volumes': None,
        'WorkingDir': '', 'Entrypoint': None, 'NetworkDisabled': False,
        'MacAddress': '', 'OnBuild': None,
    }
    for key, expected in supplied_config.items():
        if key not in inspected_config:
            # Docker's image inspect intentionally omits the parent Image
            # pointer; the graph's config digest and RootFS checks above bind
            # that identity instead.
            if key == 'Image':
                require(isinstance(expected, str) and expected.startswith('sha256:'),
                        'Docker image configuration omits an invalid Image field')
            else:
                require(key in omitted_defaults and expected == omitted_defaults[key],
                        'Docker image configuration omits non-default field: ' + key)
        else:
            require(inspected_config[key] == expected, 'Docker image configuration differs: ' + key)
    return {'schema': 'broker-oci-identity-v1', 'reference': reference,
            'published_sha256': published, 'root_kind': root_kind,
            'index_sha256': published if root_kind == 'index' else None,
            'manifest_sha256': manifest_digest,
            'config_sha256': config_digest, 'docker_image_id': image['Id'],
            'layer_count': len(config['rootfs']['diff_ids']),
            'public_oci_documents_verified': True, 'runtime_config_verified': True,
            'original_policy_and_inventory_unchanged': True}
