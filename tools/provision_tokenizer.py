"""Explicit network provisioning, never invoked by an episode or doctor.

Usage: python tools/provision_tokenizer.py --cache-dir /chosen/cache
"""
import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

URL = 'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken'
SHA256 = '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', type=Path, required=True)
    args = parser.parse_args()
    destination = args.cache_dir / hashlib.sha1(URL.encode()).hexdigest()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        data = destination.read_bytes()
    else:
        with urlopen(URL, timeout=60) as response:
            data = response.read(8 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError('Tokenizer checksum mismatch')
        with destination.open('xb') as stream:
            stream.write(data)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError('Existing tokenizer cache checksum mismatch; not overwritten')
    print('Verified o200k_base cache. Set TIKTOKEN_CACHE_DIR to the selected directory.')


if __name__ == '__main__':
    main()
