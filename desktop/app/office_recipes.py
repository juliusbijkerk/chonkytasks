"""Deterministic local outputs prepared for approval from a new source item."""
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import unicodedata


RECIPES = {
    'mailToTask': {'title': 'Turn an email into a follow-up task', 'source': 'mail', 'extensions': ()},
    'mailToNote': {'title': 'Save an email as a note', 'source': 'mail', 'extensions': ()},
    'invoiceFiling': {'title': 'File an invoice', 'source': 'files', 'extensions': ('.txt', '.md')},
    'spreadsheetReport': {'title': 'Create a filtered spreadsheet report', 'source': 'files', 'extensions': ('.csv',)},
    'orderDraft': {'title': 'Prepare an order draft', 'source': 'files', 'extensions': ('.txt', '.md')},
}

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_MAIL_BYTES = 256 * 1024
MAX_FIELD_LENGTH = 512
MAX_QUANTITY = 2**53 - 1  # Keep the JSON result exact in browser clients.
DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
AMOUNT = re.compile(r'[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,6})?')
MARKDOWN = re.compile(r'([\\`*_\[\]{}<>#~])')
REPLIED = re.compile(
    r'^(?:On .+ wrote:|[- ]*(?:Original Message|Forwarded message)[- ]*|Begin forwarded message:|>)', re.I,
)


@dataclass(frozen=True, slots=True)
class Output:
    relative_path: str
    content: bytes
    preview: str
    mime_type: str


def prepare(kind: str, data: bytes, filename: str, *, status_filter: str = '', mail: dict | None = None) -> Output:
    """Return the exact result to review. This layer performs no external actions."""
    recipe = RECIPES.get(kind) if isinstance(kind, str) else None
    if recipe is None:
        raise ValueError('This workflow does not have a local recipe.')
    if recipe['source'] == 'mail':
        return prepare_mail(kind, mail)
    if not isinstance(filename, str) or '/' in filename or '\\' in filename:
        raise ValueError('Use the source file name without a directory.')
    source = PurePosixPath(filename)
    if source.suffix.lower() not in recipe['extensions']:
        raise ValueError('The source file type is unsupported for this workflow.')
    if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError('Use a nonempty UTF-8 file no larger than 2 MiB.')
    try:
        text = clean_text(data.decode('utf-8-sig'))
    except UnicodeError:
        raise ValueError('Use a UTF-8 source file.') from None
    if kind == 'spreadsheetReport':
        return spreadsheet(text, source.stem, status_filter)
    values = fields(text)
    if kind == 'invoiceFiling':
        return invoice(values, source, data)
    return order(values)


def clean_text(value: str) -> str:
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    if any(unicodedata.category(ch) in {'Cc', 'Cs'} and ch not in '\n\t' for ch in value):
        raise ValueError('The source contains unsupported control characters.')
    return value


def label(value, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f'Add a {name} field.')
    value = clean_text(value).strip()
    if not value or len(value) > MAX_FIELD_LENGTH or any(ch in value for ch in '\n\t'):
        raise ValueError(f'{name} must be a short single line.')
    return value


def fields(text: str) -> dict[str, set[str]]:
    values = {}
    for line in text.split('\n'):
        name, colon, value = line.partition(':')
        if colon and value.strip():
            values.setdefault(name.strip().casefold(), set()).add(value.strip())
    return values


def required(values, name: str, *aliases: str) -> str:
    found = set().union(*(values.get(key, ()) for key in (name.casefold(), *aliases)))
    if not found:
        raise ValueError(f'Add a {name} field.')
    if len(found) > 1:
        raise ValueError(f'The source contains conflicting {name} fields.')
    return label(next(iter(found)), name)


def valid_date(value: str, name: str) -> str:
    if not DATE.fullmatch(value):
        raise ValueError(f'{name} must be a valid YYYY-MM-DD date.')
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f'{name} must be a valid YYYY-MM-DD date.') from None


def slug(value: str, fallback: str) -> str:
    ascii_text = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    safe = '-'.join(re.findall(r'[a-z0-9]+', ascii_text))[:60].rstrip('-')
    return safe or f'{fallback}-{hashlib.sha256(value.encode()).hexdigest()[:8]}'


def markdown(text: str) -> str:
    return MARKDOWN.sub(r'\\\1', text)


def prepare_mail(kind, mail: dict | None) -> Output:
    if not isinstance(mail, dict) or not isinstance(mail.get('body'), str):
        raise ValueError('The mail message body is unavailable.')
    sender = label(mail.get('sender'), 'Sender')
    subject = label(mail.get('subject'), 'Subject')
    body = clean_text(mail['body'])
    if len(body) > MAX_MAIL_BYTES or len(body.encode()) > MAX_MAIL_BYTES or not body.strip():
        raise ValueError('Use a nonempty email body no larger than 256 KiB.')

    if kind == 'mailToNote':
        # Keep the message readable as text without rendering embedded HTML or links.
        closing_fences = re.findall(r'^[ \t]*(~{3,})(?=[ \t]*$)', body, re.M)
        fence = '~' * max(3, max(map(len, closing_fences), default=2) + 1)
        fenced_body = body if body.endswith('\n') else body + '\n'
        content = f'# {markdown(subject)}\n\n**From** {markdown(sender)}\n\n{fence}\n{fenced_body}{fence}\n'
        return Output(f'note-{slug(subject, "note")}.md', content.encode(), f'{subject}\nFrom {sender}\n\n{body}', 'text/markdown')

    current = []
    for line in body.split('\n'):
        if REPLIED.match(line.lstrip()):
            break
        current.append(line)
    values = fields('\n'.join(current))
    task = required(values, 'Task')
    due = valid_date(required(values, 'Due', 'due date'), 'Due')
    content = f'# Task\n\n- [ ] {markdown(task)}\n- **Due** {due}\n- **From** {markdown(sender)}\n- **Subject** {markdown(subject)}\n'
    preview = f'{task}\nDue {due}\nFrom {sender}\nSubject {subject}'
    return Output(f'task-{slug(task, "task")}.md', content.encode(), preview, 'text/markdown')


