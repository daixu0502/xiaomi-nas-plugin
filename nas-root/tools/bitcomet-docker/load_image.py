"""Import the official BitComet ARM64 image without changing Docker daemon proxy."""
import gzip
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener

REPO = 'wxhere/bitcomet-webui'
TAG = 'v2.22.0'
MANIFEST = 'sha256:c9726d131ec57512fbde1b42fb3ee0da0b5f4159292ade57118f822014e60d63'
REGISTRY = 'https://registry-1.docker.io/v2/' + REPO


class Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header('Authorization')
        return redirected


def main():
    opener = build_opener(ProxyHandler({'https': 'http://127.0.0.1:7890'}), Redirect())
    token = json.load(opener.open('https://auth.docker.io/token?service=registry.docker.io&scope=repository:' + REPO + ':pull', timeout=30))['token']
    headers = {'Authorization': 'Bearer ' + token,
               'Accept': 'application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json'}
    def read(path):
        return opener.open(Request(REGISTRY + path, headers=headers), timeout=60)
    with read('/manifests/' + MANIFEST) as response:
        raw = response.read()
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != MANIFEST:
        raise RuntimeError('Manifest digest mismatch')
    manifest = json.loads(raw)
    with tempfile.TemporaryDirectory(prefix='bitcomet-image-') as folder:
        stage = Path(folder)
        def blob(item, name):
            target = stage / name
            digest = hashlib.sha256()
            with read('/blobs/' + item['digest']) as response, target.open('wb') as out:
                while True:
                    part = response.read(1024 * 1024)
                    if not part: break
                    digest.update(part); out.write(part)
            if 'sha256:' + digest.hexdigest() != item['digest'] or target.stat().st_size != item['size']:
                raise RuntimeError('Blob validation failed')
            return target
        config = blob(manifest['config'], 'config.json')
        configuration = json.loads(config.read_text())
        if configuration['architecture'] != 'arm64' or configuration['os'] != 'linux':
            raise RuntimeError('Wrong platform')
        layers = []
        for n, item in enumerate(manifest['layers']):
            print('Downloading verified layer %d/%d' % (n+1, len(manifest['layers'])), flush=True)
            compressed = blob(item, 'blob-%d.gz' % n)
            name = 'layer-%d.tar' % n
            with gzip.open(compressed, 'rb') as source, (stage / name).open('wb') as out:
                shutil.copyfileobj(source, out)
            digest = hashlib.sha256()
            with (stage / name).open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''): digest.update(block)
            if 'sha256:' + digest.hexdigest() != configuration['rootfs']['diff_ids'][n]:
                raise RuntimeError('Unpacked layer digest mismatch')
            compressed.unlink(); layers.append(name)
        (stage / 'manifest.json').write_text(json.dumps([{'Config':'config.json', 'RepoTags':[REPO+':'+TAG], 'Layers':layers}]))
        archive = stage / 'image.tar'
        with tarfile.open(archive, 'w') as tar:
            for name in ['config.json','manifest.json'] + layers:
                tar.add(stage / name, arcname=name)
        subprocess.run(['/data/docker/docker','load','-i',str(archive)], check=True)
        print('Validated image ID: ' + manifest['config']['digest'], flush=True)


if __name__ == '__main__': main()
