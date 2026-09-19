#!/usr/bin/env python3
"""Распаковать архивы журналов (zip/rar) и извлечь из них pdf/djvu.

Извлечённые файлы кладутся рядом с архивом. Архивы, где нет pdf/djvu,
заносятся в список необработанных.

Использование:
  python3 unpack_journals.py            # предпросмотр
  python3 unpack_journals.py --apply    # распаковать
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOTS = [
    "/home/yurik/data/libraryarch/Компьютерная/Журналы",
    "/home/yurik/data/libraryarch/Техническая/Электроника/Журналы",
]

EXTS = {'.pdf', '.djvu', '.djv'}


def find_archives():
    result = []
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for dp, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(('.zip', '.rar')):
                    result.append(os.path.join(dp, f))
    return sorted(result)


def unpack(archive):
    """Распаковать архив во временную папку, вернуть список pdf/djvu внутри."""
    tmp = tempfile.mkdtemp(prefix='jrnal_')
    ext = archive.lower().rsplit('.', 1)[-1]
    try:
        if ext == 'zip':
            subprocess.run(['unzip', '-q', '-o', archive, '-d', tmp],
                           capture_output=True, check=False)
        elif ext == 'rar':
            subprocess.run(['unrar', 'x', '-y', '-o+', archive, tmp + os.sep],
                           capture_output=True, check=False)
    except Exception:
        pass
    pdfs = []
    for dp, _dirs, files in os.walk(tmp):
        for f in files:
            if os.path.splitext(f)[1].lower() in EXTS:
                pdfs.append(os.path.join(dp, f))
    return pdfs, tmp


def main():
    apply = '--apply' in sys.argv
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:
        pass
    archives = find_archives()
    print('=== Архивов: %d ===' % len(archives))
    print()

    extracted = 0
    no_pdf = []
    for arch in archives:
        pdfs, tmp = unpack(arch)
        names = [os.path.basename(p) for p in pdfs]
        if not pdfs:
            no_pdf.append(arch)
            print('[нет pdf/djvu] %s' % arch)
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        print('[%d файл(а)] %s -> %s' % (len(pdfs), os.path.basename(arch), ', '.join(names[:3])))
        if apply:
            dst_dir = os.path.dirname(arch)
            for p in pdfs:
                base = os.path.basename(p)
                dest = os.path.join(dst_dir, base)
                # избегаем перезаписи
                i = 2
                while os.path.exists(dest):
                    stem, ext = os.path.splitext(base)
                    dest = os.path.join(dst_dir, '%s (%d)%s' % (stem, i, ext))
                    i += 1
                shutil.move(p, dest)
                extracted += 1
        shutil.rmtree(tmp, ignore_errors=True)

    if apply:
        print()
        print('Извлечено файлов: %d' % extracted)

    if no_pdf:
        with open('/tmp/journals_no_pdf.txt', 'w') as f:
            for a in no_pdf:
                f.write(a + '\n')
        print()
        print('Архивов без pdf/djvu: %d -> /tmp/journals_no_pdf.txt' % len(no_pdf))


if __name__ == '__main__':
    main()
