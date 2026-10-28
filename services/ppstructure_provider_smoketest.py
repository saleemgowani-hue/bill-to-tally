"""
Run this on the machine that actually has PP-StructureV3 installed
(the Python 3.11 environment), to check the app's assumptions about
PaddleOCR's API are correct for the installed version:

    .venv\\Scripts\\python.exe services\\ppstructure_provider_smoketest.py path\\to\\a\\bill.jpg

What it does, step by step, printing what happens at each one:
  1. Imports PPStructureV3 from paddleocr.
  2. Loads the pipeline (slow the first time - downloads model weights).
  3. Runs it on your image.
  4. Calls res.save_to_markdown(...) on each result, exactly like
     services/ocr_service.py's PPStructureProvider does.
  5. Prints the resulting markdown and runs it through
     services/ppstructure_normalize.py, showing what the app would have
     extracted (supplier, invoice number, items, totals).

If any step fails, the printed error is exactly what a real bill upload
would show in the app - send that back so the integration can be fixed for
this PaddleOCR version.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python ppstructure_provider_smoketest.py path\\to\\bill.jpg")
        return 1
    image_path = Path(sys.argv[1])
    if not image_path.exists():
        print(f"File not found: {image_path}")
        return 1

    print("[1/5] Importing paddleocr.PPStructureV3 ...")
    try:
        from paddleocr import PPStructureV3
    except ImportError as exc:
        print(f"FAILED: {exc}")
        print('Run: pip install "paddlepaddle==3.2.2" "paddleocr[doc-parser]" "paddlex[ocr]"')
        return 1
    print("      OK")

    def _print_failure(exc: Exception) -> None:
        import traceback

        print(f"FAILED: {exc}")
        print("\n----- FULL TRACEBACK (send this back if you need help) -----")
        traceback.print_exc()
        print("----- END -----")
        text = str(exc)
        if "ConvertPirAttribute2RuntimeAttribute" in text or "onednn_instruction" in text:
            print(
                "\nThis is a known paddlepaddle 3.3.0/3.3.1 bug (CPU oneDNN "
                "engine — see PaddlePaddle/Paddle#77340). Fix:\n"
                '  pip install "paddlepaddle==3.2.2"\n'
                "then run this script again."
            )
        else:
            print(
                '\nThis exact error usually means a missing extra or a bad '
                "paddlepaddle version. Try:\n"
                '  pip install "paddlepaddle==3.2.2" "paddleocr[doc-parser]" "paddlex[ocr]"'
            )

    print("[2/5] Loading the pipeline (slow on first run - downloads models) ...")
    try:
        pipeline = PPStructureV3(device="cpu")
    except Exception as exc:
        _print_failure(exc)
        return 1
    print("      OK")

    print(f"[3/5] Running it on {image_path} ...")
    try:
        import numpy as np
        from PIL import Image

        img = np.array(Image.open(image_path).convert("RGB"))
        results = list(pipeline.predict(img))
    except Exception as exc:
        _print_failure(exc)
        return 1
    print(f"      OK - got {len(results)} result object(s)")

    print("[4/5] Calling res.save_to_markdown(...) on each result ...")
    import shutil
    import tempfile

    workdir = tempfile.mkdtemp(prefix="ppstructure_smoketest_")
    try:
        for index, res in enumerate(results, start=1):
            try:
                res.save_to_markdown(save_path=workdir)
                print(f"      Result {index}: OK")
            except Exception as exc:
                print(f"      Result {index}: FAILED - {exc}")
                print(
                    "      This means the app's PPStructureProvider needs updating "
                    "for this PaddleOCR version - please share this error."
                )
        md_files = sorted(Path(workdir).glob("*.md"))
        markdown = "\n\n".join(f.read_text(encoding="utf-8", errors="ignore") for f in md_files)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not markdown.strip():
        print("      No markdown was produced - nothing to show in step 5.")
        return 1

    print("\n----- MARKDOWN OUTPUT (first 2000 chars) -----")
    print(markdown[:2000])
    print("----- END -----\n")

    print("[5/5] Running it through the app's normalizer ...")
    from services.ppstructure_normalize import markdown_to_invoice

    data, notes = markdown_to_invoice(markdown)
    print(f"      Supplier:        {data['supplier']['name']!r}")
    print(f"      Invoice number:  {data['invoice']['invoice_number']!r}")
    print(f"      Items found:     {len(data['items'])}")
    for item in data["items"]:
        print(f"        - {item['raw_name']!r}: qty={item['quantity']} rate={item['rate']} total={item['total']}")
    print(f"      Grand total:     {data['grand_total']}")
    print(f"      Confidence:      {data['overall_confidence']}")
    if notes:
        print("      Notes:")
        for note in notes:
            print(f"        - {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
