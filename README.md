# RSS Scraper

Scarica i nuovi post da una lista di feed RSS e li salva come file Markdown, preservando titolo, autore, data, tag e URL originale. Il corpo dell'articolo viene convertito in Markdown pulito (senza classi CSS o attributi HTML superflui) e le immagini vengono scaricate nella cartella del post, nel punto in cui compaiono nell'articolo.

## Requisiti

- Python ≥ 3.13
- [pandoc](https://pandoc.org/installing.html) installato sul sistema
- Dipendenze Python gestite con `uv`

## Installazione

```bash
uv sync
```

## Configurazione

Copia o modifica `config.json`:

```json
{
  "output_dir": "./posts",
  "feeds": [
    {
      "name": "Nome della fonte",
      "url": "https://esempio.com/feed.xml",
      "output_subdir": "nome-fonte",
      "from_date": "2025-01-01"
    }
  ]
}
```

- `output_dir`: cartella radice dove verranno salvati i post.
- `name`: nome leggibile della fonte.
- `url`: URL del feed RSS/Atom.
- `output_subdir` (opzionale): sottocartella dedicata alla fonte. Se omessa viene generata dallo slug del `name`.
- `from_date` (opzionale): non vengono importati i post precedenti a questa data (formato `YYYY-MM-DD`).

## Esecuzione

```bash
uv run python main.py
```

Per usare un file di configurazione diverso:

```bash
uv run python main.py --config mio-config.json
```

## Struttura dei file generati

```
posts/
└── nome-fonte/
    └── YYYY-MM-DD-titolo-del-post/
        ├── index.md
        └── immagine.png
```

Ogni post ha una cartella dedicata con il file Markdown e le immagini scaricate dal corpo dell'articolo.

## Stato e duplicati

Lo script tiene traccia dei post già importati nel file `state.json`, usando l'URL del post come chiave. Al successivo avvio i post già presenti vengono saltati.

Il file di log `import.log` viene eliminato all'inizio di ogni esecuzione e ricreato, quindi contiene sempre solo l'ultimo import eseguito.
