#!/usr/bin/env python3
"""RSS scraper: scarica i nuovi post dai feed configurati e li salva come Markdown."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import mimetypes
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from lxml import html as lh
from readability import Document
from slugify import slugify

CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")
LOG_PATH = Path("import.log")

# Tag mantenuti durante la pulizia dell'HTML prima di passarlo a pandoc.
CLEAN_KEEP_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "blockquote",
    "a",
    "img",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "strong",
    "em",
    "b",
    "i",
    "code",
    "pre",
    "br",
    "hr",
    "sub",
    "sup",
    "s",
    "strike",
    "del",
    "ins",
    "q",
    "abbr",
    "cite",
    "small",
    "mark",
}

# Attributi consentiti durante la pulizia dell'HTML.
CLEAN_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Importa post RSS come Markdown.")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help=f"Percorso del file di configurazione (default: {CONFIG_PATH})",
    )
    return parser.parse_args()


# Regex per trovare immagini nel Markdown generato da Pandoc / HTML residuo.
MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
HTML_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Configurazione
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FeedConfig:
    name: str
    url: str
    output_subdir: str
    from_date: datetime | None


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configurazione non trovata: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if "output_dir" not in data or not isinstance(data["output_dir"], str):
        raise ValueError("Il config deve contenere la chiave 'output_dir' (stringa)")
    if "feeds" not in data or not isinstance(data["feeds"], list):
        raise ValueError("Il config deve contenere la chiave 'feeds' (lista)")

    return data


def parse_feed_config(raw: dict[str, Any]) -> FeedConfig:
    name = raw.get("name", "").strip()
    if not name:
        raise ValueError("Ogni feed deve avere un 'name' non vuoto")

    url = raw.get("url", "").strip()
    if not url:
        raise ValueError(f"Feed '{name}' non ha un 'url' valido")

    output_subdir = raw.get("output_subdir", "").strip()
    if not output_subdir:
        output_subdir = slugify_name(name)

    from_date = None
    if raw.get("from_date"):
        from_date = parse_iso_datetime(str(raw["from_date"]))

    return FeedConfig(name=name, url=url, output_subdir=output_subdir, from_date=from_date)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def setup_logging(log_path: Path = LOG_PATH) -> logging.Logger:
    """Crea un logger che scrive su file (sovrascritto) e console."""
    logger = logging.getLogger("rss_scraper")
    logger.setLevel(logging.INFO)
    logger.handlers = []

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # File sovrascritto ad ogni esecuzione.
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


# --------------------------------------------------------------------------- #
# Stato
# --------------------------------------------------------------------------- #


def load_state(path: Path = STATE_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_state(state: dict[str, str], path: Path = STATE_PATH) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- #
# Utilità
# --------------------------------------------------------------------------- #


def slugify_name(value: str) -> str:
    """Restituisce uno slug sicuro per nomi di file/cartelle."""
    value = unicodedata.normalize("NFKD", value)
    return slugify(value, separator="-", lowercase=True, max_length=80)


def parse_iso_datetime(value: str) -> datetime:
    """Parsa 'YYYY-MM-DD' o datetime ISO."""
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Data non valida: {value}") from exc

    # Se l'utente ha dato solo YYYY-MM-DD, dt è naive a mezzanotte.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def entry_datetime(entry: Any) -> datetime | None:
    """Estrae la data di pubblicazione/aggiornamento da un entry di feedparser."""
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
    return None


def make_post_folder_name(entry: Any, post_date: datetime | None) -> str:
    """Costruisce il nome della cartella per un post: YYYY-MM-DD-slug-titolo."""
    title = entry.get("title", "").strip()
    if not title:
        # Fallback sull'ultimo segmento dell'URL.
        parsed = urlparse(entry.get("link", ""))
        title = Path(parsed.path).name or "untitled"

    title_slug = slugify_name(title)
    date_prefix = post_date.strftime("%Y-%m-%d") if post_date else "no-date"
    folder = f"{date_prefix}-{title_slug}"[:120].rstrip("-")
    return folder


def ensure_unique_dir(parent: Path, name: str) -> Path:
    """Restituisce un percorso di cartella univoco, aggiungendo un contatore se necessario."""
    candidate = parent / name
    if not candidate.exists():
        return candidate

    counter = 2
    while True:
        candidate = parent / f"{name}-{counter}"
        if not candidate.exists():
            return candidate
        counter += 1


def ensure_unique_file(parent: Path, filename: str) -> Path:
    """Restituisce un percorso di file univoco, inserendo il contatore prima dell'estensione."""
    candidate = parent / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# --------------------------------------------------------------------------- #
