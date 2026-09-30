# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2025 Calibre-Web contributors
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import base64
import json
import os
import re
import threading
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

from cps import config, logger, db, helper
from cps.isoLanguages import get_lang3
from cps.search_metadata import cl as metadata_providers
from cps.services.Metadata import MetaRecord, MetaSourceInfo
import sys
sys.path.insert(1, '/app/calibre-web-automated/scripts/')
from cwa_db import CWA_DB

log = logger.create()

def fetch_and_apply_metadata(book_id: int, user_enabled: bool = False, filename: str = None) -> bool:
    """
    Fetch metadata for a newly ingested book and apply it if settings allow.
    
    Args:
        book_id: The ID of the book to fetch metadata for
        user_enabled: Deprecated parameter - metadata fetching is now admin-controlled only
        filename: Optional original filename (used for periodical/issue detection)
        
    Returns:
        bool: True if metadata was successfully fetched and applied, False otherwise
    """
    try:
        if not db.CalibreDB.session_factory:
            log.error("CalibreDB not initialized; skipping metadata fetch")
            return False

        # Check global settings (admin-controlled only)
        cwa_db = CWA_DB()
        cwa_settings = cwa_db.get_cwa_settings()
        
        if not cwa_settings.get('auto_metadata_fetch_enabled', False):
            log.debug("Auto metadata fetch disabled by administrator")
            return False
            
        # Get the book
        calibre_db_instance = db.CalibreDB(expire_on_commit=False, init=True)
        book = calibre_db_instance.get_book(book_id)
        if not book:
            log.error(f"Book with ID {book_id} not found")
            return False
            
        # Try local AI metadata extraction first (if enabled)
        ai_applied = False
        if cwa_settings.get('auto_metadata_ai_enabled', False):
            ai_metadata = _fetch_metadata_from_ai(book, calibre_db_instance)
            if ai_metadata is not None:
                if _apply_metadata_to_book(book, ai_metadata, calibre_db_instance):
                    log.info("Successfully applied AI metadata for book: %s", book.title)
                    ai_applied = True

        # Detect a periodical (magazine) from the original filename.
        periodical = _detect_periodical(filename)

        # Parse [Series] and {tags} markers from the original filename.
        filename_series, filename_tags = _parse_filename_markers(filename)

        # If AI already set the metadata and the book has an annotation, we're done.
        if ai_applied and _book_has_description(book):
            if periodical is not None:
                _apply_periodical(book, periodical[0], periodical[1], periodical[2], periodical[3], calibre_db_instance)
            _apply_filename_series_and_tags(
                book, filename_series if periodical is None else None,
                filename_tags, calibre_db_instance)
            calibre_db_instance.session.close()
            return True
            
        # Create search query from book title and author
        search_query = book.title
        if book.authors:
            author_names = [author.name for author in book.authors]
            search_query += " " + " ".join(author_names)
            
        log.info(f"Fetching metadata for: {search_query}")
        
        # Get provider hierarchy
        try:
            provider_hierarchy = json.loads(cwa_settings.get('metadata_provider_hierarchy', '["google","douban","dnb","ibdb","comicvine"]'))
        except (json.JSONDecodeError, TypeError):
            provider_hierarchy = ["google", "douban", "dnb", "ibdb", "comicvine"]

        # Global provider enablement map
        enabled_map = _parse_metadata_providers_enabled(
            cwa_settings.get('metadata_providers_enabled', '{}')
        )
            
        # Try each provider in order
        metadata_found = False
        for provider_id in provider_hierarchy:
            # Check if explicitly disabled (default is enabled if not specified)
            is_enabled = enabled_map.get(provider_id, True)
            if not is_enabled:
                log.debug(f"Provider {provider_id} is globally disabled")
                continue
            try:
                # Find the provider
                provider = None
                for p in metadata_providers:
                    if p.__id__ == provider_id:
                        provider = p
                        break
                        
                if not provider or not provider.active:
                    continue
                    
                log.debug(f"Trying metadata provider: {provider.__name__}")
                
                # Search for metadata
                results = provider.search(search_query, "", "en")
                if not results or len(results) == 0:
                    continue
                    
                # Use the first result
                metadata = results[0]
                
                # Apply metadata to book
                if ai_applied:
                    # AI already set title/authors; only take the annotation (description).
                    if _apply_description_only(book, metadata.description, calibre_db_instance):
                        log.info(f"Applied description from {provider.__name__} for book: {book.title}")
                        metadata_found = True
                        break
                else:
                    if _apply_metadata_to_book(book, metadata, calibre_db_instance):
                        log.info(f"Successfully applied metadata from {provider.__name__} for book: {book.title}")
                        metadata_found = True
                        break
                    
            except Exception as e:
                log.warning(f"Error fetching metadata from provider {provider_id}: {e}")
                continue
                
        # Periodical (magazine) detection: unique title + series + series index + year.
        if periodical is not None:
            _apply_periodical(book, periodical[0], periodical[1], periodical[2], periodical[3], calibre_db_instance)

        # Apply [Series] and {tags} markers from the filename (series only for non-periodicals).
        _apply_filename_series_and_tags(
            book, filename_series if periodical is None else None,
            filename_tags, calibre_db_instance)

        calibre_db_instance.session.close()
        return ai_applied or metadata_found
        
    except Exception as e:
        log.error(f"Error in fetch_and_apply_metadata: {e}", exc_info=True)
        return False


_LANG_MAP = {
    "ru": "ru", "rus": "ru", "russian": "ru", "русский": "ru",
    "en": "en", "eng": "en", "english": "en", "английский": "en",
    "de": "de", "deu": "de", "ger": "de", "german": "de", "немецкий": "de",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr", "французский": "fr",
    "es": "es", "spa": "es", "spanish": "es", "испанский": "es",
    "uk": "uk", "ukr": "uk", "ukrainian": "uk", "украинский": "uk",
    "it": "it", "ita": "it", "italian": "it", "итальянский": "it",
    "pl": "pl", "pol": "pl", "polish": "pl", "польский": "pl",
}


def _normalize_language(lang):
    """Map a language name or code to an ISO639-1 code (e.g. 'Russian' -> 'ru')."""
    if not lang:
        return ""
    key = str(lang).strip().lower()
    if key in _LANG_MAP:
        return _LANG_MAP[key]
    return str(lang).strip()


