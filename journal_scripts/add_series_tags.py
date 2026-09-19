#!/usr/bin/env python3
"""Добавить [Серия] и/или {теги} к именам файлов книг в папке.

Имя серии добавляется как префикс «[Серия] », теги — как суффикс
«{тег1, тег2}» перед расширением. CWA при импорте распознаёт эти маркеры
и записывает серию и теги в книгу.

Использование:
  python3 add_series_tags.py ПАПКА --series "Название серии"
  python3 add_series_tags.py ПАПКА --tags "тег1, тег2, тег3"
  python3 add_series_tags.py ПАПКА --series "Серия" --tags "т1, т2"
  python3 add_series_tags.py ПАПКА --series "Серия" --dry-run
  python3 add_series_tags.py ПАПКА --tags "т1, т2" -r   # рекурсивно по подпапкам
"""

import argparse
import os
import re
import sys

BOOK_EXTS = {'.pdf', '.djvu', '.djv', '.epub', '.fb2', '.txt', '.mobi', '.azw3', '.doc', '.docx'}


def add_markers(name: str, series: str, tags: list) -> str:
    """Добавить маркеры серии/тегов к имени файла (заменяя уже существующие)."""
    stem, ext = os.path.splitext(name)
    if series:
        stem = re.sub(r'^\s*\[[^\]]*\]\s*', '', stem)          # убрать старую [Серия]
        stem = '[%s] %s' % (series, stem.strip())
    if tags:
        stem = re.sub(r'\s*\{[^{}]*\}\s*$', '', stem)          # убрать старые {теги}
        stem = '%s {%s}' % (stem.strip(), ', '.join(t.strip() for t in tags))
    return stem + ext


def main():
    ap = argparse.ArgumentParser(description='Добавить [Серия]/теги к именам файлов книг')
    ap.add_argument('folder', help='папка с файлами')
    ap.add_argument('-s', '--series', help='имя серии (префикс [Серия])')
    ap.add_argument('-t', '--tags', help='теги через запятую (суффикс {т1, т2})')
    ap.add_argument('-r', '--recursive', action='store_true', help='обрабатывать подпапки рекурсивно')
    ap.add_argument('--dry-run', action='store_true', help='только показать, что будет изменено')
    args = ap.parse_args()

    if not args.series and not args.tags:
        print('Укажите --series и/или --tags')
        sys.exit(1)

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        print('Папка не найдена:', folder)
        sys.exit(1)

    tags = [t.strip() for t in args.tags.split(',') if t.strip()] if args.tags else []

    # собрать файлы
    if args.recursive:
        files = []
        for dp, _dirs, fs in os.walk(folder):
            for f in fs:
                if os.path.splitext(f)[1].lower() in BOOK_EXTS:
                    files.append(os.path.join(dp, f))
    else:
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if os.path.isfile(os.path.join(folder, f))
                 and os.path.splitext(f)[1].lower() in BOOK_EXTS]

    files.sort()
    print('Файлов к обработке: %d' % len(files))
    renamed = 0
    for full in files:
        base = os.path.basename(full)
        new_base = add_markers(base, args.series, tags)
        if new_base == base:
            continue
        dest = os.path.join(os.path.dirname(full), new_base)
        if os.path.exists(dest):
            print('  ПРОПУСК (существует): %s' % new_base)
            continue
        if args.dry_run:
            print('  %s  ->  %s' % (base, new_base))
        else:
            os.rename(full, dest)
            print('  %s  ->  %s' % (base, new_base))
        renamed += 1
    print('Обработано: %d' % renamed)


if __name__ == '__main__':
    main()