# Download & estrazione
# --------------------------------------------------------------------------- #


def fetch_html(url: str) -> str:
    """Scarica la pagina HTML."""
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def extract_article_html(html: str, fallback_title: str = "") -> tuple[str, str]:
    """Estrae il contenuto principale della pagina con readability."""
    doc = Document(html)
    article_html = doc.summary()
    title = doc.title() or fallback_title
    return article_html, title


def _unwrap_element(el: Any) -> None:
    """Rimuove un elemento mantenendo il suo contenuto (testo + figli)."""
    parent = el.getparent()
    if parent is None:
        return

    index = parent.index(el)
    text = (el.text or "") + (el.tail or "")

    # Inserisce i figli come fratelli dell'elemento da rimuovere.
    for child in reversed(el):
        parent.insert(index, child)

    # Ricolloca il testo (prima dei figli se ce ne sono, altrimenti nel punto giusto).
    if text:
        if len(el) > 0:
            first = el[0]
            first.text = text + (first.text or "")
        else:
            if index == 0:
                parent.text = (parent.text or "") + text
            else:
                prev = parent[index - 1]
                prev.tail = (prev.tail or "") + text

    parent.remove(el)


def clean_html_for_pandoc(html: str) -> str:
    """Rimuove classi, id, attributi superflui e tag contenitiori non semantici."""
    tree = lh.fromstring(html)

    # Rimuove tag che non devono finire nel Markdown.
    for tag in ("script", "style", "noscript"):
        for el in tree.iter(tag):
            el.drop_tree()

    # Rimuove tutti gli attributi tranne quelli essenziali.
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        keep = CLEAN_ALLOWED_ATTRS.get(el.tag, set())
        for attr in list(el.attrib.keys()):
            if attr not in keep:
                del el.attrib[attr]

    # Rimuove i tag contenitori mantenendone il contenuto.
    for el in list(tree.iter()):
        if not isinstance(el.tag, str):
            continue
        if el.tag in CLEAN_KEEP_TAGS:
            continue
        _unwrap_element(el)

    return lh.tostring(tree, encoding="unicode")


# --------------------------------------------------------------------------- #
# Pandoc
# --------------------------------------------------------------------------- #


