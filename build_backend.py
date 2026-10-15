"""Setuptools backend with deterministic source archive metadata."""
import gzip
import io
import os
from pathlib import Path
import tarfile

from setuptools.build_meta import (build_wheel, get_requires_for_build_sdist,
                                   get_requires_for_build_wheel, prepare_metadata_for_build_wheel)
from setuptools.build_meta import build_sdist as _build_sdist


def build_sdist(sdist_directory, config_settings=None):
    name = _build_sdist(sdist_directory, config_settings)
    path = Path(sdist_directory) / name
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '315532800'))
    content = io.BytesIO()
    with tarfile.open(path, 'r:gz') as source, tarfile.open(fileobj=content, mode='w', format=tarfile.PAX_FORMAT) as target:
        for member in sorted(source.getmembers(), key=lambda item: item.name):
            data = source.extractfile(member) if member.isfile() else None
            member.mtime = epoch
            member.uid = member.gid = 0
            member.uname = member.gname = ''
            member.pax_headers = {}
            target.addfile(member, data)
    with path.open('wb') as output, gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=epoch) as archive:
        archive.write(content.getvalue())
    return name
