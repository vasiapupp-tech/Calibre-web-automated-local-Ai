#!/bin/bash
SRC="~/data/calibre/books_topic"
DST="~/data/calibre/books_topic_dwl"

# Проверяем, существуют ли директории
if [ ! -d "$SRC" ] || [ ! -d "$DST" ]; then
    echo "Ошибка: одна из директорий не существует."
    exit 1
fi

# Проверяем, есть ли файлы в папке DST
if [ -n "$(find "$DST" -maxdepth 1 -type f -print -quit)" ]; then
    echo "Папка DST не пуста. Перенос пропущен."
    exit 0
fi

# Ищем файлы нужных расширений (регистронезависимо), сортируем, берем первые 50
find "$SRC"  -maxdepth 1 -type f \( -iname "*.pdf" -o -iname "*.djvu" -o -iname "*.djv" -o -iname "*.fb2" -o -iname "*.epub" \) -print0 | sort -z | head -z -n 50 | while IFS= read -r -d '' file; do
    filename=$(basename "$file")
    
    # Отделяем имя файла от расширения
    basename_no_ext="${filename%.*}"
    ext="${filename##*.}"
    
    # Приводим расширение к нижнему регистру
    ext_lower=$(echo "$ext" | tr '[:upper:]' '[:lower:]')
    
    # Нормализуем .djv в .djvu
    if [ "$ext_lower" = "djv" ]; then
        ext_lower="djvu"
    fi
    
    dest_filename="${basename_no_ext}.${ext_lower}"
    dest_file="$DST/$dest_filename"
    
    # Перемещаем с учетом нового имени расширения
    mv "$file" "$dest_file"
    echo "Перемещён: $filename -> $dest_filename"
done