def check_pandoc() -> None:
    """Verifica che pandoc sia disponibile."""
    try:
        subprocess.run(["pandoc", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "pandoc non trovato. Installalo prima di eseguire lo script."
        ) from exc


def html_to_markdown(html: str) -> str:
    """Converte HTML in Markdown usando pandoc."""
    result = subprocess.run(
        ["pandoc", "-f", "html", "-t", "markdown", "--wrap=none"],
        input=html,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


# --------------------------------------------------------------------------- #
# Immagini
# --------------------------------------------------------------------------- #


def ext_from_url(url: str, content_type: str | None) -> str:
    """Determina l'estensione del file immagine."""
    # 1. Content-Type
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext

    # 2. Estensione nell'URL
    parsed = urlparse(url)
    path = parsed.path
    if path:
        guessed = mimetypes.guess_extension(mimetypes.guess_type(path)[0] or "")
        if guessed:
            return guessed
        if "." in path:
            ext = Path(path).suffix.lower()
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico"}:
                return ext

    return ".bin"


def safe_image_filename(url: str, content_type: str | None) -> str:
    """Genera un nome file pulito per un'immagine."""
    parsed = urlparse(url)
    base = Path(parsed.path).stem
    if base:
        base = slugify_name(base) or "img"
    else:
        base = "img"

    ext = ext_from_url(url, content_type)
    return f"{base}{ext}"[:100]


def decode_data_uri(url: str) -> tuple[bytes, str | None]:
    """Decodifica un'immagine inline data URI."""
    match = re.match(r"data:([^;]+);base64,(.+)", url, re.IGNORECASE)
    if not match:
        return b"", None

    mime = match.group(1)
    data = base64.b64decode(match.group(2))
    return data, mime


def download_image(url: str, post_url: str) -> tuple[bytes, str | None]:
    """Scarica un'immagine o decodifica un data URI."""
    if url.startswith("data:"):
        data, mime = decode_data_uri(url)
        return data, mime

    absolute_url = urljoin(post_url, url)
    response = requests.get(absolute_url, timeout=30)
    response.raise_for_status()
    return response.content, response.headers.get("Content-Type")


def process_images(markdown: str, post_dir: Path, post_url: str, logger: logging.Logger) -> str:
    """Scarica le immagini referenziate nel Markdown e aggiorna i riferimenti."""
    downloaded: dict[str, str] = {}  # url originale -> nome file locale

    def replace_image(match: re.Match, pattern_type: str) -> str:
        if pattern_type == "md":
            alt, url = match.group(1), match.group(2)
        else:
            url = match.group(1)
            alt = ""

        if url in downloaded:
            local = downloaded[url]
        else:
            try:
                data, content_type = download_image(url, post_url)
                if not data:
                    logger.warning("  Immagine vuota o non decodificabile: %s", url[:80])
                    return match.group(0)

                filename = safe_image_filename(url, content_type)
                local_path = ensure_unique_file(post_dir, filename)
                filename = local_path.name

                image_path = post_dir / filename
                image_path.write_bytes(data)
                downloaded[url] = filename
                local = filename
                logger.debug("  Immagine salvata: %s", filename)
            except Exception as exc:
                logger.warning("  Errore scaricando immagine %s: %s", url[:80], exc)
                return match.group(0)

        if pattern_type == "md":
            return f"![{alt}](./{local})"
        return match.group(0).replace(url, f"./{local}")

    # Sostituisce le immagini in stile Markdown.
    md = MD_IMAGE_RE.sub(lambda m: replace_image(m, "md"), markdown)
    # Sostituisce eventuali tag <img> residui.
    md = HTML_IMG_RE.sub(lambda m: replace_image(m, "html"), md)
    return md


# --------------------------------------------------------------------------- #
# Frontmatter & salvataggio
# --------------------------------------------------------------------------- #


def build_frontmatter(entry: Any, feed_name: str) -> str:
    """Costruisce il frontmatter YAML per il post."""
    title = entry.get("title", "").strip() or "Untitled"
    author = entry.get("author", "").strip() or ""
    date = ""
    dt = entry_datetime(entry)
    if dt:
        date = dt.isoformat()

    tags = []
    if "tags" in entry:
        tags = [str(t.get("term", t)) for t in entry.tags if t]

    source_url = entry.get("link", "").strip()

    lines = ["---"]
    lines.append(f'title: "{escape_yaml(title)}"')
    if author:
        lines.append(f'author: "{escape_yaml(author)}"')
    if date:
        lines.append(f"date: {date}")
    if tags:
        lines.append("tags:")
        for tag in tags:
            lines.append(f'  - "{escape_yaml(tag)}"')
    if source_url:
        lines.append(f"source_url: {source_url}")
    lines.append(f'feed: "{escape_yaml(feed_name)}"')
    lines.append("---\n")
    return "\n".join(lines)


def escape_yaml(value: str) -> str:
    """Escaping minimo per valori racchiusi tra doppi apici."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# --------------------------------------------------------------------------- #
# Pipeline per singolo feed
# --------------------------------------------------------------------------- #


def process_feed(
    feed_config: FeedConfig,
    output_root: Path,
    state: dict[str, str],
    logger: logging.Logger,
) -> int:
    """Elabora un feed RSS e restituisce il numero di post importati."""
    logger.info("=" * 60)
    logger.info("Controllo feed: %s", feed_config.name)

    feed = feedparser.parse(feed_config.url)
    if feed.bozo and feed.get("status", 200) not in (200, 301, 302):
        logger.error("  Errore parsing feed %s: %s", feed_config.url, feed.get("bozo_exception"))
        return 0

    entries = feed.entries

    to_import: list[tuple[Any, datetime | None]] = []
    already_imported = 0
    skipped_by_date = 0

    for entry in entries:
        post_url = entry.get("link", "").strip()
        if not post_url:
            logger.warning("  Entry senza URL, saltato")
            continue

        if post_url in state:
            already_imported += 1
            continue

        post_date = entry_datetime(entry)
        if feed_config.from_date and post_date and post_date < feed_config.from_date:
            skipped_by_date += 1
            continue

        to_import.append((entry, post_date))

    from_date_part = f" | saltati per from_date: {skipped_by_date}" if feed_config.from_date else ""
    logger.info(
        "  Post rilevati: %d | da importare: %d | già importati: %d%s",
        len(entries),
        len(to_import),
        already_imported,
        from_date_part,
    )

    imported = 0
    for entry, post_date in to_import:
        post_url = entry.get("link", "").strip()

        try:
            post_dir = import_post(
                entry=entry,
                post_date=post_date,
                feed_config=feed_config,
                output_root=output_root,
                logger=logger,
            )
            state[post_url] = str(post_dir.relative_to(output_root))
            save_state(state)
            imported += 1
        except Exception as exc:
            title = entry.get("title", "").strip() or "Untitled"
            logger.error("  Errore importando '%s': %s", title, exc)

    logger.info("  Importati in questo giro: %d", imported)
    return imported


def import_post(
    entry: Any,
    post_date: datetime | None,
    feed_config: FeedConfig,
    output_root: Path,
    logger: logging.Logger,
) -> Path:
    """Importa un singolo post e restituisce la cartella creata."""
    post_url = entry.get("link", "").strip()
    title = entry.get("title", "").strip()

    feed_dir = output_root / feed_config.output_subdir
    folder_name = make_post_folder_name(entry, post_date)
    post_dir = ensure_unique_dir(feed_dir, folder_name)
    post_dir.mkdir(parents=True, exist_ok=True)

    # Scarica la pagina, estrae il contenuto principale e lo pulisce.
    html = fetch_html(post_url)
    article_html, extracted_title = extract_article_html(html, fallback_title=title)
    article_html = clean_html_for_pandoc(article_html)

    # Converte in Markdown con pandoc.
    markdown = html_to_markdown(article_html)

    # Scarica e sistema le immagini.
    markdown = process_images(markdown, post_dir, post_url, logger)

    # Frontmatter + contenuto.
    frontmatter = build_frontmatter(entry, feed_config.name)
    md_content = frontmatter + "\n" + markdown

    md_path = post_dir / "index.md"
    md_path.write_text(md_content, encoding="utf-8")

    logger.info(
        "  Importo: %s (salvato: %s)",
        title or "Untitled",
        md_path.relative_to(output_root),
    )
    return post_dir


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> None:
    args = parse_args()

    # Elimina il log precedente in modo che resti solo quello dell'ultimo import.
    if LOG_PATH.exists():
        LOG_PATH.unlink()

    logger = setup_logging()
    logger.info("Avvio importazione RSS")

    check_pandoc()

    config = load_config(args.config)
    output_root = Path(config["output_dir"]).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    state = load_state()
    logger.info("Post già tracciati: %d", len(state))

    total_imported = 0
    for raw_feed in config["feeds"]:
        try:
            feed_config = parse_feed_config(raw_feed)
        except ValueError as exc:
            logger.error("Config feed non valido: %s", exc)
            continue

        total_imported += process_feed(feed_config, output_root, state, logger)

    logger.info("=" * 60)
    logger.info("Importazione completata. Totale post importati: %d", total_imported)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.getLogger("rss_scraper").error("Errore fatale: %s", exc)
        sys.exit(1)
