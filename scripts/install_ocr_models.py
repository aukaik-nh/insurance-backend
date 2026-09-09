"""Install pinned public Tesseract models. Run during setup, never per upload."""
import hashlib
import os
from pathlib import Path
import urllib.request

REVISION = "e12c65a915945e4c28e237a9b52bc4a8f39a0cec"
HASHES = {
    "eng": "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba",
    "tha": "ee8adab6dc69eb8df3d3c8307ae8295471b7bd7d86a06d9267aa8f479b064eac",
}


def main():
    target = Path(os.getenv("LOCAL_OCR_MODELS", str(Path(__file__).resolve().parents[1] / "models/tessdata")))
    target.mkdir(parents=True, exist_ok=True)
    for lang, expected in HASHES.items():
        path = target / f"{lang}.traineddata"
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
            print(f"{lang}: verified")
            continue
        url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{REVISION}/{lang}.traineddata"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(40 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"{lang}: model checksum mismatch")
        temporary = path.with_suffix(".partial")
        temporary.write_bytes(data)
        temporary.replace(path)
        print(f"{lang}: installed and verified")


if __name__ == "__main__":
    main()

