import csv
import io
import json

import pytest

from app.office_recipes import prepare


def test_mail_task_uses_the_new_message_and_preserves_its_explicit_due_date():
    mail = {
        'subject': 'Week-end stock check',
        'sender': 'Nia Meadows <nia@example.test>',
        'body': 'Hi!\r\nTask: Review [new bins]\r\nDue date: 2028-02-29\r\n\r\n'
                'On Monday someone wrote:\r\nTask: Old task\r\nDue: 2028-02-02\r\n',
    }
    result = prepare('mailToTask', b'', '', mail=mail)
    assert result.relative_path == 'task-review-new-bins.md'
    assert result.mime_type == 'text/markdown'
    assert b'- [ ] Review \\[new bins\\]' in result.content
    assert b'2028-02-29' in result.content
    assert 'Week-end stock check' in result.preview
    assert b'Old task' not in result.content

    mail['body'] = 'Thanks!\n\nOn Monday someone wrote:\nTask: Old task\nDue: 2028-02-02'
    with pytest.raises(ValueError, match='Task field'):
        prepare('mailToTask', b'', '', mail=mail)


def test_mail_note_keeps_body_plain_and_renders_layout_as_expected():
    body = 'Please keep these details.\n~~~\n<img src="https://example.test/tracker">\n\n'
    result = prepare('mailToNote', b'', '', mail={'subject': 'Stock [notes]', 'sender': 'Nia', 'body': body})
    assert result.relative_path == 'note-stock-notes.md'
    assert result.preview == 'Stock [notes]\nFrom Nia\n\n' + body
    assert result.content.decode() == '# Stock \\[notes\\]\n\n**From** Nia\n\n~~~~\n' + body + '~~~~\n'


def test_invoice_validates_fields_but_keeps_original_bytes_and_safe_destination():
    data = ('\ufeffInvoice: X/482\r\nVendor: ../Étage and Co.\r\nDate: 2028-02-29\r\n'
            'Total: 2,450.20 GBP\r\nExtra details remain intact.\r\n').encode()
    result = prepare('invoiceFiling', data, 'Incoming X482.TXT')
    assert result.relative_path == 'etage-and-co/2028-02/incoming-x482.txt'
    assert result.content == data
    assert result.mime_type == 'text/plain'
    assert 'Total 2,450.20 GBP' in result.preview

    with pytest.raises(ValueError, match='valid YYYY-MM-DD'):
        prepare('invoiceFiling', data.replace(b'2028', b'2027'), 'incoming.txt')
    with pytest.raises(ValueError, match='source file name'):
        prepare('invoiceFiling', data, '../incoming.txt')
    with pytest.raises(ValueError, match='UTF-8'):
        prepare('invoiceFiling', data + b'\xff', 'incoming.txt')


def test_report_uses_exact_matching_decimal_amounts_and_formula_safe_status():
    rows = ('Description,Status,Amount\r\n"Paper, recycled",APPROVED,0.10\r\n'
            'Pens,Approved,0.20\r\nFiles,Pre-approved,999\r\nLabels,Pending,100\r\n')
    result = prepare('spreadsheetReport', rows.encode(), 'Fresh Workbook.csv', status_filter=' Approved ')
    assert result.relative_path == 'fresh-workbook-report-approved.csv'
    assert result.content == b'Status,Rows,Total\r\nApproved,2,0.30\r\n'
    assert result.preview == 'Status Approved\nRows 2\nTotal 0.30'

    label = '\ufeff＝SUM(1,2)'
    data = f'Status,Amount\n"{label}","1,000.004"\n'
    report = prepare('spreadsheetReport', data.encode(), 'cash.csv', status_filter=label)
    assert list(csv.reader(io.StringIO(report.content.decode()))) == [
        ['Status', 'Rows', 'Total'], ["'" + label, '1', '1000.004'],
    ]
    assert prepare('spreadsheetReport', data.encode(), 'cash.csv', status_filter=' ').content == (
        b'Status,Rows,Total\r\nAll,1,1000.004\r\n'
    )
    with pytest.raises(ValueError, match='one Amount column'):
        prepare('spreadsheetReport', b'Status,Amount,Total\nApproved,1,2\n', 'cash.csv')


def test_order_draft_is_typed_and_does_not_infer_conflicting_fields():
    data = ('Order number: ORD/920\nCustomer: The "Blue" Office\nSKU: JG-003\nQuantity: 7\n'
            'Internal details: For staging only\n').encode()
    result = prepare('orderDraft', data, 'request.md')
    assert result.relative_path == 'order-ord-920.json'
    assert result.mime_type == 'application/json'
    assert json.loads(result.content) == {
        'customer': 'The "Blue" Office', 'order': 'ORD/920', 'quantity': 7, 'sku': 'JG-003', 'status': 'draft',
    }
    assert 'Quantity 7' in result.preview
    with pytest.raises(ValueError, match='conflicting Order fields'):
        prepare('orderDraft', data + b'Order: Different order\n', 'request.md')
    with pytest.raises(ValueError, match='positive whole number'):
        prepare('orderDraft', data.replace(b'Quantity: 7', b'Quantity: 9007199254740992'), 'request.md')
