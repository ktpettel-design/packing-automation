import os
import re
import zipfile
import tempfile
from io import BytesIO
from collections import defaultdict
from datetime import datetime

from flask import Flask, request, render_template, send_file, flash, redirect, url_for, session
import fitz
import pandas as pd
import barcode
from barcode.writer import ImageWriter

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "hemcha-packing-2026-change-me")
app.config['MAX_CONTENT_LENGTH'] = 80 * 1024 * 1024  # 80 MB

# Optional simple password protection (set this environment variable on Replit)
TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "")  # leave empty = no password

UPLOAD_FOLDER = tempfile.mkdtemp(prefix="packing_")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ========== CONFIG ==========
FONT_CANDIDATES = ["Helvetica-Bold", "Courier-Bold", "Times-Bold", "Helvetica", "Courier", "Times-Roman"]
STAMP_FONTSIZE = 18
LINE_SPACING = 1.25
LEFT_MARGIN = 40
RIGHT_MARGIN = 40
BOTTOM_MARGIN = 50
BARCODE_WIDTH = 520
BARCODE_HEIGHT = 120

SINGLE_SKU_GAP_ABOVE_BARCODE = -30.0
SINGLE_SKU_GAP_BARCODE_TO_TEXT = -22.0
MULTI_SKU_FIRST_BARCODE_OFFSET = 220.0
MULTI_SKU_GAP_BARCODE_TO_TEXT = -22.0
MULTI_SKU_GAP_BETWEEN_LINES = -45.0

COMPANY_FOOTER = "for hemcha food international llp"


# ==================== HELPERS (same logic) ====================
def strip_trailing_punct(s: str) -> str:
    return re.sub(r'[^A-Za-z0-9]+$', '', (s or '').strip())

def norm_sku(s: str) -> str:
    return strip_trailing_punct(s).lower()

def sku_boundary_pattern(nk: str) -> str:
    base = re.escape(nk)
    return rf"(?<![A-Za-z0-9]){base}(?![-A-Za-z0-9])"

def sku_fuzzy_in_text_pattern(raw: str) -> str:
    base = re.escape(strip_trailing_punct(raw))
    return rf"{base}(?:[._,\-]+)?"

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-").replace("\u2011", "-")
    s = s.replace("\uFF08", "(").replace("\uFF09", ")")
    s = s.replace("\u200B", "").replace("\u200C", "").replace("\u200D", "")
    return s

def load_sku_master(path):
    df = pd.read_excel(path)
    df.columns = [c.strip() for c in df.columns]
    required = ['sku', 'Product Name', 'Grams', 'Inner Pack Size', 'Box Code']
    for c in required:
        if c not in df.columns:
            raise ValueError(f"Missing column: {c}")
    df['Inner Pack Size'] = pd.to_numeric(df['Inner Pack Size'], errors='coerce').fillna(1).astype(int)
    lookup = {}
    for _, row in df.iterrows():
        raw = row.get('sku')
        if pd.isna(raw):
            continue
        sku = str(raw).strip()
        if not sku:
            continue
        box_codes = {}
        for q in range(2, 6):
            col = f'Box Code {q}'
            val = row.get(col, '')
            box_codes[q] = '' if pd.isna(val) else str(val).strip()
        lookup[sku] = {
            'name': '' if pd.isna(row['Product Name']) else str(row['Product Name']).strip(),
            'grams': '' if pd.isna(row['Grams']) else str(row['Grams']).strip(),
            'pack_size': int(row['Inner Pack Size']),
            'box_code': '' if pd.isna(row['Box Code']) else str(row['Box Code']).strip(),
            'box_codes': box_codes,
        }
    if not lookup:
        raise ValueError("No valid SKUs found")
    return lookup

def choose_box_code(item, qty):
    if qty == 1:
        return item['box_code'] or '-'
    if 2 <= qty <= 5:
        return item['box_codes'].get(qty, '-') or '-'
    return '-'

def _is_qty_header(text):
    t = (text or "").strip().lower()
    return t.startswith("qty") or "quantity" in t

def _flatten_lines(d):
    all_lines = []
    for b in d.get("blocks", []):
        for l in b.get("lines", []):
            spans = l.get("spans", [])
            if spans:
                row_text = " ".join(s.get("text", "") for s in spans)
                all_lines.append({"text": row_text, "spans": spans})
    return all_lines

