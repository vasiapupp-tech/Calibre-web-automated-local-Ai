#!/usr/bin/env python3
"""Склеить страничные фрагменты журналов в целые PDF.

Находит папки-выпуски (имя вида "1208", "05_2007", "2008-12") внутри подпапок-журналов.
Если в папке есть подпапки ua/ru — склеивает каждую языковую версию отдельно
(суффикс _ua / _ru), иначе склеивает PDF-фрагменты напрямую.

Использование:
  python3 merge_journals.py            # предпросмотр (dry run)
  python3 merge_journals.py --apply    # склеить
"""

import os
import re
import subprocess
import sys

ROOTS = [
    "~/data/libraryarch/Компьютерная/Журналы",
    "~/data/libraryarch/Техническая/Электроника/Журналы",
]


def parse_folder_date(name):
    """Из имени папки вернуть (год, месяц) или None."""
    m = re.match(r'^(\d{1,2})_(\d{4})$', name)          # "05_2007"
    if m:
        return m.group(2), m.group(1).zfill(2)
    m = re.match(r'^(\d{4})[_-](\d{1,2})$', name)        # "2007-05" / "2007_05"
    if m:
        return m.group(1), m.group(2).zfill(2)
    m = re.match(r'^(\d{2})(\d{2})$', name)              # "0508" -> месяц 05, год 2008
    if m and 1 <= int(m.group(1)) <= 12:
        return '20' + m.group(2), m.group(1)
    return None


def page_key(filename):
    m = re.match(r'^(\d+)', filename)
    return int(m.group(1)) if m else 0


def pdf_files_under(path):
    return [os.path.join(dp, f) for dp, _dirs, files in os.walk(path)
            for f in files
            if f.lower().endswith('.pdf')
            and not re.match(r'^.+_(?:19|20)\d{2}_\d{1,3}(?:-\d{1,3})?(?:_(?:ua|ru))?\.pdf$', f, re.IGNORECASE)]


def collect_merge_groups():
    """Вернуть список (magazine, folder, year, month, lang_suffix, pdf_list)."""
    groups = []
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for mag in sorted(os.listdir(root)):
            mag_path = os.path.join(root, mag)
            if not os.path.isdir(mag_path):
                continue
            for name in sorted(os.listdir(mag_path)):
                folder = os.path.join(mag_path, name)
                if not os.path.isdir(folder):
                    continue
                date = parse_folder_date(name)
                if not date:
                    continue
                year, month = date
                # Языковые подпапки ua/ru?
                found_sub = False
                for lang in ('ua', 'ru'):
                    sub = os.path.join(folder, lang)
                    if os.path.isdir(sub):
                        pdfs = pdf_files_under(sub)
                        if pdfs:
                            found_sub = True
                            groups.append((mag, folder, year, month, lang, pdfs))
                if not found_sub:
                    pdfs = pdf_files_under(folder)
                    if pdfs:
                        groups.append((mag, folder, year, month, '', pdfs))
    return groups


def main():
    apply = '--apply' in sys.argv
    groups = collect_merge_groups()
    groups.sort(key=lambda g: (g[0], g[2], g[3], g[4]))

    print('=== Групп для склейки: %d ===' % len(groups))
    print('Всего PDF-фрагментов: %d' % sum(len(g[5]) for g in groups))
    print()

    merged = 0
    for mag, folder, year, month, lang, pdfs in groups:
        pdfs = sorted(pdfs, key=lambda p: page_key(os.path.basename(p)))
        suffix = ('_' + lang) if lang else ''
        out_name = '%s_%s_%s%s.pdf' % (mag, year, month, suffix)
        out_path = os.path.join(folder, out_name)
        print('[%s] %s%s (%d фрагментов) -> %s' % (mag, os.path.basename(folder), suffix, len(pdfs), out_name))
        if apply:
            try:
                import fitz
                out = fitz.open()
                for pdf in pdfs:
                    src = fitz.open(pdf)
                    out.insert_pdf(src)
                    src.close()
                out.save(out_path)
                out.close()
                if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                    merged += 1
                else:
                    print('   ОШИБКА: пустой результат')
            except Exception as e:
                print('   ОШИБКА: %s' % e)

    if apply:
        print()
        print('Склеено: %d' % merged)


if __name__ == '__main__':
    main()