def _render_first_pages(book, count: int = 3):
    """Render the first ``count`` pages of the book's PDF format to PNGs via Ghostscript.

    Returns a list of PNG paths (possibly shorter than ``count`` if the PDF has fewer
    pages or rendering fails). Returns an empty list if there is no PDF.
    """
    import subprocess
    import tempfile
    book_dir = os.path.join(config.get_book_path(), book.path)
    if not os.path.isdir(book_dir):
        return []
    pdf_file = None
    for name in sorted(os.listdir(book_dir)):
        if name.lower().endswith('.pdf'):
            pdf_file = os.path.join(book_dir, name)
            break
    if not pdf_file:
        return []
    out_paths = []
    for page in range(1, count + 1):
        out_png = os.path.join(tempfile.gettempdir(), '_cwa_page_%s_%d.png' % (book.id, page))
        try:
            result = subprocess.run(
                ['gs', '-dNOPAUSE', '-dBATCH', '-sDEVICE=png16m', '-r150',
                 '-dFirstPage=%d' % page, '-dLastPage=%d' % page,
                 '-sOutputFile=' + out_png, pdf_file],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            log.debug("Failed to render page %d for book %s: %s", page, book.id, e)
            break
        if result.returncode != 0 or not os.path.isfile(out_png) or os.path.getsize(out_png) == 0:
            break
        out_paths.append(out_png)
    return out_paths


def _render_last_page(book):
    """Render the last page (back cover) of the book's PDF to a PNG via Ghostscript.

    The annotation/description is often printed on the back cover. Returns the PNG
    path, or ``None`` if there is no PDF, the page count is unknown, or rendering
    fails.
    """
    import subprocess
    import tempfile
    book_dir = os.path.join(config.get_book_path(), book.path)
    if not os.path.isdir(book_dir):
        return None
    pdf_file = None
    for name in sorted(os.listdir(book_dir)):
        if name.lower().endswith('.pdf'):
            pdf_file = os.path.join(book_dir, name)
            break
    if not pdf_file:
        return None
    # Get the page count via pypdf (robust for paths with spaces/Cyrillic).
    try:
        import pypdf
        page_count = len(pypdf.PdfReader(pdf_file).pages)
    except Exception as e:
        log.debug("Failed to get page count for book %s: %s", book.id, e)
        page_count = 0
    if not page_count:
        return None
    out_png = os.path.join(tempfile.gettempdir(), '_cwa_lastpage_%s.png' % book.id)
    try:
        result = subprocess.run(
            ['gs', '-dNOPAUSE', '-dBATCH', '-sDEVICE=png16m', '-r150',
             '-dFirstPage=%d' % page_count, '-dLastPage=%d' % page_count,
             '-sOutputFile=' + out_png, pdf_file],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        log.debug("Failed to render last page for book %s: %s", book.id, e)
        return None
    if result.returncode != 0 or not os.path.isfile(out_png) or os.path.getsize(out_png) == 0:
        return None
    return out_png


def _local(tag):
    """Return the local (namespace-stripped) name of an XML tag."""
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _find_all(root, localname):
    return [e for e in root.iter() if _local(e.tag) == localname]


def _extract_epub_cover_and_meta(epub_path):
    """Extract the cover image and metadata from an EPUB file.

    Returns ``(cover_bytes_or_None, meta_dict)``. Parses the OPF package document
    (no external dependency): title, authors, description, subjects, language,
    publisher and the cover image referenced by the manifest.
    """
    meta = {'title': '', 'authors': [], 'description': '', 'tags': [], 'language': '', 'publisher': ''}
    cover_bytes = None
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_path = None
            try:
                container = ET.fromstring(zf.read('META-INF/container.xml'))
                for e in container.iter():
                    if _local(e.tag) == 'rootfile':
                        opf_path = e.get('full-path')
                        if opf_path:
                            break
            except Exception:
                opf_path = None
            if not opf_path:
                for name in zf.namelist():
                    if name.lower().endswith('.opf'):
                        opf_path = name
                        break
            if not opf_path:
                return cover_bytes, meta

            root = ET.fromstring(zf.read(opf_path))
            metadata = next((e for e in root.iter() if _local(e.tag) == 'metadata'), None)
            if metadata is None:
                return cover_bytes, meta

            def dc_field(name):
                for e in _find_all(metadata, name):
                    if e.text and e.text.strip():
                        return e.text.strip()
                return ''

            meta['title'] = dc_field('title')
            meta['authors'] = [e.text.strip() for e in _find_all(metadata, 'creator') if e.text and e.text.strip()]
            meta['description'] = dc_field('description')
            meta['tags'] = [e.text.strip() for e in _find_all(metadata, 'subject') if e.text and e.text.strip()]
            meta['language'] = dc_field('language')
            meta['publisher'] = dc_field('publisher')

            # Find the cover image href from the manifest.
            cover_id = None
            for e in _find_all(metadata, 'meta'):
                if e.get('name') == 'cover':
                    cover_id = e.get('content')
                    break
            manifest = next((e for e in root.iter() if _local(e.tag) == 'manifest'), None)
            if manifest is not None:
                cover_href = None
                for item in _find_all(manifest, 'item'):
                    if item.get('id') == cover_id or item.get('properties') == 'cover-image':
                        cover_href = item.get('href')
                        break
                if cover_href:
                    import posixpath
                    base = posixpath.dirname(opf_path)
                    full = posixpath.normpath(posixpath.join(base, cover_href)) if base else cover_href
                    try:
                        cover_bytes = zf.read(full)
                    except Exception:
                        cover_bytes = None
        return cover_bytes, meta
    except Exception as e:
        log.debug("Failed to parse EPUB metadata %s: %s", epub_path, e)
        return None, {}


def _extract_fb2_cover_and_meta(fb2_path):
    """Extract the cover image and metadata from a FictionBook2 (.fb2) file.

    Returns ``(cover_bytes_or_None, meta_dict)``. Parses ``<title-info>`` (title,
    authors, annotation, genres, language, publisher) and the coverpage image.
    """
    meta = {'title': '', 'authors': [], 'description': '', 'tags': [], 'language': '', 'publisher': ''}
    cover_bytes = None
    try:
        root = ET.parse(fb2_path).getroot()
        title_info = next((e for e in root.iter() if _local(e.tag) == 'title-info'), None)
        if title_info is not None:
            for e in _find_all(title_info, 'book-title'):
                if e.text and e.text.strip():
                    meta['title'] = e.text.strip()
                    break
            authors = []
            for author in _find_all(title_info, 'author'):
                parts = []
                for tag in ('first-name', 'middle-name', 'last-name'):
                    for e in _find_all(author, tag):
                        if e.text and e.text.strip():
                            parts.append(e.text.strip())
                            break
                name = ' '.join(parts).strip()
                if name:
                    authors.append(name)
            meta['authors'] = authors
            for ann in _find_all(title_info, 'annotation'):
                text = ''.join(ann.itertext()).strip()
                if text:
                    meta['description'] = text
                    break
            meta['tags'] = [g.text.strip() for g in _find_all(title_info, 'genre') if g.text and g.text.strip()]
            for lang in _find_all(title_info, 'lang'):
                if lang.text and lang.text.strip():
                    meta['language'] = lang.text.strip()
                    break
            for pub in _find_all(title_info, 'publisher'):
                if pub.text and pub.text.strip():
                    meta['publisher'] = pub.text.strip()
                    break
        # Cover image from <coverpage><image .../></coverpage> -> <binary id=...>
        coverpage = next((e for e in root.iter() if _local(e.tag) == 'coverpage'), None)
        if coverpage is not None:
            for img in _find_all(coverpage, 'image'):
                href = img.get('href') or img.get('{http://www.w3.org/1999/xlink}href')
                if not href:
                    continue
                href = href.lstrip('#')
                for binary in _find_all(root, 'binary'):
                    if binary.get('id') == href and binary.text:
                        try:
                            cover_bytes = base64.b64decode(binary.text)
                        except Exception:
                            cover_bytes = None
                        break
                if cover_bytes:
                    break
        return cover_bytes, meta
    except Exception as e:
        log.debug("Failed to parse FB2 metadata %s: %s", fb2_path, e)
        return None, {}


def _save_cover_temp(book_id, data):
    """Save raw cover image bytes to a temporary JPEG and return its path."""
    import io
    import tempfile
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert('RGB')
        path = os.path.join(tempfile.gettempdir(), '_cwa_cover_%s.jpg' % book_id)
        img.save(path, 'JPEG', quality=85)
        return path
    except Exception as e:
        log.debug("Failed to decode cover image for book %s: %s", book_id, e)
        return None


def _get_book_source(book, book_dir):
    """Determine what to send to the AI for a book.

    Returns ``(image_paths, embedded_meta, is_rendered)``:
    - ``image_paths``: image files for the AI (PDF pages, or a single extracted
      epub/fb2 cover, or cover.jpg).
    - ``embedded_meta``: dict with structured metadata extracted from epub/fb2
      (title, authors, description/annotation, tags, language, publisher).
    - ``is_rendered``: True if image_paths are rendered PDF pages (so cover_page
      is a 1..N index), False for a single cover image.
    """
    embedded_meta = {}
    if not os.path.isdir(book_dir):
        return [], embedded_meta, False
    files = sorted(os.listdir(book_dir))

    pdf = next((f for f in files if f.lower().endswith('.pdf')), None)
    if pdf:
        paths = _render_first_pages(book, count=4)
        if paths:
            return paths, embedded_meta, True

    epub = next((f for f in files if f.lower().endswith('.epub')), None)
    if epub:
        cover_bytes, meta = _extract_epub_cover_and_meta(os.path.join(book_dir, epub))
        if meta:
            embedded_meta = meta
        if cover_bytes:
            tmp = _save_cover_temp(book.id, cover_bytes)
            if tmp:
                return [tmp], embedded_meta, False

    fb2 = next((f for f in files if f.lower().endswith('.fb2')), None)
    if fb2:
        cover_bytes, meta = _extract_fb2_cover_and_meta(os.path.join(book_dir, fb2))
        if meta:
            embedded_meta = meta
        if cover_bytes:
            tmp = _save_cover_temp(book.id, cover_bytes)
            if tmp:
                return [tmp], embedded_meta, False

    cover = os.path.join(book_dir, 'cover.jpg')
    if os.path.isfile(cover):
        return [cover], embedded_meta, False

    return [], embedded_meta, False


def _extract_book_text(book, book_dir, max_chars=12000):
    """Extract plain text from a book (PDF text layer / epub / fb2) for a text-only AI.

    Returns a string (possibly empty). Scanned PDFs without a text layer return ''.
    """
    if not os.path.isdir(book_dir):
        return ''
    files = sorted(os.listdir(book_dir))

    # PDF: text layer via pypdf
    pdf = next((f for f in files if f.lower().endswith('.pdf')), None)
    if pdf:
        try:
            import pypdf
            reader = pypdf.PdfReader(os.path.join(book_dir, pdf))
            parts = []
            total = 0
            for page in reader.pages[:10]:
                txt = (page.extract_text() or '').strip()
                if txt:
                    parts.append(txt)
                    total += len(txt)
                    if total >= max_chars:
                        break
            return '\n'.join(parts)[:max_chars]
        except Exception:
            return ''

    # EPUB: text from XHTML/HTML content files
    epub = next((f for f in files if f.lower().endswith('.epub')), None)
    if epub:
        try:
            with zipfile.ZipFile(os.path.join(book_dir, epub)) as zf:
                parts = []
                total = 0
                for name in zf.namelist():
                    if name.lower().endswith(('.xhtml', '.html', '.htm')):
                        raw = zf.read(name).decode('utf-8', errors='ignore')
                        txt = re.sub(r'<[^>]+>', ' ', raw)
                        txt = re.sub(r'\s+', ' ', txt).strip()
                        if txt:
                            parts.append(txt)
                            total += len(txt)
                            if total >= max_chars:
                                break
            return (' '.join(parts))[:max_chars]
        except Exception:
            return ''

    # FB2: text from the XML body
    fb2 = next((f for f in files if f.lower().endswith('.fb2')), None)
    if fb2:
        try:
            root = ET.parse(os.path.join(book_dir, fb2)).getroot()
            texts = []
            for e in root.iter():
                if _local(e.tag) in ('p', 'title', 'subtitle'):
                    if e.text and e.text.strip():
                        texts.append(e.text.strip())
            return ('\n'.join(texts))[:max_chars]
        except Exception:
            return ''

    return ''


def _ai_text_mode():
    """Return True if the configured AI is text-only (e.g. DeepSeek)."""
    return bool(CWA_DB().get_cwa_settings().get('ai_text_mode'))


def _ask_ai(ai_url, image_paths, task_text):
    """Send ``image_paths`` + ``task_text`` to the AI and return the parsed JSON dict.

    Supports both a local single-model server and external OpenAI-compatible
    providers (DeepSeek, OpenRouter, ...). Settings ``ai_api_key``, ``ai_model`` and
    ``ai_text_mode`` are read from the CWA settings.
    """
    cwa_settings = CWA_DB().get_cwa_settings()
    api_key = (cwa_settings.get('ai_api_key') or '').strip()
    model = (cwa_settings.get('ai_model') or '').strip()
    text_mode = bool(cwa_settings.get('ai_text_mode'))

    if text_mode:
        # Text-only model (e.g. DeepSeek): send a plain text message.
        content = task_text
    else:
        image_parts = []
        for path in image_paths:
            with open(path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            mime = 'image/png' if path.lower().endswith('.png') else 'image/jpeg'
            image_parts.append(
                {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}}
            )
        content = [{"type": "text", "text": task_text}] + image_parts

    payload = {
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
    }
    if model:
        payload["model"] = model

    base = ai_url.rstrip('/')
    if base.endswith('/chat/completions'):
        endpoint = base
    elif base.endswith('/v1') or base.endswith('/openai'):
        # Providers whose base already includes the API path (OpenRouter, Gemini
        # OpenAI-compat endpoint, ...).
        endpoint = base + "/chat/completions"
    else:
        # Local llama-server / DeepSeek / OpenAI: append the versioned path.
        endpoint = base + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8"))
    content = resp["choices"][0]["message"]["content"]
    return json.loads(content)


def _fetch_metadata_from_ai(book, calibre_db_instance):
    """Extract book metadata using a local AI server.

    Sources an image (rendered PDF pages, an extracted epub/fb2 cover, or the
    existing cover.jpg), asks the AI for metadata, and merges it with structured
    metadata parsed from epub/fb2 (title, authors, annotation/description, etc.).
    """
    try:
        cwa_db = CWA_DB()
        cwa_settings = cwa_db.get_cwa_settings()
        ai_url = (cwa_settings.get('ai_metadata_url') or '').strip()
        if not ai_url:
            return None

        book_dir = os.path.join(config.get_book_path(), book.path)

        page_paths, embedded_meta, is_rendered = _get_book_source(book, book_dir)
        if not page_paths:
            log.debug("No image source for AI extraction: %s", book.path)
            return None

        # Text-only AI (e.g. DeepSeek): single request with the extracted book text.
        if _ai_text_mode():
            text = _extract_book_text(book, book_dir)
            if not text:
                log.debug("No text for text-only AI extraction: %s", book.path)
                return None
            prompt = (
                "Извлеки полные метаданные книги и верни строгий JSON: "
                '{"title": "...", "authors": ["Фамилия И. О."], "series": "...", "series_index": <число или null>, '
                '"tags": ["..."], "publisher": "...", "language": "ru", "description": "..."}. '
                "Если поле не удалось определить — верни пустую строку, пустой список или null.\n\n"
                "Текст книги:\n" + text
            )
            result = _ask_ai(ai_url, [], prompt)

            emb = embedded_meta or {}
            title = (emb.get('title') or '').strip() or (result.get('title') or '').strip()
            authors = (emb.get('authors') or []) or [a.strip() for a in (result.get('authors') or []) if a and str(a).strip()]
            tags = (emb.get('tags') or []) or [t.strip() for t in (result.get('tags') or []) if t and str(t).strip()]
            publisher = (emb.get('publisher') or '').strip() or (result.get('publisher') or '').strip()
            description = (emb.get('description') or '').strip() or (result.get('description') or '').strip()
            lang = _normalize_language(emb.get('language') or '') or _normalize_language(result.get('language') or '')
            series = (result.get('series') or '').strip() or None
            series_index = None
            if result.get('series_index') is not None:
                try:
                    series_index = str(float(result['series_index']))
                except (TypeError, ValueError):
                    series_index = None

            if not title and not authors:
                return None
            record = MetaRecord(
                id="local_ai",
                title=title or book.title,
                authors=authors,
                url="",
                source=MetaSourceInfo(id="local_ai", description="Local AI (text)", link=""),
            )
            record.tags = tags
            record.publisher = publisher or None
            if description:
                record.description = description
            if series:
                record.series = series
            if series_index:
                record.series_index = series_index
            if lang:
                try:
                    record.languages = [get_lang3(lang)]
                except Exception:
                    record.languages = []
            return record

        # Local AI settings.
        series_enabled = bool(cwa_settings.get('ai_metadata_series_enabled', True))
        annotation_enabled = bool(cwa_settings.get('ai_metadata_annotation_enabled', True))

        # Stage 1: cover + title + authors (+ language/tags/publisher).
        if is_rendered:
            stage1_text = ("Это первые страницы книги (по порядку, начиная с первой). "
                           "Определи, какая из них является обложкой книги (если есть). "
                           "Верни строгий JSON: "
                           '{"cover_page": <номер страницы 1..%d или null>, "title": "...", '
                           '"authors": ["..."], "language": "ru", "tags": ["..."], '
                           '"publisher": "..."}. "authors" — полные имена авторов (имя и фамилия '
                           'полностью). "language" — код ISO639-1 из двух букв (ru, en, de, ...). '
                           'Поля, которых нет, — null или пустой массив.'
                           ) % len(page_paths)
        else:
            stage1_text = ("Это обложка книги. Верни строгий JSON: "
                           '{"title": "...", "authors": ["..."], "language": "ru", '
                           '"tags": ["..."], "publisher": "..."}. Поля, которых нет, — null или '
                           'пустой массив.')
        stage1 = _ask_ai(ai_url, page_paths, stage1_text)

        ai_title = (stage1.get("title") or "").strip()
        ai_authors = [a.strip() for a in (stage1.get("authors") or []) if a and str(a).strip()]
        ai_tags = [t.strip() for t in (stage1.get("tags") or []) if t and str(t).strip()]
        ai_publisher = (stage1.get("publisher") or "").strip()
        ai_lang = _normalize_language(stage1.get("language") or "")
        cover_page = stage1.get("cover_page")

        # Stage 2: series + series number.
        ai_series = None
        ai_series_index = None
        if series_enabled:
            series_text = ("Найди на этих страницах указание на серию/цикл/том/книгу/часть/выпуск "
                           "и её номер. Верни строгий JSON: "
                           '{"series": "название серии или null", "series_index": <номер (число) или null>}. '
                           'Номер может быть арабским или римским. Если есть имя серии без номера — '
                           'номер null. Если серии нет — оба поля null.')
            try:
                stage2 = _ask_ai(ai_url, page_paths, series_text)
                ai_series = (stage2.get("series") or "").strip() or None
                raw_idx = stage2.get("series_index")
                if isinstance(raw_idx, bool):
                    raw_idx = None
                if isinstance(raw_idx, (int, float)) and raw_idx:
                    ai_series_index = str(float(raw_idx))
                elif isinstance(raw_idx, str) and raw_idx.strip():
                    try:
                        ai_series_index = str(float(raw_idx.strip()))
                    except ValueError:
                        ai_series_index = None
            except Exception as e:
                log.debug("AI series detection failed for book %s: %s", book.id, e)

        # Stage 3: annotation (first pages + back cover).
        ai_description = ""
        if annotation_enabled:
            anno_images = list(page_paths)
            back_cover = None
            if is_rendered:
                back_cover = _render_last_page(book)
                if back_cover:
                    anno_images = anno_images + [back_cover]
            anno_text = ("Найди аннотацию/описание книги (краткий текст о её содержании). "
                         'Верни строгий JSON: {"description": "текст аннотации или null"}. '
                         'Не выдумывай: если аннотации нет — null.')
            try:
                stage3 = _ask_ai(ai_url, anno_images, anno_text)
                ai_description = (stage3.get("description") or "").strip()
            except Exception as e:
                log.debug("AI annotation extraction failed for book %s: %s", book.id, e)
            if back_cover:
                try:
                    os.remove(back_cover)
                except OSError:
                    pass

        # Structured epub/fb2 metadata is authoritative; the AI fills the gaps.
        title = (embedded_meta.get('title') or '').strip() or ai_title
        authors = embedded_meta.get('authors') or ai_authors
        tags = (embedded_meta.get('tags') or []) or ai_tags
        publisher = (embedded_meta.get('publisher') or '').strip() or ai_publisher
        description = (embedded_meta.get('description') or '').strip() or ai_description
        lang = _normalize_language(embedded_meta.get('language') or '') or ai_lang
        series = (embedded_meta.get('series') or '').strip() or ai_series
        series_index = embedded_meta.get('series_index') or ai_series_index

        cover_path = os.path.join(book_dir, 'cover.jpg')

        # Save the cover: PDF -> identified page; epub/fb2 -> the extracted image.
        if is_rendered:
            cover_index = None
            if cover_page is not None:
                try:
                    idx = int(cover_page) - 1
                    if 0 <= idx < len(page_paths):
                        cover_index = idx
                except (TypeError, ValueError):
                    cover_index = None
            if cover_index is not None:
                try:
                    from PIL import Image
                    Image.open(page_paths[cover_index]).convert('RGB').save(cover_path, 'JPEG', quality=85)
                    book.has_cover = 1
                    log.info("AI-detected cover saved for book %s (page %d)", book.id, cover_index + 1)
                except Exception as e:
                    log.warning("Failed to save AI-detected cover for book %s: %s", book.id, e)
        else:
            # Single image that is a freshly-extracted cover (not the existing cover.jpg).
            if page_paths[0] != cover_path:
                try:
                    from PIL import Image
                    Image.open(page_paths[0]).convert('RGB').save(cover_path, 'JPEG', quality=85)
                    book.has_cover = 1
                    log.info("Cover extracted and saved for book %s", book.id)
                except Exception as e:
                    log.warning("Failed to save extracted cover for book %s: %s", book.id, e)

        # Clean up temporary images (skip the real cover.jpg).
        for path in page_paths:
            if path == cover_path:
                continue
            try:
                os.remove(path)
            except OSError:
                pass

        if not title and not authors:
            return None

        record = MetaRecord(
            id="local_ai",
            title=title or book.title,
            authors=authors,
            url="",
            source=MetaSourceInfo(id="local_ai", description="Local AI", link=""),
        )
        record.tags = tags
        record.publisher = publisher or None
        if description:
            record.description = description
        if series:
            record.series = series
        if series_index:
            record.series_index = series_index
        if lang:
            try:
                record.languages = [get_lang3(lang)]
            except Exception:
                record.languages = []
        return record
    except Exception as e:
        log.warning("AI metadata extraction failed for book %s: %s",
                    getattr(book, 'id', 'unknown'), e)
        return None


def _normalize_description(description: str) -> str:
    """Ensure the description is stored as HTML (Calibre comments are HTML).

    Plain-text annotations are wrapped in <p> tags and line breaks are turned
    into paragraph breaks so they render correctly in Calibre-Web.
    """
    text = (description or "").strip()
    if not text:
        return ""
    if re.search(r"<[a-z][^>]*>", text, re.IGNORECASE):
        return text
    paras = [p.strip() for p in text.splitlines() if p.strip()]
    return "<p>" + "</p><p>".join(paras) + "</p>"


def _book_has_description(book) -> bool:
    """Return True if the book already has a non-empty comment (annotation)."""
    if book.comments:
        text = book.comments[0].text
        return bool(text and text.strip())
    return False


def _apply_description_only(book, description, calibre_db_instance) -> bool:
    """Apply only the description/annotation to a book (keeps title/authors intact)."""
    description = (description or "").strip()
    if not description:
        return False
    normalized = _normalize_description(description)
    existing = calibre_db_instance.session.query(db.Comments).filter(
        db.Comments.book == book.id).first()
    if existing:
        existing.text = normalized
    elif book.comments:
        book.comments[0].text = normalized
    else:
        comment = db.Comments(normalized, book.id)
        calibre_db_instance.session.add(comment)
        book.comments.append(comment)
    calibre_db_instance.session.commit()
    return True


def _detect_periodical(filename):
    """Detect a periodical (magazine) from a filename.

    Returns ``(name, year, issue)`` or ``None``. ``name`` and ``year`` may be
    empty strings when they cannot be determined from the filename (in which case
    the caller falls back to the book title / no year). Recognises:
      - ``"Magazine_YYYY_MM"`` / ``"Magazine_YYYY_MM-MM"`` (name, year, issue);
      - a leading 3-digit number (``"120 Sistiemnyi Administr"``);
      - a trailing 2-3 digit number (``"PROgrammist08"``).
    """
    if not filename:
        return None
    s = str(filename).strip()
    s = os.path.splitext(s)[0].strip().strip(' ._-')
    if len(s) < 4:
        return None
    # New canonical format: "Magazine_YYYY_MM", "Magazine_YYYY_MM-MM", optional "_ua"/"_ru".
    m = re.match(r'^(.+?)_((?:19|20)\d{2})_(\d{1,3})(?:-(\d{1,3}))?(?:_(ua|ru))?$', s)
    if m:
        name = m.group(1).strip()
        year = m.group(2)
        issue = m.group(3) + ('-' + m.group(4) if m.group(4) else '')
        return name, year, issue, m.group(5) or ''
    # Leading 3-digit issue number (old loose format).
    m = re.match(r'^(\d{3})[\s._\-]+(\D.*)$', s)
    if m:
        return '', '', m.group(1), ''
    # Trailing issue number (old loose format).
    m = re.match(r'^(\D.*?\D)(\d{2,3})$', s)
    if m:
        return '', '', m.group(2), ''
    return None


def _parse_filename_markers(filename):
    """Parse a ``[Series]`` prefix and a ``{tag1, tag2}`` suffix from a filename.

    Returns ``(series_name, tags_list)``. ``series_name`` is ``None`` when there is
    no ``[Series]`` marker; ``tags_list`` is a (possibly empty) list of tag names.
    """
    if not filename:
        return None, []
    s = str(filename).strip()
    s = os.path.splitext(s)[0].strip()
    series_name = None
    tags = []
    m = re.match(r'^\[([^\]]+)\]\s*', s)
    if m:
        series_name = m.group(1).strip()
    m = re.search(r'\{([^{}]+)\}\s*$', s)
    if m:
        tags = [t.strip() for t in m.group(1).split(',') if t.strip()]
    return series_name, tags


def _get_or_create_series_normalized(calibre_db_instance, name):
    """Return an existing series matching ``name`` case-insensitively, or create it.

    SQLite's ``=`` comparison (and even NOCASE collation) does not fold Cyrillic
    case, so an AI may return "Библиотека Электромонтера" while the library stores
    "Библиотека электромонтера". We match those here to avoid duplicate series.
    """
    name = (name or '').strip()
    if not name:
        return None
    series = calibre_db_instance.get_series_by_name(name)
    if series:
        return series
    low = name.lower()
    for existing in calibre_db_instance.session.query(db.Series).all():
        if existing.name and existing.name.lower() == low:
            return existing
    series = db.Series(name, name)
    calibre_db_instance.session.add(series)
    return series


def _get_or_create_publisher(calibre_db_instance, name):
    """Return an existing publisher matching ``name`` case-insensitively, or create it.

    SQLite's ``=`` comparison does not fold Cyrillic case, so the AI may return
    "ЭНЕРГОАТОМИЗДАТ" while the library stores "Энергоатомиздат". Matching those here
    avoids creating duplicate publisher rows that later break ``publishers.name`` UNIQUE.
    """
    name = (name or '').strip()
    if not name:
        return None
    pub = calibre_db_instance.get_publisher_by_name(name)
    if pub:
        return pub
    low = name.lower()
    for existing in calibre_db_instance.session.query(db.Publishers).all():
        if existing.name and existing.name.lower() == low:
            return existing
    pub = db.Publishers(name, name)
    calibre_db_instance.session.add(pub)
    return pub


def _get_or_create_tag(calibre_db_instance, name):
    """Return an existing tag matching ``name`` case-insensitively, or create it.

    Avoids creating duplicate ``tags.name`` rows (same reason as publishers/series).
    """
    name = (name or '').strip()
    if not name:
        return None
    tag = calibre_db_instance.get_tag_by_name(name)
    if tag:
        return tag
    low = name.lower()
    for existing in calibre_db_instance.session.query(db.Tags).all():
        if existing.name and existing.name.lower() == low:
            return existing
    tag = db.Tags(name=name)
    calibre_db_instance.session.add(tag)
    return tag


def _get_or_create_author(calibre_db_instance, name):
    """Return an existing author matching ``name`` case-insensitively, or create it."""
    name = (name or '').strip()
    if not name:
        return None
    author = calibre_db_instance.get_author_by_name(name)
    if author:
        return author
    low = name.lower()
    for existing in calibre_db_instance.session.query(db.Authors).all():
        if existing.name and existing.name.lower() == low:
            return existing
    author = db.Authors(name, helper.get_sorted_author(name.replace('|', ',')))
    calibre_db_instance.session.add(author)
    return author


def _apply_filename_series_and_tags(book, series_name, tags, calibre_db_instance) -> bool:
    """Apply a series name and tags parsed from the original filename.

    The series is only set when the book has no other (periodical) series; tags are
    appended to the existing tags.
    """
    updated = False
    # Clean the title when the filename markers leaked into it (this happens when
    # AI/online metadata failed and the title is still the raw filename, e.g.
    # "[Серия] Заголовок {тег1, тег2}.djvu").
    title = (book.title or '').strip()
    if title:
        cleaned = title
        cleaned = re.sub(r'^\s*\[[^\]]*\]\s*', '', cleaned)          # [Series]
        cleaned = re.sub(r'\s*\{[^{}]*\}\s*$', '', cleaned)          # {tags}
        cleaned = re.sub(r'\s*\.(?:djvu|djv|pdf|epub|fb2|mobi|azw3|docx?)\s*$', '',
                         cleaned, flags=re.IGNORECASE)               # file extension
        cleaned = cleaned.strip(' ._-')
        if cleaned and cleaned != title:
            book.title = cleaned
            updated = True
    if series_name:
        series = _get_or_create_series_normalized(calibre_db_instance, series_name)
        if series is not None and series not in book.series:
            book.series.append(series)
            updated = True
    if tags:
        for tag_name in tags:
            if not tag_name:
                continue
            tag = _get_or_create_tag(calibre_db_instance, tag_name)
            if tag and tag not in book.tags:
                book.tags.append(tag)
                updated = True
    if updated:
        calibre_db_instance.session.commit()
    return updated


def _issue_to_index(issue):
    """'05' -> 5.0 ; '04-05' -> 4.5."""
    if '-' in issue:
        a, b = issue.split('-')
        return (float(a) + float(b)) / 2.0
    return float(issue)


def _apply_periodical(book, name, year, issue, lang, calibre_db_instance) -> bool:
    """Mark a book as a periodical: unique title + series + series index + year."""
    title = (book.title or '').strip()

    if name:
        magazine_name = name + (' (%s)' % lang if lang else '')
    else:
        # Fall back to the book title (from AI/online metadata); strip any leading
        # or trailing issue number the raw filename may have left behind.
        if not title:
            return False
        title = re.sub(r'^%s[\s._\-]+' % issue, '', title)  # "120 Sistiemnyi" -> "Sistiemnyi"
        title = re.sub(r'\d+$', '', title).strip(' ._-')    # "PROgrammist08" -> "PROgrammist"
        if not title:
            return False
        magazine_name = title

    if ('№%s' % issue) not in magazine_name:
        if year:
            book.title = '%s %s №%s' % (magazine_name, year, issue)
        else:
            book.title = '%s №%s' % (magazine_name, issue)

    series = _get_or_create_series_normalized(calibre_db_instance, magazine_name)
    book.series = [series]
    book.series_index = str(_issue_to_index(issue))

    if year:
        try:
            from datetime import datetime
            book.pubdate = datetime(int(year), 1, 1)
        except Exception:
            pass

    log.info("Detected periodical for book %s: %s №%s (%s)", book.id, magazine_name, issue, year or '-')
    calibre_db_instance.session.commit()
    return True


def _apply_metadata_to_book(book, metadata, calibre_db_instance, force: bool = False) -> bool:
    """
    Apply fetched metadata to a book record.
    
    Args:
        book: The book database record
        metadata: The metadata record from provider
        calibre_db_instance: Database instance
        force: When True, bypass the "smart application" heuristics and apply
            every field unconditionally (used by the AI-edit / revert flow).
        
    Returns:
        bool: True if metadata was successfully applied
    """
    try:
        # Get CWA settings to check smart application preference and field selections
        cwa_db = CWA_DB()
        cwa_settings = cwa_db.get_cwa_settings()
        use_smart_application = cwa_settings.get('auto_metadata_smart_application', False) and not force
        
        updated = False
        
        # Update title - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_title', True) and 
            metadata.title and metadata.title.strip()):
            if use_smart_application:
                if len(metadata.title.strip()) > len(book.title.strip()):
                    book.title = metadata.title.strip()
                    updated = True
            else:
                book.title = metadata.title.strip()
                updated = True
            
        # Update authors - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_authors', True) and 
            metadata.authors and len(metadata.authors) > 0):
            # Clear existing authors
            book.authors.clear()
            for author_name in metadata.authors:
                if author_name and author_name.strip():
                    author = _get_or_create_author(calibre_db_instance, author_name.strip())
                    if author:
                        book.authors.append(author)
            # Keep author_sort in sync with the (new) authors so Calibre-Web can
            # display them in the right order (avoids stale author_sort).
            book.author_sort = ' & '.join(a.sort for a in book.authors if a.sort)
            updated = True
            
        # Update description - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_description', True) and 
            metadata.description and metadata.description.strip()):
            normalized_description = _normalize_description(metadata.description)
            current_description = book.comments[0].text if book.comments else ""
            if use_smart_application:
                if len(normalized_description) > len(current_description):
                    if book.comments:
                        book.comments[0].text = normalized_description
                    else:
                        comment = db.Comments(normalized_description, book.id)
                        calibre_db_instance.session.add(comment)
                        book.comments.append(comment)
                    updated = True
            else:
                if book.comments:
                    book.comments[0].text = normalized_description
                else:
                    comment = db.Comments(normalized_description, book.id)
                    calibre_db_instance.session.add(comment)
                    book.comments.append(comment)
                updated = True
            
        # Update publisher - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_publisher', True) and 
            metadata.publisher and metadata.publisher.strip()):
            if use_smart_application:
                if not book.publishers or len(book.publishers) == 0:
                    publisher = _get_or_create_publisher(calibre_db_instance, metadata.publisher.strip())
                    if publisher:
                        book.publishers = [publisher]
                        updated = True
            else:
                # Clear existing publishers and add new one
                book.publishers.clear()
                publisher = _get_or_create_publisher(calibre_db_instance, metadata.publisher.strip())
                if publisher:
                    book.publishers = [publisher]
                    updated = True
                
        # Update tags if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_tags', True) and 
            hasattr(metadata, 'tags') and metadata.tags):
            book.tags.clear()
            for tag_name in metadata.tags:
                if tag_name and tag_name.strip():
                    tag = _get_or_create_tag(calibre_db_instance, tag_name.strip())
                    if tag and tag not in book.tags:
                        book.tags.append(tag)
            updated = True
            
        # Update language if available (ISO639-3 codes, e.g. 'rus', 'eng')
        if hasattr(metadata, 'languages') and metadata.languages:
            book.languages.clear()
            for lang_code in metadata.languages:
                if not lang_code:
                    continue
                language = calibre_db_instance.session.query(db.Languages).filter(
                    db.Languages.lang_code == lang_code).first()
                if not language:
                    language = db.Languages(lang_code)
                    calibre_db_instance.session.add(language)
                if language not in book.languages:
                    book.languages.append(language)
            updated = True
            
        # Update series if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_series', True) and 
            hasattr(metadata, 'series') and metadata.series and metadata.series.strip()):
            series = _get_or_create_series_normalized(calibre_db_instance, metadata.series.strip())
            book.series.clear()
            book.series.append(series)
            
            # Set series index if available
            if hasattr(metadata, 'series_index') and metadata.series_index:
                try:
                    # Convert to float first to validate, then store as string (DB column is String)
                    float_value = float(metadata.series_index)
                    book.series_index = str(float_value)
                except (ValueError, TypeError):
                    book.series_index = '1.0'
            updated = True
            
        # Update published date if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_published_date', True) and 
            hasattr(metadata, 'publishedDate') and metadata.publishedDate):
            try:
                from datetime import datetime
                if isinstance(metadata.publishedDate, str):
                    # Try to parse various date formats
                    for fmt in ['%Y-%m-%d', '%Y-%m', '%Y']:
                        try:
                            book.pubdate = datetime.strptime(metadata.publishedDate, fmt).date()
                            updated = True
                            break
                        except ValueError:
                            continue
                elif hasattr(metadata.publishedDate, 'date'):
                    book.pubdate = metadata.publishedDate.date()
                    updated = True
            except Exception as e:
                log.warning(f"Error parsing published date: {e}")
                
        # Update rating if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_rating', True) and 
            hasattr(metadata, 'rating') and metadata.rating):
            try:
                rating_value = float(metadata.rating)
                if 0 <= rating_value <= 10:  # Calibre uses 0-10 scale
                    if book.ratings:
                        book.ratings[0].rating = int(rating_value * 2)  # Convert to Calibre's 0-10 scale
                    else:
                        rating = db.Ratings(rating=int(rating_value * 2))
                        calibre_db_instance.session.add(rating)
                        book.ratings = [rating]
                    updated = True
            except (ValueError, TypeError):
                pass
                
        # Update identifiers if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_identifiers', True) and 
            hasattr(metadata, 'identifiers') and metadata.identifiers):
            for identifier_type, identifier_value in metadata.identifiers.items():
                if identifier_type and identifier_value:
                    # Check if identifier already exists
                    existing = False
                    for identifier in book.identifiers:
                        if identifier.type == identifier_type:
                            identifier.val = identifier_value
                            existing = True
                            break
                    if not existing:
                        new_identifier = db.Identifiers(identifier_value, identifier_type, book.id)
                        calibre_db_instance.session.add(new_identifier)
                        book.identifiers.append(new_identifier)
                    updated = True
        
        # Handle cover image - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_cover', True) and 
            hasattr(metadata, 'cover') and metadata.cover):
            if not use_smart_application:
                cover_url = (metadata.cover or "").strip()
                if cover_url and not cover_url.endswith('/static/generic_cover.svg'):
                    try:
                        result, error = helper.save_cover_from_url(cover_url, book.path)
                        if result:
                            book.has_cover = 1
                            updated = True
                        else:
                            log.warning("Failed to save cover for book %s: %s",
                                        getattr(book, 'id', 'unknown'), error)
                    except Exception as e:
                        log.warning("Error downloading cover for book %s: %s",
                                    getattr(book, 'id', 'unknown'), e)
        
        if updated:
            calibre_db_instance.session.commit()
            
        return updated
        
    except Exception as e:
        log.error(f"Error applying metadata to book {getattr(book, 'id', 'unknown')}: {e}")
        calibre_db_instance.session.rollback()
        return False