def amount(raw: str) -> Decimal:
    if len(raw) > 40 or not AMOUNT.fullmatch(raw) or sum(ch.isdigit() for ch in raw) > 24:
        raise ValueError('Use decimal amounts with at most six decimal places.')
    return Decimal(raw.replace(',', ''))


def invoice(values, source: PurePosixPath, original: bytes) -> Output:
    identifier = required(values, 'Invoice', 'invoice id', 'invoice number', 'invoice #')
    vendor = required(values, 'Vendor')
    issued = valid_date(required(values, 'Date', 'invoice date'), 'Date')
    total = required(values, 'Total', 'invoice total')
    parts = total.split()
    if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r'[A-Za-z]{3}', parts[1])):
        raise ValueError('Total must be a decimal amount with an optional three-letter currency code.')
    amount(parts[0])
    extension = source.suffix.lower()
    path = f'{slug(vendor, "vendor")}/{issued[:7]}/{slug(source.stem, "invoice")}{extension}'
    preview = f'Invoice {identifier}\nVendor {vendor}\nDate {issued}\nTotal {total}\nCopy of the original source file'
    return Output(path, original, preview, 'text/markdown' if extension == '.md' else 'text/plain')


def column(header, name: str, *aliases: str) -> int:
    matches = [index for index, value in enumerate(header) if value.strip().casefold() in (name.casefold(), *aliases)]
    if len(matches) != 1:
        raise ValueError(f'The CSV needs exactly one {name} column.')
    return matches[0]


def spreadsheet(text: str, stem: str, status_filter: str) -> Output:
    if not isinstance(status_filter, str):
        raise ValueError('Use a text value for the status filter.')
    selected = status_filter.strip()
    if selected:
        selected = label(selected, 'Status filter')
    match_status = selected.casefold()
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    count = 0
    total = Decimal(0)
    try:
        header = next(reader, [])
        status = column(header, 'Status')
        value = column(header, 'Amount', 'total', 'measure')
        with localcontext() as context:
            context.prec = 40  # A 2 MiB file cannot overflow this precision with 24-digit inputs.
            for row in reader:
                if not row:
                    continue
                if len(row) != len(header):
                    raise ValueError('Each CSV row must have the same number of cells as the header.')
                if not match_status or row[status].strip().casefold() == match_status:
                    total += amount(row[value].strip())
                    count += 1
    except csv.Error:
        raise ValueError('Check the CSV for unclosed quotes or oversized cells.') from None
    if not count:
        raise ValueError('No rows match the approved status filter.')
    whole, _, fraction = format(total or Decimal(0), 'f').partition('.')
    formatted = f'{whole}.{fraction.rstrip("0").ljust(2, "0")}'
    visible_label = selected or 'All'
    # CSV quoting does not prevent a spreadsheet from evaluating a text cell.
    normalized = unicodedata.normalize('NFKC', visible_label)
    check = ''.join(ch for ch in normalized if unicodedata.category(ch) != 'Cf').lstrip()
    safe_label = "'" + visible_label if check and check[0] in '=+-@' else visible_label
    report = io.StringIO(newline='')
    csv.writer(report).writerows([('Status', 'Rows', 'Total'), (safe_label, count, formatted)])
    suffix = f'-{slug(selected, "status")}' if selected else ''
    preview = f'Status {visible_label}\nRows {count}\nTotal {formatted}'
    return Output(f'{slug(stem, "spreadsheet")}-report{suffix}.csv', report.getvalue().encode(), preview, 'text/csv')


def order(values) -> Output:
    identifier = required(values, 'Order', 'order id', 'order number', 'order #')
    customer = required(values, 'Customer')
    sku = required(values, 'SKU')
    digits = required(values, 'Quantity')
    if not re.fullmatch(r'[0-9]{1,16}', digits) or not 1 <= int(digits) <= MAX_QUANTITY:
        raise ValueError('Quantity must be a positive whole number within the supported range.')
    quantity = int(digits)
    draft = {'order': identifier, 'customer': customer, 'sku': sku, 'quantity': quantity, 'status': 'draft'}
    content = (json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()
    preview = f'Order {identifier}\nCustomer {customer}\nSKU {sku}\nQuantity {quantity}\nState Draft'
    return Output(f'order-{slug(identifier, "draft")}.json', content, preview, 'application/json')
