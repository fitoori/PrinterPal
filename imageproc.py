from __future__ import annotations

import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from PIL import Image, ImageFilter, ImageOps

from .util import PrinterPalError, run_cmd


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _file_sig(path: str) -> str:
    st = os.stat(path)
    token = f"{os.path.abspath(path)}:{st.st_size}:{int(st.st_mtime)}".encode("utf-8")
    return _sha256_bytes(token)


def pdf_page_count(path: str) -> int:
    if not os.path.exists(path):
        raise PrinterPalError("PDF not found")
    res = run_cmd(["pdfinfo", path], timeout_s=8.0, check=True)
    for ln in (res.stdout or "").splitlines():
        if ln.lower().startswith("pages:"):
            try:
                return int(ln.split(":", 1)[1].strip())
            except ValueError:
                break
    raise PrinterPalError("Unable to determine PDF page count (pdfinfo output unexpected)")


def _render_pdf_page_to_png(path: str, *, page: int, dpi: int) -> Image.Image:
    if page < 1:
        raise PrinterPalError("page must be >= 1")
    with tempfile.TemporaryDirectory(prefix="printerpal_pdf_") as td:
        outprefix = os.path.join(td, "page")
        # pdftoppm uses 1-based page numbers
        run_cmd(
            [
                "pdftoppm",
                "-png",
                "-f",
                str(page),
                "-l",
                str(page),
                "-r",
                str(dpi),
                "-singlefile",
                path,
                outprefix,
            ],
            timeout_s=25.0,
            check=True,
        )
        png_path = f"{outprefix}.png"
        if not os.path.exists(png_path):
            raise PrinterPalError("pdftoppm did not produce expected PNG output")
        with Image.open(png_path) as im:
            return im.convert("RGB")


def _open_image(path: str) -> Image.Image:
    try:
        with Image.open(path) as im:
            return im.convert("RGB")
    except Exception as e:
        raise PrinterPalError(f"Unable to open image: {e}") from e


def apply_mode(im: Image.Image, mode: str, *, threshold: int) -> Image.Image:
    mode = (mode or "").lower()

    if mode == "raw":
        return im

    if mode == "grayscale":
        return im.convert("L").convert("RGB")

    if mode == "bw":
        g = im.convert("L")
        bw = g.point(lambda p: 255 if p >= threshold else 0, mode="L")
        return bw.convert("RGB")

    if mode == "dither":
        g = im.convert("L")
        bw1 = g.convert("1")
        return bw1.convert("RGB")

    if mode == "outline":
        g = im.convert("L")
        edges = g.filter(ImageFilter.FIND_EDGES)
        edges = ImageOps.autocontrast(edges)
        inv = ImageOps.invert(edges)
        bw = inv.point(lambda p: 255 if p >= threshold else 0, mode="L")
        return bw.convert("RGB")

    raise PrinterPalError("Unsupported mode")


def render_preview_png(
    path: str,
    *,
    mode: str,
    page: int,
    width: int,
    preview_dpi: int,
    threshold: int,
) -> bytes:
    if width < 64 or width > 2000:
        raise PrinterPalError("width must be between 64 and 2000")

    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        im = _render_pdf_page_to_png(path, page=page, dpi=preview_dpi)
    elif ext in SUPPORTED_IMAGE_EXTS:
        im = _open_image(path)
    else:
        raise PrinterPalError("Preview supports PDF and common image formats")

    im2 = apply_mode(im, mode, threshold=threshold)

    # Resize to requested width, preserve aspect ratio.
    w0, h0 = im2.size
    if w0 <= 0 or h0 <= 0:
        raise PrinterPalError("Invalid image dimensions")
    scale = min(1.0, float(width) / float(w0))
    new_w = max(1, int(w0 * scale))
    new_h = max(1, int(h0 * scale))
    im2 = im2.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    im2.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _images_to_pdf_bytes(images: list[Image.Image]) -> bytes:
    # Prefer img2pdf (more predictable PDF output) and fall back to Pillow.
    try:
        import img2pdf  # type: ignore

        img_blobs: list[bytes] = []
        for im in images:
            bio = io.BytesIO()
            im.convert("RGB").save(bio, format="PNG", optimize=True)
            img_blobs.append(bio.getvalue())

        return img2pdf.convert(img_blobs)
    except Exception:
        # Pillow fallback
        buf = io.BytesIO()
        rgb_images = [im.convert("RGB") for im in images]
        first, rest = rgb_images[0], rgb_images[1:]
        first.save(buf, format="PDF", save_all=True, append_images=rest)
        return buf.getvalue()


def prepare_print_file(
    src_path: str,
    *,
    mode: str,
    print_dpi: int,
    max_pdf_pages: int,
    threshold: int,
) -> Tuple[str, Dict[str, Any]]:
    """Prepare a PDF for printing and return (path, metadata).

    The returned path is a temporary file that the caller must delete.
    """
    if not os.path.exists(src_path):
        raise PrinterPalError("Source file not found")

    ext = os.path.splitext(src_path)[1].lower()
    meta: Dict[str, Any] = {"source": src_path, "mode": mode, "print_dpi": print_dpi}

    if mode == "raw":
        # Print original, no conversion.
        return src_path, {**meta, "prepared": False}

    if ext == ".pdf":
        pages = pdf_page_count(src_path)
        meta["pages"] = pages
        if pages > max_pdf_pages:
            raise PrinterPalError(
                f"PDF has {pages} pages, which exceeds processing limit ({max_pdf_pages}). "
                "Either increase printing.max_pdf_pages_process or use 'Raw' mode."
            )

        imgs: list[Image.Image] = []
        for p in range(1, pages + 1):
            im = _render_pdf_page_to_png(src_path, page=p, dpi=print_dpi)
            im2 = apply_mode(im, mode, threshold=threshold)
            imgs.append(im2)

        pdf_bytes = _images_to_pdf_bytes(imgs)

        fd, outpath = tempfile.mkstemp(prefix="printerpal_print_", suffix=".pdf")
        os.close(fd)
        with open(outpath, "wb") as f:
            f.write(pdf_bytes)
        return outpath, {**meta, "prepared": True, "output": outpath}

    if ext in SUPPORTED_IMAGE_EXTS:
        im = _open_image(src_path)
        im2 = apply_mode(im, mode, threshold=threshold)

        # Convert to single-page PDF for consistent printing across drivers.
        buf = io.BytesIO()
        im2.convert("RGB").save(buf, format="PDF")

        fd, outpath = tempfile.mkstemp(prefix="printerpal_print_", suffix=".pdf")
        os.close(fd)
        with open(outpath, "wb") as f:
            f.write(buf.getvalue())
        return outpath, {**meta, "prepared": True, "output": outpath}

    raise PrinterPalError("Unsupported file type for printing")