def find_qty_by_column(page, sku_raw, x_pad=100, lookahead_lines=20):
    d = page.get_text("dict")
    qty_xmin = qty_xmax = None
    for b in d.get("blocks", []):
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                if _is_qty_header(s.get("text", "")):
                    x0, _, x1, _ = s["bbox"]
                    qty_xmin, qty_xmax = x0 - x_pad, x1 + x_pad
                    break
            if qty_xmin is not None:
                break
        if qty_xmin is not None:
            break
    if qty_xmin is None:
        return None
    lines = _flatten_lines(d)
    sku_pat = re.compile(sku_fuzzy_in_text_pattern(sku_raw), re.I)
    idx = next((i for i, L in enumerate(lines) if sku_pat.search(L["text"])), None)
    if idx is None:
        return None
    end = min(idx + 1 + lookahead_lines, len(lines))
    for L in lines[idx:end]:
        for s in L["spans"]:
            x0, _, x1, _ = s["bbox"]
            mid_x = (x0 + x1) / 2.0
            txt = (s.get("text", "")).strip()
            if qty_xmin <= mid_x <= qty_xmax and re.fullmatch(r"\d{1,3}", txt):
                return int(txt)
    return None

def find_qty_fallback_local(lines, sku_raw, window=25):
    sku_pat = re.compile(sku_fuzzy_in_text_pattern(sku_raw), re.I)
    start_idx = next((j for j, line in enumerate(lines) if sku_pat.search(line)), None)
    if start_idx is None:
        return None
    end = min(start_idx + window, len(lines))
    patterns = [
        r'(?:₹|rs\.?|inr)?\s*[\d,.]+\s+(?:-?\s*(?:₹|rs\.?|inr)?\s*[\d,.]+\s+)?(\d{1,3})\s+(?:₹|rs\.?|inr)?\s*[\d,.]+',
        r'^\s*(\d{1,3})\s+(?:₹|rs\.?|inr)?\s*[\d,.]+',
    ]
    for k in range(start_idx, end):
        candidates = [lines[k]]
        if k + 1 < end:
            candidates.append(lines[k] + " " + lines[k + 1])
        for t in candidates:
            t_norm = " ".join((t or "").split())
            for pat in patterns:
                m = re.search(pat, t_norm, flags=re.I)
                if m:
                    try:
                        return int(m.group(1))
                    except:
                        pass
    return None

def find_qty_fallback_fullpage(full_text):
    t = " ".join((full_text or "").split())
    m = re.search(r'(?:₹|rs\.?|inr)?\s*[\d,.]+\s+(?:-?\s*(?:₹|rs\.?|inr)?\s*[\d,.]+\s+)?(\d{1,3})\s+(?:₹|rs\.?|inr)?\s*[\d,.]+', t, flags=re.I)
    return int(m.group(1)) if m else None

def detect_qty(page, lines, full_text, sku_raw):
    q = find_qty_by_column(page, sku_raw)
    if q is None:
        q = find_qty_fallback_local(lines, sku_raw)
    if q is None:
        q = find_qty_fallback_fullpage(full_text)
    return q if q is not None else 1

def _longest_match_wins(found_keys):
    if len(found_keys) <= 1:
        return found_keys
    pairs = sorted([(k, norm_sku(k)) for k in found_keys], key=lambda kv: len(kv[1]), reverse=True)
    kept = []
    for k, nk in pairs:
        if any(nk2.startswith(nk) and len(nk2) > len(nk) and nk2[len(nk)] in "-._" for _, nk2 in kept):
            continue
        kept.append((k, nk))
    return {k for k, _ in kept}

def find_skus_on_page(full_text, sku_norm_index):
    text = normalize_text(full_text)
    found = set()
    bracket_tokens = re.findall(r"[\(\[\{]\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*[\)\]\}]", text)
    for tok in bracket_tokens:
        key = sku_norm_index.get(norm_sku(tok))
        if key:
            found.add(key)
    if found:
        return _longest_match_wins(found)
    lower_text = text.lower()
    for nk, canonical in sku_norm_index.items():
        if nk and re.search(sku_boundary_pattern(nk), lower_text, flags=re.I):
            found.add(canonical)
    return _longest_match_wins(found)

def choose_font():
    for fn in FONT_CANDIDATES:
        try:
            fitz.get_text_length("ABC", fontname=fn, fontsize=STAMP_FONTSIZE)
            return fn
        except:
            continue
    return "Helvetica"

def find_single_stamp_start_y(page):
    d = page.get_text("dict")
    ys = []
    for b in d.get("blocks", []):
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                txt = (s.get("text", "")).lower().strip()
                if "mode of payment" in txt or "authorized signatory" in txt:
                    ys.append(s["bbox"][3])
    return max(ys) + SINGLE_SKU_GAP_ABOVE_BARCODE if ys else None