def _parse_metadata_providers_enabled(raw_value):
    """Lightweight parser for metadata_providers_enabled without importing cwa_functions."""
    try:
        if raw_value is None:
            return {}
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode('utf-8', errors='ignore')
        if isinstance(raw_value, str):
            s = raw_value.strip()
            if not s:
                return {}
            if s.startswith("'") and s.endswith("'"):
                s = s[1:-1]
            if not s:
                return {}
            data = json.loads(s)
            return data if isinstance(data, dict) else {}
        if isinstance(raw_value, dict):
            return raw_value
        return {}
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return {}


AI_EDIT_SNAPSHOT_DIR = '/config/ai_edit_history'


def _ai_edit_snapshot_path(book_id):
    return os.path.join(AI_EDIT_SNAPSHOT_DIR, '%d.json' % book_id)


def ai_edit_save_snapshot(book):
    """Snapshot the current metadata so an AI edit can be reverted (one level)."""
    snap = {
        'title': (book.title or '').strip(),
        'authors': [a.name for a in book.authors],
        'series': book.series[0].name if book.series else '',
        'series_index': book.series_index,
        'tags': [t.name for t in book.tags],
        'publisher': book.publishers[0].name if book.publishers else '',
        'languages': [l.lang_code for l in book.languages],
        'description': book.comments[0].text if book.comments else '',
    }
    try:
        os.makedirs(AI_EDIT_SNAPSHOT_DIR, exist_ok=True)
        with open(_ai_edit_snapshot_path(book.id), 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False)
        return True
    except Exception as e:
        log.warning("Failed to save AI edit snapshot for book %s: %s", book.id, e)
        return False


