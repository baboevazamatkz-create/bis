"""Fetch the exact Windows dependency wheels used in the verified AOSR bundle."""
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).parent
NUGET = 'https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg'
PYTHON_HASH = '0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8'


def verified_download(url, target, digest):
    with urlopen(url, timeout=90) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"SHA-256 mismatch for {target.name}")
    target.write_bytes(data)


def download_wheel(wheels, line):
    digest, filename = line.split('  ', 1)
    distribution, version, _ = filename.split('-', 2)
    with urlopen(f'https://pypi.org/pypi/{distribution}/{version}/json', timeout=40) as response:
        info = json.load(response)
    asset = next(file for file in info['urls'] if file['filename'] == filename)
    if asset['digests']['sha256'] != digest:
        raise ValueError(f"PyPI digest differs for {filename}")
    verified_download(asset['url'], wheels / filename, digest)


if __name__ == '__main__':
    staging = Path(sys.argv[1])
    wheels = staging / 'wheels'
    wheels.mkdir(parents=True, exist_ok=True)
    entries = (ROOT / 'windows-wheels.txt').read_text().splitlines()
    with ThreadPoolExecutor(max_workers=8) as pool:
        python = pool.submit(verified_download, NUGET, staging / 'python-3.12.10.nupkg', PYTHON_HASH)
        jobs = [pool.submit(download_wheel, wheels, entry) for entry in entries]
        python.result()
        for job in jobs:
            job.result()
    print(f'Verified Python and {len(entries)} Windows wheels against SHA-256.')
