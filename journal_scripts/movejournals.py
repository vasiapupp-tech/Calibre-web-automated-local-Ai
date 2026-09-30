#!/usr/bin/env python3
"""Перенести переименованные журналы в папку импорта CWA порциями.

Находит файлы формата «Имя_ГГГГ_НН[_ua|ru].pdf/djvu» в папках libraryarch и
перемещает первые N штук в ~/data/calibre/books_topic_dwl (папка импорта CWA).

Если папка импорта не пуста — перенос пропускается, чтобы не перегружать CWA.

Использование:
  python3 movejournals.py              # перенести 50 (если импорт пуст)
  python3 movejournals.py --count N    # перенести N
  python3 movejournals.py --all        # перенести все
  python3 movejournals.py --dry-run    # только показать, что будет перенесено
"""

import os
import re
import shutil
import sys

ROOTS = [
    "~/data/libraryarch/Компьютерная/Журналы",
    "~/data/libraryarch/Техническая/Электроника/Журналы",
]
DST = "~/data/calibre/books_topic_dwl"

PAT = re.compile(
    r'^.+_(?:19|20)\d{2}_\d{1,3}(?:-\d{1,3})?(?:_(?:ua|ru))?\.(?:pdf|djvu|djv)$',
    re.IGNORECASE,
)


def find_renamed():
    files = []
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for dp, _dirs, fs in os.walk(root):
            for f in fs:
                if PAT.match(f):
                    files.append(os.path.join(dp, f))
    return sorted(files)


def main():
    args = sys.argv[1:]
    dry = '--dry-run' in args
    count = 50
    if '--all' in args:
        count = None
    elif '--count' in args:
        i = args.index('--count')
        count = int(args[i + 1])

    if not os.path.isdir(DST):
        print('Ошибка: папка импорта не существует:', DST)
        sys.exit(1)

    if os.listdir(DST):
        print('Папка импорта не пуста. Перенос пропущен (ждём, пока CWA обработает).')
        sys.exit(0)

    files = find_renamed()
    to_move = files if count is None else files[:count]
    print('Найдено переименованных файлов: %d' % len(files))
    print('К переносу: %d' % len(to_move))

    if dry:
        for f in to_move[:20]:
            print('  %s' % os.path.basename(f))
        if len(to_move) > 20:
            print('  ... и ещё %d' % (len(to_move) - 20))
        sys.exit(0)

    moved = 0
    for f in to_move:
        base = os.path.basename(f)
        dest = os.path.join(DST, base)
        i = 2
        while os.path.exists(dest):
            stem, ext = os.path.splitext(base)
            dest = os.path.join(DST, '%s (%d)%s' % (stem, i, ext))
            i += 1
        try:
            shutil.move(f, dest)
            moved += 1
        except OSError as e:
            print('ОШИБКА переноса %s: %s' % (f, e))
    print('Перенесено: %d файлов' % moved)


if __name__ == '__main__':
    main()
