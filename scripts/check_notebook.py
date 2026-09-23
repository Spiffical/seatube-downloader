"""Execute the default offline notebook with this interpreter; do not overwrite it."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import nbformat
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpecManager
from nbclient import NotebookClient

root = Path(__file__).resolve().parents[1]
notebook = nbformat.read(root / "examples" / "research_walkthrough.ipynb", as_version=4)
nbformat.validate(notebook)
# A temporary kernel spec avoids using a system Jupyter kernel with a different
# Python environment. Nothing is installed into the user's global kernel list.
with TemporaryDirectory() as directory:
    import json
    spec = Path(directory) / "seatube-check"
    spec.mkdir()
    (spec / "kernel.json").write_text(json.dumps({
        "argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": "SeaTube check", "language": "python",
    }))
    manager = KernelManager(kernel_name="seatube-check",
                            kernel_spec_manager=KernelSpecManager(kernel_dirs=[directory]))
    try:
        NotebookClient(notebook, km=manager, timeout=60,
                       resources={"metadata": {"path": str(root)}}).execute()
    finally:
        if manager.has_kernel:
            manager.shutdown_kernel(now=True)
print("Notebook executed successfully (offline defaults).")
