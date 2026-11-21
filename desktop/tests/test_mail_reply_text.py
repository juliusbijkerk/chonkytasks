"""Exercise the actual native comparison without reading or writing any email."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture(scope='module')
def native_comparison(tmp_path_factory):
    if sys.platform != 'darwin':
        pytest.skip('AppleScript requires macOS')
    folder = tmp_path_factory.mktemp('mail-comparison')
    compiled = folder / 'Mail.scpt'
    source = Path(__file__).resolve().parents[1] / 'macos/AutomationHelper/Mail.applescript'
    subprocess.run(['osacompile', '-o', str(compiled), str(source)], check=True, capture_output=True)
    harness = folder / 'compare.applescript'
    harness.write_text('''on run argv
set helper to load script POSIX file (item 1 of argv)
return helper's replyBodyMatches(item 2 of argv, item 3 of argv)
end run
''')
    def compare(actual, expected):
        result = subprocess.run(['osascript', str(harness), str(compiled), actual, expected],
                                check=True, capture_output=True, text=True, timeout=10)
        return result.stdout.strip() == 'true'
    return compare


@pytest.mark.parametrize('actual,expected,matches', [
    ('\nHello,\n\nTuesday at 14:00.\n\nBest regards, \n', 'Hello,\n\nTuesday at 14:00.\n\nBest regards,', True),
    ('\tBonjour,\r\n\r\nMardi à 14:00.\r\nCordialement,\u00a0', 'Bonjour,\nMardi à 14:00.\nCordialement,', True),
    ('Tuesday at 15:00.', 'Tuesday at 14:00.', False),
    ('Hello,\nExtra text\nBest regards,', 'Hello,\nBest regards,', False),
    ('Best regards,\nHello,', 'Hello,\nBest regards,', False),
    ('HELLO', 'Hello', False),
    ('', 'Hello,', False),
])
def test_mail_serialization_keeps_substantive_text_exact(native_comparison, actual, expected, matches):
    assert native_comparison(actual, expected) is matches
