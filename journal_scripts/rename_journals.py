#!/usr/bin/env python3
"""Переименовать файлы журналов в формат 'ИмяЖурнала_ГГГГ_НН.ext'.

Имя берётся из имени подпапки; год (4 цифры) и номер (1-3 цифры) — из имени файла.
Файлы без года/номера пропускаются и попадают в список неразобранных.

Использование:
  python3 rename_journals.py            # предпросмотр (dry run)
  python3 rename_journals.py --apply    # переименовать
"""

import os
import re
import sys

ROOTS = [
    "/home/yurik/video/torrents/incomplete/jornals",
"""    "/home/yurik/data/libraryarch/Техническая/Электроника/Журналы","""
]

EXTS = {'.pdf', '.djvu', '.djv'}

MONTHS = {
    'january': '01', 'february': '02', 'march': '03', 'april': '04', 'may': '05',
    'june': '06', 'july': '07', 'august': '08', 'september': '09', 'october': '10',
    'november': '11', 'december': '12',
}


def parse_year_issue(stem):
    """Вернуть (год, номер) из имени файла или (None, None)."""
    # Годовые диапазоны (сборники «1993-2002») пропускаем.
    if len(re.findall(r'(?:19|20)\d{2}', stem)) >= 2:
        return None, None
    ym = re.search(r'(?:19|20)\d{2}', stem)
    if not ym:
        return None, None
    year = ym.group(0)
    after = stem[ym.end():]
    before = stem[:ym.start()]
    # номер после года: "2010 10", "2010'10", "2010-12", "201402" -> "10"/"12"/"02"
    m = re.match(r'[\s№\'\-_\.]*(\d{1,3})(?:-(\d{1,3}))?', after)
    if m:
        issue = m.group(1) + ('-' + m.group(2) if m.group(2) else '')
        return year, issue
    # номер до года в скобках (кумулятивный): "31(380) 2008" -> "31"
    m = re.search(r'(\d{1,3})\s*\(\d+\)[\s№\'\-_\.]*$', before)
    if m:
        return year, m.group(1)
    # номер до года: "05_2009", "№11 2010", "01 2010" -> "05"/"11"/"01"
    m = re.search(r'(\d{1,3})(?:-(\d{1,3}))?[\s№\'\-_\.]*$', before)
    if m:
        issue = m.group(1) + ('-' + m.group(2) if m.group(2) else '')
        return year, issue
    # месяц текстом: "january_2006" -> "01"
    low = stem.lower()
    for month_name, num in MONTHS.items():
        if month_name in low:
            return year, num
    return year, None


def collect_files():
    """Собрать (полный_путь, имя_журнала, имя_файла) для всех pdf/djvu."""
    items = []   # (full_path, magazine, filename)
    loose = []   # (full_path, filename) — файлы без подпапки
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            sub = os.path.join(root, entry)
            if os.path.isdir(sub):
                for dirpath, _dirs, files in os.walk(sub):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in EXTS:
                            items.append((os.path.join(dirpath, f), entry, f))
            elif os.path.isfile(sub) and os.path.splitext(entry)[1].lower() in EXTS:
                loose.append((sub, entry))
    return items, loose


def main():
    apply = '--apply' in sys.argv
    items, loose = collect_files()

    planned = []      # (full_path, new_name, magazine)
    skipped = []      # (full_path, reason)
    loose_names = {}  # magazine -> inferred name (for loose files, best effort)

    for full, magazine, filename in items:
        stem = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1].lower()
        if ext == '.djv':
            ext = '.djvu'
        # Пропустить уже переименованные файлы ("Magazine_YYYY_MM[_lang]").
        if re.match(r'^.+_(?:19|20)\d{2}_\d{1,3}(?:-\d{1,3})?(?:_(?:ua|ru))?$', stem):
            continue
        year, issue = parse_year_issue(stem)
        if year and issue:
            new_name = '%s_%s_%s%s' % (magazine, year, issue, ext)
            planned.append((full, new_name, magazine))
        else:
            skipped.append((full, 'нет года/номера'))

    # Свободные файлы (без подпапки) — имя из префикса имени файла
    for full, filename in loose:
        stem = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1].lower()
        if ext == '.djv':
            ext = '.djvu'
        year, issue = parse_year_issue(stem)
        if year and issue:
            # имя = префикс до первого разделителя/числа
            name = re.split(r'[\s№\'\-_\.]*(19|20)\d{2}', stem, maxsplit=1)[0].strip(' _-.')
            name = name or 'Unknown'
            new_name = '%s_%s_%s%s' % (name, year, issue, ext)
            planned.append((full, new_name, name))
        else:
            skipped.append((full, 'нет года/номера (свободный файл)'))

    # Обработка коллизий одинаковых целевых имён
    seen = {}
    final_plan = []
    for full, new_name, magazine in planned:
        if new_name in seen:
            seen[new_name] += 1
            base, ext = os.path.splitext(new_name)
            new_name = '%s (%d)%s' % (base, seen[new_name], ext)
        else:
            seen[new_name] = 1
        final_plan.append((full, new_name))

    print('=== Предпросмотр' + (' (ПРИМЕНЕНИЕ)' if apply else '') + ' ===')
    print('К переименованию: %d файлов' % len(final_plan))
    print('Пропущено (не разобрано): %d файлов' % len(skipped))
    print()

    if apply:
        for full, new_name in final_plan:
            dest = os.path.join(os.path.dirname(full), new_name)
            try:
                os.rename(full, dest)
            except OSError as e:
                print('ОШИБКА при переименовании %s: %s' % (full, e))
        print('Переименовано: %d файлов' % len(final_plan))
    else:
        # показать несколько примеров
        print('--- Примеры переименования ---')
        for full, new_name in final_plan[:25]:
            print('  %s  ->  %s' % (os.path.basename(full), new_name))
        print()

    if skipped:
        print('--- Пропущенные файлы (%d) ---' % len(skipped))
        for full, reason in skipped[:60]:
            print('  [%s] %s' % (reason, full))
        if len(skipped) > 60:
            print('  ... и ещё %d' % (len(skipped) - 60))
        # сохранить полный список
        with open('/tmp/journals_skipped.txt', 'w') as f:
            for full, reason in skipped:
                f.write('%s\t%s\n' % (reason, full))
        print()
        print('Полный список пропущенных: ~/journals_skipped.txt')


if __name__ == '__main__':
    main()
