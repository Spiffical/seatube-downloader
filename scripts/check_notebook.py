"""Validate notebook structure, or explicitly execute it against live ONC data.

CI runs validation without credentials. For a real local run:
    python scripts/check_notebook.py --execute --download-media
Executed output is written under ignored downloads/, never over the source notebook.
"""
import argparse
import ast
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import nbformat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Query live ONC data using ONC_TOKEN")
    parser.add_argument("--download-media", action="store_true", help="Also extract from one real archive")
    parser.add_argument("--env-file", type=Path, help="Read ONC_TOKEN from this private .env file")
    args = parser.parse_args()
    if (args.download_media or args.env_file) and not args.execute:
        parser.error("--download-media and --env-file require --execute")
    root = Path(__file__).resolve().parents[1]
    notebook = nbformat.read(root / "examples" / "research_walkthrough.ipynb", as_version=4)
    nbformat.validate(notebook)
    for cell in notebook.cells:
        if cell.cell_type == "code":
            ast.parse(cell.source)
    if not args.execute:
        print("Notebook structure and code syntax validated; live cells were not executed.")
        return

    from dotenv import dotenv_values
    from jupyter_client import KernelManager
    from jupyter_client.kernelspec import KernelSpecManager
    from nbclient import NotebookClient

    env_file = args.env_file or root / ".env"
    token = (dotenv_values(env_file).get("ONC_TOKEN") if args.env_file else
             os.environ.get("ONC_TOKEN") or dotenv_values(env_file).get("ONC_TOKEN"))
    if not token:
        parser.error("ONC_TOKEN is missing. Follow the token setup instructions in the notebook.")
    if args.download_media:
        parameters = [c for c in notebook.cells if "parameters" in c.metadata.get("tags", [])]
        if len(parameters) != 1:
            raise RuntimeError("Expected exactly one notebook configuration cell")
        parameters[0].source += "\nDOWNLOAD_MEDIA = True  # Enabled for this verification run."

    # Use this interpreter without installing a global Jupyter kernel.
    with TemporaryDirectory() as directory:
        spec = Path(directory) / "seatube-check"
        spec.mkdir()
        (spec / "kernel.json").write_text(json.dumps({
            "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
            "display_name": "SeaTube check", "language": "python",
        }), encoding="utf-8")
        manager = KernelManager(kernel_name="seatube-check",
                                kernel_spec_manager=KernelSpecManager(kernel_dirs=[directory]))

        def progress(cell, cell_index, **kwargs):
            if cell.cell_type == "code":
                print(f"Executing notebook cell {cell_index + 1}/{len(notebook.cells)}", flush=True)

        try:
            NotebookClient(notebook, km=manager, timeout=900, on_cell_start=progress,
                           resources={"metadata": {"path": str(root)}}).execute(
                               env={**os.environ, "ONC_TOKEN": token})
        except Exception as exc:
            # Never echo a credential if an upstream error includes a request URL.
            raise SystemExit(str(exc).replace(token, "[redacted]")) from None
        finally:
            if manager.has_kernel:
                manager.shutdown_kernel(now=True)

    serialized = nbformat.writes(notebook)
    if token in serialized:
        raise RuntimeError("Refusing to save notebook output containing a credential")
    output = root / "downloads" / "research_walkthrough" / "verified.ipynb"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized, encoding="utf-8")
    print("Notebook executed successfully against live ONC data.")
    print("Executed notebook saved to downloads/research_walkthrough/verified.ipynb")


if __name__ == "__main__":
    main()
