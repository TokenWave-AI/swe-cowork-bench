"""Small Docker Engine API bridge using the operator's current Docker context."""

import http.client
import json
import subprocess
import threading
from urllib.parse import quote


class DockerAPIError(RuntimeError):
    pass


class _ResponseBuffer:
    def __init__(self, stream):
        self.stream = stream

    def makefile(self, mode):
        return self.stream


def request(method, path, body=None, timeout=120):
    if not path.startswith('/') or '\r' in path or '\n' in path:
        raise DockerAPIError('Invalid Docker API path')
    payload = b'' if body is None else json.dumps(body, separators=(',', ':')).encode()
    headers = [f'{method} {path} HTTP/1.1', 'Host: docker', 'Connection: close',
               f'Content-Length: {len(payload)}']
    if body is not None:
        headers.append('Content-Type: application/json')
    wire = ('\r\n'.join(headers) + '\r\n\r\n').encode() + payload
    process = subprocess.Popen(['docker', 'system', 'dial-stdio'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out = threading.Event()

    def expire():
        timed_out.set()
        process.kill()

    timer = threading.Timer(timeout, expire)
    timer.start()
    try:
        process.stdin.write(wire)
        process.stdin.flush()
        response = http.client.HTTPResponse(_ResponseBuffer(process.stdout))
        response.begin()
        raw = response.read()
    except (http.client.HTTPException, OSError) as error:
        raise DockerAPIError('Docker Engine response timed out' if timed_out.is_set()
                             else 'Invalid Docker Engine response') from error
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        timer.cancel()
    if timed_out.is_set():
        raise DockerAPIError('Docker Engine response timed out')
    if process.returncode:
        raise DockerAPIError('Docker transport failed: ' + process.stderr.read().decode(errors='replace')[-1000:])
    if response.status >= 400:
        try:
            message = json.loads(raw).get('message', '')
        except ValueError:
            message = raw.decode(errors='replace')
        raise DockerAPIError(f'Docker API {method} {path} failed ({response.status}): {message[-1000:]}')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def create_container(name, spec):
    return request('POST', '/containers/create?name=' + quote(name, safe=''), spec)