def find_multi_clear_start_y(page):
    d = page.get_text("dict")
    y_start = None
    for b in d.get("blocks", []):
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                if COMPANY_FOOTER in (s.get("text", "")).lower():
                    y0 = s["bbox"][1]
                    if y_start is None or y0 < y_start:
                        y_start = y0
    return max(40, y_start - 5) if y_start is not None else None

def make_barcode_bytes(data: str) -> bytes:
    buf = BytesIO()
    code128 = barcode.get("code128", data, writer=ImageWriter())
    options = {"module_width": 0.9, "module_height": 18.0, "font_size": 0, "write_text": False, "quiet_zone": 2.0, "dpi": 400}
    code128.write(buf, options)
    return buf.getvalue()

def make_short_order_key(order_no: str) -> str:
    digits = re.sub(r"\D", "", order_no or "")
    return (digits[-5:] if len(digits) > 5 else digits) or (order_no or "XXXXX")[-5:]

def process_pdf(pdf_path, sku_lookup, sku_norm_index, sku_totals, box_totals, output_dir):
    modified = False
    pages_stamped = 0
    with fitz.open(pdf_path) as doc:
        for p_idx, page in enumerate(doc):
            raw_text = page.get_text()
            lines = raw_text.split("\n")
            full_text = normalize_text(raw_text)
            m = re.search(r"Order\s*Number\s*:\s*([0-9-]+)", full_text, flags=re.I)
            order_no = m.group(1).strip() if m else f"{os.path.basename(pdf_path)}-p{p_idx+1}"
            short_key = make_short_order_key(order_no)
            page_skus = find_skus_on_page(full_text, sku_norm_index)
            if not page_skus:
                continue
            overlay_lines = []
            page_line_info = []
            for key in sorted(page_skus):
                item = sku_lookup[key]
                qty = detect_qty(page, lines, full_text, key)
                total_packets = item['pack_size'] * qty
                box_code = choose_box_code(item, qty)
                sku_totals[key]["Product Name"] = item["name"]
                sku_totals[key]["Grams"] = item["grams"]
                sku_totals[key]["Total Packets"] += total_packets
                if box_code not in ("", "-"):
                    box_totals[box_code] += 1
                overlay_lines.append(f"{item['name']}    {item['grams']}    {total_packets} ({item['pack_size']} x {qty})    [Box: {box_code}]")
                page_line_info.append((key, total_packets))
            if not overlay_lines:
                continue
            used_font = choose_font()
            line_h = STAMP_FONTSIZE * LINE_SPACING
            page_width = page.rect.width
            page_height = page.rect.height
            num_lines = len(page_line_info)
            if num_lines == 1:
                y_start = find_single_stamp_start_y(page) or (page_height - BOTTOM_MARGIN - BARCODE_HEIGHT - 30)
                y_start = max(10, y_start)
                x_bar = LEFT_MARGIN
                actual_width = min(BARCODE_WIDTH, page_width - x_bar - RIGHT_MARGIN)
                if actual_width <= 0:
                    continue
                amazon_sku, total_packets = page_line_info[0]
                safe_sku = amazon_sku.replace(",", " ")
                barcode_string = f"{short_key},{safe_sku},{int(total_packets)},{num_lines}"
                try:
                    page.insert_image(fitz.Rect(x_bar, y_start, x_bar + actual_width, y_start + BARCODE_HEIGHT), stream=make_barcode_bytes(barcode_string))
                    y_text = y_start + BARCODE_HEIGHT + SINGLE_SKU_GAP_BARCODE_TO_TEXT
                    page.insert_text((x_bar, y_text), overlay_lines[0], fontname=used_font, fontsize=STAMP_FONTSIZE)
                    modified = True
                    pages_stamped += 1
                except Exception as e:
                    print("Single barcode error:", e)
            else:
                y_clear_base = find_multi_clear_start_y(page) or page_height * 0.55
                y_cursor = max(y_clear_base, y_clear_base + MULTI_SKU_FIRST_BARCODE_OFFSET)
                page.draw_rect(fitz.Rect(0, y_cursor, page_width, page_height), color=(1,1,1), fill=(1,1,1))
                page_stamped = False
                for amazon_sku, total_packets in page_line_info:
                    if y_cursor + BARCODE_HEIGHT > page_height - 15:
                        break
                    actual_width = min(BARCODE_WIDTH, page_width - LEFT_MARGIN - RIGHT_MARGIN)
                    safe_sku = amazon_sku.replace(",", " ")
                    barcode_string = f"{short_key},{safe_sku},{int(total_packets)},{num_lines}"
                    try:
                        page.insert_image(fitz.Rect(LEFT_MARGIN, y_cursor, LEFT_MARGIN + actual_width, y_cursor + BARCODE_HEIGHT), stream=make_barcode_bytes(barcode_string))
                        y_text = y_cursor + BARCODE_HEIGHT + MULTI_SKU_GAP_BARCODE_TO_TEXT
                        page.insert_text((LEFT_MARGIN, y_text), overlay_lines[page_line_info.index((amazon_sku, total_packets))], fontname=used_font, fontsize=STAMP_FONTSIZE)
                        y_cursor = y_text + line_h + MULTI_SKU_GAP_BETWEEN_LINES
                        modified = True
                        page_stamped = True
                    except Exception as e:
                        print("Multi barcode error:", e)
                if page_stamped:
                    pages_stamped += 1
        if modified:
            out_path = os.path.join(output_dir, f"MOD_{os.path.basename(pdf_path)}")
            doc.save(out_path, deflate=True, garbage=3)
            return True, pages_stamped, out_path
    return False, pages_stamped, None

