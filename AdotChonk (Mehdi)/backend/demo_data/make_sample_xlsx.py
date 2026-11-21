"""Generate a sample applications.xlsx. Run: python -m backend.demo_data.make_sample_xlsx [path]"""
import sys
from pathlib import Path

if __name__ == "__main__":
    # Allow running both as a module and as a script
    try:
        from backend.excel_client import generate_sample_tracker
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from backend.excel_client import generate_sample_tracker

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "applications.xlsx"
    generate_sample_tracker(path)
    print(f"Wrote sample tracker to {path}")
