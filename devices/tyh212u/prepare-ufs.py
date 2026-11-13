#!/usr/bin/env python3
"""Stage the shared QogirN6Pro UFS driver for the pinned kernel baseline."""
from pathlib import Path
import runpy
import subprocess

HERE = Path(__file__).resolve().parent
REVISION = '020b970e351eee3887e6caaf2d653926e7086e44'


def apply(kernel):
    kernel = Path(kernel).resolve()
    head = subprocess.check_output(
        ['git', '-c', 'safe.directory=' + str(kernel), '-C', str(kernel),
         'rev-parse', 'HEAD'], text=True).strip()
    if head != REVISION:
        raise ValueError('Expected the pinned Linux 6.18.54 baseline')
    runpy.run_path(str(HERE.parent / 'common/ufs.py'))['apply'](kernel)


if __name__ == '__main__':
    import sys
    apply(sys.argv[1])