def export_summary(sku_totals, box_totals, out_dir):
    prod_rows = [{"Product Name": v["Product Name"], "Grams": v["Grams"], "Total Packets": int(v["Total Packets"])} for v in sku_totals.values()]
    prod_df = pd.DataFrame(prod_rows)
    if not prod_df.empty:
        prod_df = prod_df.sort_values(by=["Product Name", "Grams"], na_position="last")
    box_rows = [{"Box Code": k, "Total Boxes": int(v)} for k, v in box_totals.items()]
    box_df = pd.DataFrame(box_rows)
    if not box_df.empty:
        box_df = box_df.sort_values(by=["Box Code"])
    max_len = max(len(prod_df), len(box_df), 1)
    prod_df = prod_df.reindex(range(max_len)).fillna("")
    box_df = box_df.reindex(range(max_len)).fillna("")
    combined = pd.DataFrame({
        "Product Name": prod_df.get("Product Name", pd.Series([""]*max_len)),
        "Grams": prod_df.get("Grams", pd.Series([""]*max_len)),
        "Total Packets": prod_df.get("Total Packets", pd.Series([""]*max_len)),
        "Box Code": box_df.get("Box Code", pd.Series([""]*max_len)),
        "Total Boxes": box_df.get("Total Boxes", pd.Series([""]*max_len)),
    })
    path = os.path.join(out_dir, "Packing_Summary.xlsx")
    combined.to_excel(path, index=False)
    return path


# ==================== ROUTES ====================
@app.route("/login", methods=["GET", "POST"])
def login():
    if not TEAM_PASSWORD:
        return redirect(url_for("index"))
    if request.method == "POST":
        if request.form.get("password") == TEAM_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        flash("Wrong password")
    return render_template("login.html")

@app.before_request
def require_login():
    if TEAM_PASSWORD and request.endpoint not in ("login", "static") and not session.get("logged_in"):
        return redirect(url_for("login"))

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "sku_master" not in request.files or not request.files["sku_master"].filename:
            flash("Please upload SKU Master Sheet")
            return redirect(url_for("index"))
        pdf_files = request.files.getlist("pdfs")
        if not any(f.filename for f in pdf_files):
            flash("Please upload at least one PDF")
            return redirect(url_for("index"))

        job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_dir = os.path.join(UPLOAD_FOLDER, job_id)
        os.makedirs(job_dir, exist_ok=True)
        output_dir = os.path.join(job_dir, "output")
        os.makedirs(output_dir, exist_ok=True)

        sku_path = os.path.join(job_dir, "sku.xlsx")
        request.files["sku_master"].save(sku_path)
        try:
            sku_lookup = load_sku_master(sku_path)
        except Exception as e:
            flash(f"SKU Master error: {e}")
            return redirect(url_for("index"))

        sku_norm_index = {norm_sku(k): k for k in sku_lookup}
        sku_totals = defaultdict(lambda: {"Product Name": "", "Grams": "", "Total Packets": 0})
        box_totals = defaultdict(int)

        for pdf_file in pdf_files:
            if not pdf_file.filename.lower().endswith(".pdf"):
                continue
            pdf_path = os.path.join(job_dir, pdf_file.filename)
            pdf_file.save(pdf_path)
            try:
                process_pdf(pdf_path, sku_lookup, sku_norm_index, sku_totals, box_totals, output_dir)
            except Exception as e:
                print(f"Error on {pdf_file.filename}: {e}")

        export_summary(sku_totals, box_totals, output_dir)

        zip_path = os.path.join(job_dir, f"Packing_{job_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(output_dir):
                for file in files:
                    zf.write(os.path.join(root, file), file)

        return send_file(zip_path, as_attachment=True, download_name=f"Packing_Results_{job_id}.zip")

    return render_template("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
