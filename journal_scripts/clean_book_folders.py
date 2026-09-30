#!/usr/bin/env python3
"""Удаляет подпапки без книг. По умолчанию — только просмотр."""
import argparse
import os
from pathlib import Path
import shutil
import sys

EXTENSIONS = {'.djvu', '.epub', '.pdf', '.doc', '.djv'}


def find_candidates(root):
    """Снизу вверх: книга во вложенной папке сохраняет всех её родителей.

    Символические ссылки и точки монтирования сохраняются вместе с родителями.
    При ошибке чтения обход прерывается: непроверенные папки не удаляются.
    """
    keep = {}
    candidates = []

    def fail(error):
        raise error

    for current, dirs, files in os.walk(root, topdown=False,
                                       followlinks=False, onerror=fail):
        folder = Path(current)
        protected = False
        for name in files:
            item = folder / name
            if item.is_symlink() or (item.suffix.lower() in EXTENSIONS
                                     and item.is_file()):
                protected = True
        for name in dirs:
            child = folder / name
            if (child.is_symlink() or os.path.ismount(child)
                    or keep.get(child, False)):
                protected = True
        keep[folder] = protected
        if folder != root and not protected:
            candidates.append(folder)

    # Если удаляется родитель, его подпапки отдельно перечислять не нужно.
    selected = set(candidates)
    return sorted((p for p in candidates if p.parent not in selected),
                  key=lambda p: str(p))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', help='Папка для проверки')
    parser.add_argument('--delete', action='store_true',
                        help='Действительно удалить найденные папки с содержимым')
    args = parser.parse_args()
    root = Path(args.folder).expanduser().resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        parser.error('Укажите существующую папку, отличную от корня диска.')
    candidates = find_candidates(root)
    if not candidates:
        print('Папок для удаления нет.')
        return
    print('Папки для удаления вместе со всем содержимым:')
    for folder in candidates:
        print(f'  {str(folder)!r}')
    print(f'Всего: {len(candidates)}')
    if not args.delete:
        print('Это предварительный просмотр. Для удаления добавьте --delete.')
        return
    # Повторный обход перед удалением защищает от обычных изменений после
    # просмотра. Во время запуска не меняйте файлы другими программами.
    current_candidates = set(find_candidates(root))
    for folder in candidates:
        if folder not in current_candidates:
            print(f'Пропущена изменившаяся папка: {str(folder)!r}')
            continue
        shutil.rmtree(folder)
        print(f'Удалена: {str(folder)!r}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as error:
        print(f'Ошибка: {error}. Дальнейшее удаление остановлено.', file=sys.stderr)
        sys.exit(1)