AI_EDIT_LAST_FILE = os.path.join(AI_EDIT_SNAPSHOT_DIR, '_last.json')


def ai_edit_set_last(book_ids):
    """Remember the list of books edited in the last AI edit (for selection-free revert)."""
    try:
        os.makedirs(AI_EDIT_SNAPSHOT_DIR, exist_ok=True)
        with open(AI_EDIT_LAST_FILE, 'w', encoding='utf-8') as f:
            json.dump([int(i) for i in book_ids], f)
        return True
    except Exception as e:
        log.warning("Failed to save last AI-edited books: %s", e)
        return False


def ai_edit_get_last():
    """Return the list of book ids edited in the last AI edit (or an empty list)."""
    if not os.path.isfile(AI_EDIT_LAST_FILE):
        return []
    try:
        with open(AI_EDIT_LAST_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return [int(i) for i in data]
    except Exception:
        return []


def ai_edit_clear_last():
    """Remove the remembered list of last AI-edited books."""
    try:
        if os.path.isfile(AI_EDIT_LAST_FILE):
            os.remove(AI_EDIT_LAST_FILE)
    except OSError:
        pass


def ai_edit_restore_snapshot(book, calibre_db_instance):
    """Restore metadata from the saved snapshot (one-level revert)."""
    path = _ai_edit_snapshot_path(book.id)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            snap = json.load(f)
    except Exception:
        return False
    record = MetaRecord(
        id="ai_revert",
        title=snap.get('title') or book.title,
        authors=snap.get('authors') or [],
        url="",
        source=MetaSourceInfo(id="ai_revert", description="AI revert", link=""),
    )
    record.tags = snap.get('tags') or []
    record.publisher = snap.get('publisher') or None
    if snap.get('series'):
        record.series = snap['series']
        if snap.get('series_index'):
            record.series_index = snap['series_index']
    if snap.get('description'):
        record.description = snap['description']
    if snap.get('languages'):
        record.languages = snap['languages']
    _apply_metadata_to_book(book, record, calibre_db_instance, force=True)
    try:
        os.remove(path)
    except OSError:
        pass
    return True


def ai_edit_metadata(book, task, calibre_db_instance):
    """Run a natural-language metadata edit task on ``book`` via the local AI.

    Renders the first pages (+ back cover) and asks the AI to perform ``task``.
    Returns ``(MetaRecord_or_None, error_string)``.
    """
    cwa_db = CWA_DB()
    cwa_settings = cwa_db.get_cwa_settings()
    ai_url = (cwa_settings.get('ai_metadata_url') or '').strip()
    if not ai_url:
        return None, "AI Server URL is not configured"

    book_dir = os.path.join(config.get_book_path(), book.path)
    page_paths, embedded_meta, is_rendered = _get_book_source(book, book_dir)
    page_paths = list(page_paths)

    current = {
        'title': (book.title or '').strip(),
        'authors': [a.name for a in book.authors],
        'series': book.series[0].name if book.series else '',
        'series_index': book.series_index,
        'tags': [t.name for t in book.tags],
        'publisher': book.publishers[0].name if book.publishers else '',
        'language': book.languages[0].lang_code if book.languages else '',
        'description': book.comments[0].text if book.comments else '',
    }
    prompt = (
        "Это книга. Текущие метаданные (JSON):\n" + json.dumps(current, ensure_ascii=False) +
        "\n\nЗадача пользователя: " + (task or '').strip() +
        "\n\nВыполни задачу и верни строгий JSON с ОБНОВЛЁННЫМИ метаданными (все поля): "
        '{"title": "...", "authors": ["..."], "series": "...", "series_index": <число или null>, '
        '"tags": ["..."], "publisher": "...", "language": "ru", "description": "..."}. '
        "Поля, которые не относятся к задаче, оставь без изменений (скопируй из текущих метаданных). "
        "series_index — число (номер тома/выпуска); если серии нет — null."
    )
    if _ai_text_mode():
        text = _extract_book_text(book, book_dir)
        if not text:
            return None, "No text extracted (scanned PDF without a text layer?)"
        prompt += "\n\nТекст книги:\n" + text
        try:
            result = _ask_ai(ai_url, [], prompt)
            log.info("AI edit result for book %s (task=%r): %s",
                     book.id, (task or '').strip(), json.dumps(result, ensure_ascii=False)[:300])
        except Exception as e:
            log.warning("AI edit failed for book %s: %s", book.id, e)
            result = None
    else:
        if not page_paths:
            return None, "No cover/pages available for the book"
        if is_rendered:
            back_cover = _render_last_page(book)
            if back_cover:
                page_paths.append(back_cover)
        try:
            result = _ask_ai(ai_url, page_paths, prompt)
            log.info("AI edit result for book %s (task=%r): %s",
                     book.id, (task or '').strip(), json.dumps(result, ensure_ascii=False)[:300])
        except Exception as e:
            log.warning("AI edit failed for book %s: %s", book.id, e)
            result = None
        finally:
            import tempfile
            tmpdir = tempfile.gettempdir()
            for p in page_paths:
                if os.path.dirname(p) == tmpdir:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
    if result is None:
        return None, "AI request failed"

    record = MetaRecord(
        id="local_ai_edit",
        title=(result.get('title') or '').strip() or (book.title or ''),
        authors=[a.strip() for a in (result.get('authors') or []) if a and str(a).strip()],
        url="",
        source=MetaSourceInfo(id="local_ai_edit", description="Local AI edit", link=""),
    )
    record.tags = [t.strip() for t in (result.get('tags') or []) if t and str(t).strip()]
    record.publisher = (result.get('publisher') or '').strip() or None
    if (result.get('series') or '').strip():
        record.series = result['series'].strip()
    if result.get('series_index') is not None:
        try:
            record.series_index = str(float(result['series_index']))
        except (TypeError, ValueError):
            record.series_index = None
    if (result.get('description') or '').strip():
        record.description = result['description'].strip()
    if result.get('language'):
        lang = _normalize_language(result.get('language') or '')
        if lang:
            try:
                record.languages = [get_lang3(lang)]
            except Exception:
                record.languages = []
    return record, None


def ai_add_tags(book, tags, calibre_db_instance):
    """Append ``tags`` to a book's tags (existing tags are kept)."""
    updated = False
    for raw in tags:
        tag_name = str(raw).strip()
        if not tag_name:
            continue
        tag = _get_or_create_tag(calibre_db_instance, tag_name)
        if tag and tag not in book.tags:
            book.tags.append(tag)
            updated = True
    if updated:
        calibre_db_instance.session.commit()
    return updated


def ai_number_series(books, calibre_db_instance):
    """Ask the AI to order ``books`` (of one series) and return the correct order.

    Returns ``(order_list, error_string)`` where ``order_list`` is a list of 1-based
    indices (a permutation of 1..N) in the correct numbering order, or ``None`` on error.
    """
    cwa_db = CWA_DB()
    cwa_settings = cwa_db.get_cwa_settings()
    ai_url = (cwa_settings.get('ai_metadata_url') or '').strip()
    if not ai_url:
        return None, "AI Server URL is not configured"
    n = len(books)
    if n < 2:
        return None, "Нужно минимум 2 книги"

    images = []
    titles = []
    for i, book in enumerate(books, 1):
        book_dir = os.path.join(config.get_book_path(), book.path)
        paths, _, _ = _get_book_source(book, book_dir)
        images.append(paths[0] if paths else None)
        titles.append('%d. «%s»' % (i, (book.title or '').strip()))

    prompt = (
        "Есть %d книг одной серии (показаны в произвольном порядке). "
        "Определи правильный порядок нумерации от 1 до %d — по номеру тома/части/выпуска "
        "в названии или на обложке.\n\nНазвания книг:\n%s\n\n"
        "Верни строгий JSON: {\"order\": [<номера книг 1..%d в правильном порядке>]}. "
        "Например, если правильный порядок — книга 3, потом 1, потом 2, верни {\"order\": [3, 1, 2]}."
    ) % (n, n, "\n".join(titles), n)

    valid_images = [img for img in images if img]
    try:
        result = _ask_ai(ai_url, valid_images, prompt)
        order = result.get('order') or []
    except Exception as e:
        return None, str(e)
    finally:
        import tempfile
        tmpdir = tempfile.gettempdir()
        for img in images:
            if img and os.path.dirname(img) == tmpdir:
                try:
                    os.remove(img)
                except OSError:
                    pass

    cleaned = []
    for x in order:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= v <= n and v not in cleaned:
            cleaned.append(v)
    if len(cleaned) != n:
        return None, "AI не вернул корректный порядок (ожидалась перестановка 1..%d)" % n
    return cleaned, None


def ai_fetch_metadata(book, calibre_db_instance=None):
    """Extract full metadata from a book's cover/pages via the local AI (single request).

    Returns ``(dict_or_None, error_string)``.
    """
    cwa_db = CWA_DB()
    cwa_settings = cwa_db.get_cwa_settings()
    ai_url = (cwa_settings.get('ai_metadata_url') or '').strip()
    if not ai_url:
        return None, "AI Server URL is not configured"

    book_dir = os.path.join(config.get_book_path(), book.path)
    page_paths, embedded_meta, is_rendered = _get_book_source(book, book_dir)

    prompt = (
        "Извлеки полные метаданные книги и верни строгий JSON: "
        '{"title": "...", "authors": ["Фамилия И. О."], "series": "...", "series_index": <число или null>, '
        '"tags": ["..."], "publisher": "...", "language": "ru", "description": "..."}. '
        "Если поле не удалось определить — верни пустую строку, пустой список или null."
    )

    if _ai_text_mode():
        text = _extract_book_text(book, book_dir)
        if not text:
            return None, "No text extracted (scanned PDF without a text layer?)"
        prompt += "\n\nТекст книги:\n" + text
        try:
            result = _ask_ai(ai_url, [], prompt)
        except Exception as e:
            return None, str(e)
    else:
        if not page_paths:
            return None, "No cover/pages available"
        page_paths = list(page_paths)
        if is_rendered:
            back = _render_last_page(book)
            if back:
                page_paths.append(back)
        try:
            result = _ask_ai(ai_url, page_paths, prompt)
        except Exception as e:
            return None, str(e)
        finally:
            import tempfile
            tmpdir = tempfile.gettempdir()
            for p in page_paths:
                if os.path.dirname(p) == tmpdir:
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    # Prefer structured metadata parsed from epub/fb2 (OPF/XML); fill gaps with the AI.
    emb = embedded_meta or {}

    def _emb_str(k):
        v = emb.get(k)
        return (v or '').strip() if isinstance(v, str) else ''

    def _emb_list(k):
        v = emb.get(k)
        return [str(x).strip() for x in v if x and str(x).strip()] if isinstance(v, (list, tuple)) else []

    title = _emb_str('title') or (result.get('title') or '').strip()
    authors = _emb_list('authors') or [str(a).strip() for a in (result.get('authors') or []) if a and str(a).strip()]
    tags = _emb_list('tags') or [str(t).strip() for t in (result.get('tags') or []) if t and str(t).strip()]
    publisher = _emb_str('publisher') or (result.get('publisher') or '').strip()
    description = _emb_str('description') or (result.get('description') or '').strip()

    data = {
        'title': title,
        'authors': authors,
        'tags': tags,
        'series': (result.get('series') or '').strip(),
        'series_index': None,
        'publisher': publisher,
        'language': '',
        'description': description,
    }

    lang_src = _emb_str('language') or result.get('language')
    if lang_src:
        lang = _normalize_language(lang_src)
        if lang:
            try:
                data['language'] = get_lang3(lang)
            except Exception:
                data['language'] = ''
    if result.get('series_index') is not None:
        try:
            data['series_index'] = str(float(result['series_index']))
        except (TypeError, ValueError):
            data['series_index'] = None
    return data, None


def ai_fetch_description(book, calibre_db_instance=None):
    """Extract the book's description (annotation) via the local AI.

    Reuses the full metadata extraction (which reliably finds the annotation) and
    returns only the description. Returns ``(description_or_None, error_string)``.
    """
    data, err = ai_fetch_metadata(book, calibre_db_instance)
    if data is None:
        return None, err
    return (data.get('description') or '').strip(), None


_AI_TASK_LOCK = threading.Lock()
_AI_TASK_STATE = {'running': False, 'total': 0, 'done': 0, 'error': ''}


def ai_edit_status():
    """Return the current background AI-edit status."""
    with _AI_TASK_LOCK:
        return dict(_AI_TASK_STATE)


def ai_edit_start_background(book_ids, task, tags, number_series):
    """Start the AI edit operations in a background thread."""
    thread = threading.Thread(
        target=_ai_edit_worker,
        args=(book_ids, task, tags, number_series),
        daemon=True,
    )
    thread.start()


def _ai_edit_worker(book_ids, task, tags, number_series):
    """Run the AI edit operations (tags / numbering / task) in the background."""
    cdb = None
    try:
        with _AI_TASK_LOCK:
            _AI_TASK_STATE.update(running=True, total=len(book_ids), done=0, error='')
        cdb = db.CalibreDB(expire_on_commit=False, init=True)

        # 1. Per-book operations: tags + task
        for book_id in book_ids:
            book = cdb.get_book(book_id)
            if not book:
                continue
            if tags:
                ai_add_tags(book, tags, cdb)
            if task:
                record, err = ai_edit_metadata(book, task, cdb)
                if record is not None:
                    _apply_metadata_to_book(book, record, cdb, force=True)
            with _AI_TASK_LOCK:
                _AI_TASK_STATE['done'] += 1

        # 2. Bulk operation: number the series
        if number_series and len(book_ids) >= 2:
            books = [cdb.get_book(bid) for bid in book_ids]
            books = [b for b in books if b]
            order, err = ai_number_series(books, cdb)
            if order is not None:
                for pos, idx in enumerate(order, 1):
                    books[idx - 1].series_index = str(pos)
                cdb.session.commit()
    except Exception as e:
        with _AI_TASK_LOCK:
            _AI_TASK_STATE['error'] = str(e)
        log.error("AI background task error: %s", e)
    finally:
        if cdb is not None:
            try:
                cdb.session.close()
            except Exception:
                pass
        with _AI_TASK_LOCK:
            _AI_TASK_STATE['running'] = False
