#!/usr/bin/env python3
"""Интерактивная генерация PDF-чеков из CSV/JSON и HTML-шаблонов.

Папки относительно скрипта: data/, templates/, output/, fonts/.
Кириллица: Roboto или DejaVu Sans из fonts/.

PDF строится через Playwright и системный Chrome/Edge — без GTK3.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import tempfile
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    import pandas as pd
except ImportError:
    pd = None

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TEMPLATES_DIR = ROOT / "templates"
OUTPUT_DIR = ROOT / "output"
FONTS_DIR = ROOT / "fonts"

INVOICE_ID_KEYS = ("invoice_id", "invoiceId", "InvoiceId", "id", "ID")


def configure_stdio() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def money_filter(value) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    quantized = number.quantize(Decimal("0.01"))
    formatted = f"{quantized:,.2f}".replace(",", " ")
    return formatted


def to_decimal(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def pick_invoice_id(record: dict) -> str | None:
    for key in INVOICE_ID_KEYS:
        if key in record and record[key] not in (None, ""):
            return str(record[key])
    return None


def as_records(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("invoices", "data", "items", "records"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        if payload and all(isinstance(v, dict) for v in payload.values()):
            records = []
            for key, value in payload.items():
                record = dict(value)
                record.setdefault("invoice_id", key)
                records.append(record)
            return records
        if pick_invoice_id(payload):
            return [payload]
    raise ValueError("JSON должен содержать список чеков или объект с invoice id")


def normalize_invoice(record: dict) -> dict:
    invoice_id = pick_invoice_id(record)
    if not invoice_id:
        raise ValueError("В записи нет invoice id")

    items = record.get("items")
    if not isinstance(items, list) or not items:
        items = [
            {
                "item_name": record.get("item_name") or record.get("description") or "Позиция",
                "quantity": record.get("quantity", 1),
                "price": record.get("price") or record.get("amount") or 0,
            }
        ]

    normalized_items = []
    total = Decimal("0")
    for item in items:
        if not isinstance(item, dict):
            continue
        quantity = to_decimal(item.get("quantity", 1))
        price = to_decimal(item.get("price") or item.get("amount") or 0)
        amount = quantity * price
        total += amount
        normalized_items.append(
            {
                "item_name": item.get("item_name") or item.get("name") or item.get("description") or "Позиция",
                "quantity": quantity,
                "price": price,
                "amount": amount,
            }
        )

    if record.get("total") not in (None, ""):
        total = to_decimal(record.get("total"))

    invoice = dict(record)
    invoice.update(
        {
            "invoice_id": invoice_id,
            "date": record.get("date") or record.get("invoice_date") or "",
            "customer_name": record.get("customer_name") or record.get("customer") or "",
            "customer_address": record.get("customer_address") or record.get("address") or "",
            "currency": record.get("currency") or "RUB",
            "notes": record.get("notes") or "",
            "items": normalized_items,
            "total": total,
        }
    )
    return invoice


def load_json_invoices(path: Path) -> OrderedDict[str, dict]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    invoices: OrderedDict[str, dict] = OrderedDict()
    for record in as_records(payload):
        invoice = normalize_invoice(record)
        invoices[invoice["invoice_id"]] = invoice
    return invoices


def load_csv_invoices(path: Path) -> OrderedDict[str, dict]:
    rows: list[dict]
    if pd is not None:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        rows = frame.to_dict(orient="records")
    else:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

    grouped: OrderedDict[str, dict] = OrderedDict()
    for row in rows:
        invoice_id = pick_invoice_id(row)
        if not invoice_id:
            continue
        if invoice_id not in grouped:
            grouped[invoice_id] = {**row, "items": []}
        grouped[invoice_id]["items"].append(
            {
                "item_name": row.get("item_name") or row.get("name") or row.get("description"),
                "quantity": row.get("quantity", 1),
                "price": row.get("price") or row.get("amount") or 0,
            }
        )

    invoices: OrderedDict[str, dict] = OrderedDict()
    for invoice_id, record in grouped.items():
        invoices[invoice_id] = normalize_invoice(record)
    return invoices


def load_invoices(path: Path) -> OrderedDict[str, dict]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return load_json_invoices(path)
    if suffix == ".csv":
        return load_csv_invoices(path)
    raise ValueError(f"Неподдерживаемый формат: {path.name}")


def list_data_files() -> list[Path]:
    if not DATA_DIR.is_dir():
        return []
    files = [p for p in DATA_DIR.iterdir() if p.is_file() and p.suffix.lower() in {".csv", ".json"}]
    return sorted(files, key=lambda p: p.name.lower())


def list_templates() -> list[Path]:
    if not TEMPLATES_DIR.is_dir():
        return []
    files = [p for p in TEMPLATES_DIR.iterdir() if p.is_file() and p.suffix.lower() in {".html", ".htm"}]
    return sorted(files, key=lambda p: p.name.lower())


def print_header(title: str) -> None:
    line = "=" * 52
    print(f"\n{line}")
    print(f"  {title}")
    print(line)


def choose_from_list(title: str, items: list, label_fn) -> object:
    print_header(title)
    if not items:
        raise SystemExit("Список пуст. Добавьте файлы и запустите скрипт снова.")
    for index, item in enumerate(items, start=1):
        print(f"  {index}. {label_fn(item)}")
    print()
    while True:
        raw = input(f"Выберите пункт [1-{len(items)}]: ").strip()
        if not raw.isdigit():
            print("  Введите номер из списка.")
            continue
        choice = int(raw)
        if 1 <= choice <= len(items):
            return items[choice - 1]
        print("  Номер вне диапазона.")


def font_files() -> tuple[str, Path | None, Path | None]:
    candidates = [
        ("DejaVu Sans", FONTS_DIR / "DejaVuSans.ttf", FONTS_DIR / "DejaVuSans-Bold.ttf"),
        ("Roboto", FONTS_DIR / "Roboto-Regular.ttf", FONTS_DIR / "Roboto-Bold.ttf"),
    ]
    for family, regular, bold in candidates:
        if regular.is_file():
            return family, regular, bold if bold.is_file() else None
    return "sans-serif", None, None


def font_css() -> str:
    family, regular, bold = font_files()
    if not regular:
        return """
        body { font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif; }
        """
    rules = [
        f"""
        @font-face {{
          font-family: "{family}";
          src: url("{regular.as_uri()}");
          font-weight: 400;
          font-style: normal;
        }}
        """
    ]
    if bold:
        rules.append(
            f"""
            @font-face {{
              font-family: "{family}";
              src: url("{bold.as_uri()}");
              font-weight: 700;
              font-style: normal;
            }}
            """
        )
    rules.append(f'body {{ font-family: "{family}", sans-serif; }}')
    return "\n".join(rules)


def render_html(template_name: str, invoice: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["money"] = money_filter
    template = env.get_template(template_name)
    html = template.render(**invoice)
    style = f"<style>{font_css()}</style>"
    if "</head>" in html:
        return html.replace("</head>", f"{style}</head>", 1)
    return style + html


def chromium_executables() -> list[Path]:
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    local_app = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
        program_files / "Microsoft/Edge/Application/msedge.exe",
        program_files / "Google/Chrome/Application/chrome.exe",
        program_files_x86 / "Google/Chrome/Application/chrome.exe",
        local_app / "Google/Chrome/Application/chrome.exe",
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/microsoft-edge"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ]
    return [path for path in candidates if str(path) != "." and path.is_file()]


def generate_pdf_with_playwright(html: str, output_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    last_error: Exception | None = None
    with sync_playwright() as playwright:
        for channel in ("msedge", "chrome", "chromium", None):
            try:
                launch_kwargs = {"headless": True}
                if channel:
                    launch_kwargs["channel"] = channel
                browser = playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:
                last_error = exc
                continue
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                page.emulate_media(media="print")
                page.pdf(
                    path=str(output_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "12mm", "right": "12mm", "bottom": "12mm", "left": "12mm"},
                )
                return
            finally:
                browser.close()
    raise RuntimeError(last_error or "Playwright не смог запустить Chromium")


def generate_pdf_with_system_chromium(html: str, output_path: Path) -> None:
    executables = chromium_executables()
    if not executables:
        raise FileNotFoundError("Chrome/Edge не найден")

    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "invoice.html"
        html_path.write_text(html, encoding="utf-8")
        cmd = [
            str(executables[0]),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--allow-file-access-from-files",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={output_path}",
            html_path.resolve().as_uri(),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if completed.returncode != 0 or not output_path.is_file():
            raise RuntimeError(completed.stderr.strip() or "Chrome/Edge не создал PDF")


def generate_pdf(html: str, output_path: Path) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = output_path.resolve()
    errors: list[str] = []

    try:
        generate_pdf_with_playwright(html, output_path)
        if output_path.is_file() and output_path.stat().st_size > 0:
            return
        errors.append("Playwright завершился без файла PDF")
    except ImportError:
        errors.append("Playwright не установлен")
    except Exception as exc:
        errors.append(f"Playwright: {exc}")

    try:
        generate_pdf_with_system_chromium(html, output_path)
        if output_path.is_file() and output_path.stat().st_size > 0:
            return
        errors.append("Системный Chrome/Edge завершился без файла PDF")
    except Exception as exc:
        errors.append(f"Chrome/Edge: {exc}")

    details = "\n".join(f"  - {item}" for item in errors)
    raise SystemExit(
        "Не удалось создать PDF без GTK3.\n"
        "Установите зависимости: pip install -r requirements.txt\n"
        "На Windows/macOS также нужен Google Chrome или Microsoft Edge.\n"
        f"Причины:\n{details}"
    )


def open_pdf(path: Path) -> None:
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    if system == "Darwin":
        subprocess.run(["open", str(path)], check=False)
        return
    subprocess.run(["xdg-open", str(path)], check=False)


def show_overview(data_files: list[Path], templates: list[Path]) -> None:
    print_header("Генератор PDF-чеков")
    print("  Файлы с данными:")
    if data_files:
        for index, path in enumerate(data_files, start=1):
            print(f"    {index}. {path.name}")
    else:
        print("    (нет CSV/JSON в папке data)")
    print()
    print("  HTML-шаблоны:")
    if templates:
        for index, path in enumerate(templates, start=1):
            print(f"    {index}. {path.name}")
    else:
        print("    (нет HTML в папке templates)")


def pick_named(items: list[Path], name: str) -> Path:
    for item in items:
        if item.name == name:
            return item
    raise SystemExit(f"Не найден файл {name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Генерация PDF-чека из CSV/JSON и HTML-шаблона")
    parser.add_argument("--data", help="Имя файла в папке data, например invoices.json")
    parser.add_argument("--template", help="Имя шаблона в папке templates, например invoice.html")
    parser.add_argument("--invoice", help="Invoice id из выбранного файла")
    parser.add_argument("--no-open", action="store_true", help="Не открывать PDF после генерации")
    return parser.parse_args()


def main() -> None:
    configure_stdio()
    args = parse_args()

    data_files = list_data_files()
    templates = list_templates()
    show_overview(data_files, templates)

    if args.data:
        data_file = pick_named(data_files, args.data)
        print(f"\n  Выбран файл данных: {data_file.name}")
    else:
        data_file = choose_from_list("Выбор файла данных", data_files, lambda p: p.name)

    if args.template:
        template_file = pick_named(templates, args.template)
        print(f"  Выбран шаблон: {template_file.name}")
    else:
        template_file = choose_from_list("Выбор HTML-шаблона", templates, lambda p: p.name)

    try:
        invoices = load_invoices(data_file)
    except Exception as exc:
        raise SystemExit(f"Не удалось прочитать {data_file.name}: {exc}") from exc

    if not invoices:
        raise SystemExit(f"В файле {data_file.name} нет чеков с invoice id.")

    invoice_ids = list(invoices.keys())
    if args.invoice:
        if args.invoice not in invoices:
            raise SystemExit(f"Invoice id {args.invoice} нет в {data_file.name}")
        invoice_id = args.invoice
        print(f"  Выбран invoice id: {invoice_id}")
    else:
        invoice_id = choose_from_list(
            f"Чеки из {data_file.name}",
            invoice_ids,
            lambda value: f"{value}  —  {invoices[value].get('customer_name', '')}",
        )
    invoice = invoices[invoice_id]

    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in invoice_id)
    output_path = OUTPUT_DIR / f"{safe_id}.pdf"
    html = render_html(template_file.name, invoice)
    generate_pdf(html, output_path)

    print_header("Готово")
    print(f"  Файл данных : {data_file.name}")
    print(f"  Шаблон      : {template_file.name}")
    print(f"  Invoice ID  : {invoice_id}")
    print(f"  PDF         : {output_path}")
    print()
    if args.no_open:
        print("  PDF сохранён, открытие пропущено (--no-open).")
    else:
        open_pdf(output_path)
        print("  PDF открыт в системной программе.")


if __name__ == "__main__":
    configure_stdio()
    try:
        main()
    except KeyboardInterrupt:
        print("\nОтменено.")
        sys.exit(130)
