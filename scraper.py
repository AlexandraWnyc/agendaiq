"""
scraper.py — Miami-Dade agenda scraping for OCA Agenda Intelligence v6
Preserves all v5 scraper logic. Minor refactor: standalone module.
"""
import re, logging, time
from pathlib import Path
from datetime import datetime
from paths import PDF_CACHE_DIR
from urllib.parse import urljoin, quote_plus
import org_config

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF

log = logging.getLogger("oca-agent")


class MiamiDadeScraper:
    def __init__(self, org_id=None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "OCA-Agenda-Agent/6.0"})
        self._form_cache = {}
        self._org_id = org_id
        cfg = org_config.get_org_config(org_id or 1)
        self.base_url = cfg.get("legistar_base_url", "https://www.miamidade.gov/govaction/")
        self.committees = cfg.get("committees", {})

    def get_committees(self):
        log.info("Fetching committee list...")
        try:
            resp = self.session.get(
                self.base_url + "agendas.asp?Action=Agendas&Oper=DisplayList", timeout=30
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            committees = {}
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if "Oper=DisplayAgenda" in href and "Agenda=" in href:
                    name = link.get_text(strip=True)
                    m = re.search(r"Agenda=([A-Z]+)", href)
                    if m and name:
                        committees[name] = m.group(1)
            if committees:
                log.info(f"Found {len(committees)} active committees")
                return committees
        except Exception as e:
            log.warning(f"Could not fetch live list: {e}")
        return self.committees

    def get_agenda_dates(self, committee_name, committee_code):
        url = (f"{self.base_url}agendas.asp?Action=Agendas&Oper=DisplayAgenda"
               f"&Agenda={committee_code}&AgendaName={quote_plus(committee_name)}")
        log.info(f"  Checking dates for {committee_name}...")
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            dates, date_to_ids = [], {}
            seen_dates = set()
            for select in soup.find_all("select"):
                for opt in select.find_all("option"):
                    oid = opt.get("value", "").strip()
                    otxt = opt.get_text(strip=True)
                    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", otxt)
                    if m:
                        d = m.group(1)
                        # Store ALL agenda IDs for each date (handles
                        # supplemental/OTHER agendas on the same date)
                        date_to_ids.setdefault(d, []).append({
                            "id": oid, "label": otxt
                        })
                        # Only add the date string once to the dates list
                        if d not in seen_dates:
                            dates.append(d)
                            seen_dates.add(d)
                if dates:
                    break
            self._form_cache[committee_code] = {"date_to_ids": date_to_ids}
            log.info(f"    Found {len(dates)} dates, "
                     f"{sum(len(v) for v in date_to_ids.values())} total agendas")
            return dates
        except Exception as e:
            log.error(f"  Error: {e}")
            return []

    def get_matching_dates(self, committee_name, committee_code,
                           from_date=None, exact_date=None):
        available = self.get_agenda_dates(committee_name, committee_code)
        matched = []
        for d in available:
            try:
                dp = datetime.strptime(d.strip(), "%m/%d/%Y")
            except ValueError:
                continue
            if exact_date and dp.date() == exact_date.date():
                return [d.strip()]
            if from_date and dp.date() >= from_date.date():
                matched.append(d.strip())
        return matched

    def get_agenda_items(self, committee_code, committee_name, date):
        form_info = self._form_cache.get(committee_code, {})
        # Support both old format (date_to_id) and new format (date_to_ids)
        date_to_ids = form_info.get("date_to_ids", {})
        if not date_to_ids:
            # Legacy fallback
            old = form_info.get("date_to_id", {})
            if old.get(date):
                date_to_ids = {date: [{"id": old[date], "label": date}]}

        agenda_entries = date_to_ids.get(date, [])
        if not agenda_entries:
            return []

        all_items = []
        seen_matter_ids = set()
        for entry in agenda_entries:
            meeting_id = entry["id"]
            label = entry.get("label", date)
            agenda_url = (f"{self.base_url}legistarfiles/SourceCode/searchforpdf.asp"
                          f"?documentKey={meeting_id}&documenttype=agenda")
            log.info(f"  Fetching agenda: {committee_name} ({label})")
            try:
                resp = self.session.get(agenda_url, timeout=60, allow_redirects=True)
                resp.raise_for_status()
                ct = resp.headers.get("Content-Type", "").lower()
                if "pdf" in ct or resp.url.lower().endswith(".pdf"):
                    pp = PDF_CACHE_DIR / f"agenda_{committee_code}_{meeting_id}.pdf"
                    pp.write_bytes(resp.content)
                    items = [{
                        "committee_item_number": "1",
                        "matter_id": meeting_id,
                        "item_number": "Full Agenda",
                        "title": committee_name,
                        "pdf_url": resp.url,
                        "pdf_path": str(pp),
                        "page_text": "",
                    }]
                else:
                    items = self._parse_committee_agenda(resp.text, resp.url)

                # Merge items, skip duplicates (same matter_id already seen)
                for item in items:
                    mid = str(item.get("matter_id", ""))
                    if mid and mid in seen_matter_ids:
                        continue
                    if mid:
                        seen_matter_ids.add(mid)
                    all_items.append(item)

                if len(agenda_entries) > 1:
                    log.info(f"    → {len(items)} items from {label} "
                             f"({len(all_items)} total so far)")
            except Exception as e:
                log.error(f"  Failed fetching {label}: {e}")

        return all_items

    def _parse_committee_agenda(self, html, base_url):
        """Parse committee agenda HTML — captures sub-items like 3A1, 1G1.

        Miami-Dade agendas use a two-tier numbering scheme:
          - Section headers like '1G', '3A' on standalone rows.
          - Sub-items like '1G1', '3A1' on the actual matter rows.
        Previously we matched both with the same regex and whichever appeared
        last won, which left matter rows labeled with their section header.
        Now we scan every cell of the matter row itself for a sub-item code
        (letter followed by digit) and only fall back to the section header
        when no sub-code is present on the row.
        """
        soup = BeautifulSoup(html, "html.parser")
        items = []
        current_section_header = ""  # e.g. '1G', updated from header rows only

        # Sub-item code: digits + letter + digits (+ optional SUPPLEMENT/SUBSTITUTE)
        SUB_ITEM_RE = re.compile(
            r'^(\d+[A-Z]\d+(?:\s+(?:SUPPLEMENT|SUBSTITUTE|SUB))?)\s*$',
            re.I,
        )
        # Section header: digits + letter ONLY (no trailing digit)
        SECTION_HEADER_RE = re.compile(
            r'^(\d+[A-Z](?:\s+(?:SUPPLEMENT|SUBSTITUTE|SUB))?)\s*$',
            re.I,
        )

        def _find_item_number_in_row(row_tds):
            """Return the best item number found on this row, or ''.
            Prefers sub-item codes (1G1) over section headers (1G)."""
            best = ""
            for td in row_tds:
                t = td.get_text(strip=True)
                if not t or len(t) > 30:
                    continue
                m = SUB_ITEM_RE.match(t)
                if m:
                    return m.group(1).strip().upper()  # sub-item beats everything
                if not best:
                    m = SECTION_HEADER_RE.match(t)
                    if m:
                        best = m.group(1).strip().upper()
            return best

        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            first_text = tds[0].get_text(strip=True)

            # Update the running section header only from *bare* section-header rows
            m = SECTION_HEADER_RE.match(first_text)
            if m and not SUB_ITEM_RE.match(first_text):
                current_section_header = m.group(1).strip().upper()

            for link in tr.find_all("a", href=True):
                href = link["href"]
                m = re.search(
                    r"documentKey=(\d+).*documenttype=matter|documenttype=matter.*documentKey=(\d+)",
                    href, re.I
                )
                if m:
                    mid = m.group(1) or m.group(2)
                    file_type, sponsor, title_text = "", "", ""
                    for td in tds:
                        t = td.get_text(strip=True)
                        if t in ("Resolution", "Ordinance", "Report",
                                 "Veto Message", "Supplement"):
                            file_type = t
                        if "Sponsor" in t or "Prime Sponsor" in t:
                            sponsor = t

                    next_rows = tr.find_next_siblings("tr", limit=4)
                    for nr in next_rows:
                        nr_text = nr.get_text(strip=True)
                        if len(nr_text) > 30 and not re.match(r'^\d+[A-Z]', nr_text):
                            if not re.match(r'^\d{1,2}/\d{1,2}/\d{4}', nr_text):
                                title_text = nr_text[:300]
                                break

                    pdf_url = None
                    for nr in [tr] + list(tr.find_next_siblings("tr", limit=4)):
                        for pl in nr.find_all("a", href=True):
                            if ".pdf" in pl["href"].lower() and "Matters" in pl["href"]:
                                pdf_url = urljoin(base_url, pl["href"])
                                break
                        if pdf_url:
                            break

                    # Prefer a sub-item code found on THIS row (e.g. '1G1') over
                    # the running section header (e.g. '1G'). If the matter row
                    # has no sub-code, also check the next sibling row — some
                    # Legistar templates put the item number on the row above
                    # or below the matter link row.
                    row_item = _find_item_number_in_row(tds)
                    if not row_item:
                        for sib in tr.find_next_siblings("tr", limit=2):
                            row_item = _find_item_number_in_row(sib.find_all("td"))
                            if row_item:
                                break
                    item_number_final = row_item or current_section_header

                    items.append({
                        "committee_item_number": item_number_final,
                        "matter_id": mid,
                        "item_number": mid,
                        "title": title_text,
                        "detail_url": urljoin(base_url, href),
                        "pdf_url": pdf_url,
                        "page_text": "",
                        "file_type_from_agenda": file_type,
                        "sponsor_from_agenda": sponsor,
                    })

        log.debug(f"  Parsed {len(items)} items from committee agenda")
        return items

    def _resolve_matter_url(self, mid):
        """Two-step Legistar navigation: hit searchforpdf.asp bridge page first,
        then extract the real matter.asp URL from the JavaScript link on that page.
        Falls back to direct matter.asp URLs if the bridge page doesn't work."""
        bridge_url = (f"{self.base_url}legistarfiles/SourceCode/searchforpdf.asp"
                      f"?documenttype=matter&documentKey={mid}")
        try:
            log.debug(f"    Hitting bridge page: {bridge_url}")
            resp = self.session.get(bridge_url, timeout=30)
            resp.raise_for_status()
            # Look for javascript:ReportWindow('http://...matter.asp?...')
            m = re.search(
                r"""ReportWindow\s*\(\s*['"]([^'"]*matter\.asp[^'"]*)['"]\s*\)""",
                resp.text, re.I
            )
            if m:
                matter_url = m.group(1).rstrip("'\"\\")
                # Force file=true — with file=false the page returns a
                # stripped-down version without legislative history.
                matter_url = re.sub(r'file=false', 'file=true', matter_url, flags=re.I)
                log.info(f"    Bridge resolved: {matter_url[:120]}")
                return [matter_url]
            # Also check for a direct link/redirect to matter.asp
            for link in BeautifulSoup(resp.text, "html.parser").find_all("a", href=True):
                if "matter.asp" in link["href"].lower():
                    resolved = urljoin(bridge_url, link["href"])
                    resolved = re.sub(r'file=false', 'file=true', resolved, flags=re.I)
                    log.info(f"    Bridge link found: {resolved[:120]}")
                    return [resolved]
            log.debug(f"    Bridge page had no matter.asp link ({len(resp.text)} chars)")
        except Exception as e:
            log.warning(f"    Bridge page failed: {e}")

        # Fallback: direct matter.asp URLs
        yr = datetime.now().year
        return [
            f"http://www.miamidade.gov/govaction/matter.asp?matter={mid}&file=true&fileAnalysis=false&yearFolder=Y{yr}",
            f"http://www.miamidade.gov/govaction/matter.asp?matter={mid}&file=true&fileAnalysis=false&yearFolder=Y{yr-1}",
            f"http://www.miamidade.gov/govaction/matter.asp?matter={mid}&file=true",
        ]

    def get_item_detail(self, item):
        """Fetch matter.asp for routing info (v5 dual-approach parsing preserved).
        Uses two-step navigation: bridge page (searchforpdf.asp) → matter.asp."""
        mid = item.get("matter_id")
        if not mid:
            return item
        urls = self._resolve_matter_url(mid)
        for url in urls:
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                if len(resp.text) < 1000:
                    log.info(f"    matter.asp too short ({len(resp.text)} chars), trying next URL. Content: {resp.text[:200]}")
                    continue
                log.info(f"    matter.asp OK: {len(resp.text)} chars from {url}")
                # Store the final matter.asp URL (not the bridge) as the detail link
                item["detail_url"] = url
                soup = BeautifulSoup(resp.text, "html.parser")
                page_text = soup.get_text(separator="\n", strip=True)
                log.info(f"    page_text: {len(page_text)} chars, first 200: {page_text[:200]}")

                # Legistar matter pages often use frames/iframes.  The outer
                # page has header fields but the legislative history lives in
                # a child frame. Detect and fetch all frame/iframe src URLs,
                # merging their text into page_text.
                frame_soups = []
                frames = soup.find_all(["frame", "iframe"])
                if frames:
                    log.info(f"    Found {len(frames)} frame(s) — fetching contents")
                for fr in frames:
                    fr_src = fr.get("src", "")
                    if not fr_src:
                        continue
                    fr_url = urljoin(url, fr_src)
                    try:
                        fr_resp = self.session.get(fr_url, timeout=30)
                        fr_resp.raise_for_status()
                        fr_soup = BeautifulSoup(fr_resp.text, "html.parser")
                        fr_text = fr_soup.get_text(separator="\n", strip=True)
                        log.info(f"    frame {fr_url[:80]}: {len(fr_text)} chars")
                        if fr_text:
                            page_text = page_text + "\n" + fr_text
                            frame_soups.append(fr_soup)
                    except Exception as fe:
                        log.warning(f"    frame fetch failed: {fe}")

                # Search all soups (main page + any frames) for fields
                all_soups = [soup] + frame_soups

                # Known Legistar matter-page field labels. We match these
                # regardless of whether they're wrapped in <b>, <strong>,
                # <font>, <span>, or no tag at all — which is why the earlier
                # bold-only approach missed 'Title:' and 'Notes:' (Legistar
                # often uses <font> for those rows instead of <b>).
                KNOWN_LABELS = [
                    "File Name", "File Number", "Title", "File Type",
                    "Status", "Control", "Sponsors", "Sponsor",
                    "Requester", "Introduced", "Enactment Date",
                    "Enactment Number", "Agenda Date", "Agenda Item Number",
                    "Notes", "On Agenda", "Final Action",
                    "Department", "Version",
                ]
                # Sort longest first so "Agenda Item Number" beats "Agenda"
                KNOWN_LABELS.sort(key=len, reverse=True)

                def _label_match(td_text: str) -> str | None:
                    """If this TD looks like 'Label:' (with optional trailing
                    value), return the canonical label. Else None."""
                    t = td_text.strip().rstrip(":").strip()
                    for lbl in KNOWN_LABELS:
                        if t.lower() == lbl.lower():
                            return lbl
                        # Also catch 'Title: <value>' where value is in same TD
                        if t.lower().startswith(lbl.lower() + ":"):
                            return lbl
                    return None

                fields = {}

                for s in all_soups:
                    for td in s.find_all("td"):
                        td_text = td.get_text(" ", strip=True)
                        if not td_text or len(td_text) > 500:
                            # Skip empty cells and giant body cells (which are
                            # the *value* side, not the label side)
                            continue

                        # Case 1: label and value in same TD ('Title: ...')
                        for lbl in KNOWN_LABELS:
                            prefix = lbl + ":"
                            if td_text.lower().startswith(prefix.lower()):
                                val = td_text[len(prefix):].strip()
                                if val and lbl not in fields:
                                    fields[lbl] = val
                                break

                        # Case 2: label alone in this TD, value in next sibling
                        matched_lbl = _label_match(td_text)
                        if matched_lbl and matched_lbl not in fields:
                            # Check same-TD didn't already fill it
                            td_clean = td_text.strip().rstrip(":").strip()
                            if td_clean.lower() == matched_lbl.lower():
                                # Walk next siblings — Legistar sometimes has
                                # a spacer <td> between label and value
                                for sib in td.find_next_siblings("td", limit=3):
                                    sval = sib.get_text(" ", strip=True)
                                    if sval and len(sval) < 20000:
                                        fields[matched_lbl] = sval
                                        break

                # For the Title field specifically, the value is often a long
                # resolution body that spans paragraphs inside a single TD.
                # If we captured it but it's suspiciously short, try again
                # looking for the longest nearby TD after a 'Title:' label.
                if fields.get("Title") and len(fields["Title"]) < 50:
                    for s in all_soups:
                        for td in s.find_all("td"):
                            t = td.get_text(" ", strip=True)
                            if t.strip().rstrip(":").strip().lower() == "title":
                                # Grab the largest-text sibling in the next
                                # few cells — this catches multi-line titles
                                best = fields["Title"]
                                for sib in td.find_next_siblings("td", limit=3):
                                    sval = sib.get_text(" ", strip=True)
                                    if len(sval) > len(best):
                                        best = sval
                                fields["Title"] = best
                                break

                item["short_title"] = fields.get("File Name", item.get("title", ""))
                item["title"] = item["short_title"]
                item["full_title"] = fields.get("Title", "")
                item["sponsor"] = fields.get("Sponsors", fields.get("Sponsor", ""))
                item["file_type"] = fields.get("File Type", "")
                item["status"] = fields.get("Status", "")
                item["control"] = fields.get("Control", "")
                item["introduced"] = fields.get("Introduced", "")
                item["bcc_agenda_date"] = fields.get("Agenda Date", "")
                item["bcc_agenda_item_number"] = fields.get("Agenda Item Number", "")
                item["requester"] = fields.get("Requester", "")
                item["notes"] = fields.get("Notes", "")

                routing = [f"File Number: {mid}"]
                for k, lbl in [
                    ("short_title", "File Name"), ("full_title", "Full Title"),
                    ("file_type", "File Type"), ("status", "Status"),
                    ("sponsor", "Sponsors"), ("control", "Control"),
                    ("introduced", "Introduced"),
                    ("bcc_agenda_date", "BCC Agenda Date"),
                    ("bcc_agenda_item_number", "BCC Agenda Item #"),
                    ("notes", "Notes"), ("requester", "Requester"),
                ]:
                    v = item.get(k, "")
                    if v:
                        routing.append(f"{lbl}: {v}")

                leg_hist = ""
                # Case-insensitive search for legislative history section
                page_lower = page_text.lower()
                lh_idx = page_lower.find("legislative history")
                if lh_idx >= 0:
                    # Use the original-case text from the found position
                    end = page_lower.find("legislative text", lh_idx)
                    if end == -1:
                        end = lh_idx + 3000
                    leg_hist = page_text[lh_idx:end].strip()
                    routing.append("\n" + leg_hist)
                    log.info(f"    Leg history FOUND at pos {lh_idx}, len={len(leg_hist)}, preview: {leg_hist[:150]}")
                else:
                    log.info(f"    NO 'legislative history' in page_text (len={len(page_text)})")
                    log.info(f"    page_text last 300: ...{page_text[-300:]}")
                    # Try finding it in the raw HTML as well
                    raw_lower = resp.text.lower()
                    raw_idx = raw_lower.find("legislative history")
                    if raw_idx >= 0:
                        log.info(f"    Found 'legislative history' in raw HTML at pos {raw_idx} but NOT in parsed text!")
                        # Extract a snippet around that position
                        snippet = resp.text[max(0,raw_idx-50):raw_idx+200]
                        log.info(f"    HTML snippet: {snippet}")
                    else:
                        log.info(f"    Not in raw HTML either. URL was: {url}")
                item["legislative_history_raw"] = leg_hist

                if "Legislative Text" in page_text:
                    idx = page_text.index("Legislative Text")
                    routing.append("\n" + page_text[idx:idx + 3000])

                item["routing_info"] = "\n".join(routing)
                item["page_text"] = item["routing_info"]

                if not item.get("pdf_url"):
                    for s in all_soups:
                        for lk in s.find_all("a", href=True):
                            if ".pdf" in lk["href"].lower():
                                item["pdf_url"] = urljoin(url, lk["href"])
                                break
                        if item.get("pdf_url"):
                            break

                log.info(f"    Routing: {item['short_title'][:60]} | Notes: {item.get('notes','')[:40]}")
                return item
            except Exception as e:
                log.debug(f"    matter.asp failed: {e}")
        return item

    def download_pdf(self, url, save_dir):
        try:
            resp = self.session.get(url, timeout=60)
            resp.raise_for_status()
            fn = re.sub(r"[^\w\-.]", "_", url.split("/")[-1].split("?")[0])
            if not fn.endswith(".pdf"):
                fn += ".pdf"
            fp = Path(save_dir) / fn
            fp.write_bytes(resp.content)
            return fp
        except Exception as e:
            log.error(f"  PDF download failed: {e}")
            return None


# Sentinel returned when a PDF has no extractable text (image-only / scanned).
# The agent checks for this prefix and skips LLM generation, marking the item
# as needing manual review instead of producing a bogus "can't see content" brief.
IMAGE_ONLY_SENTINEL = "[IMAGE_ONLY_PDF]"


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from a PDF. Tries embedded text first, falls back to OCR
    for any pages that have no embedded text (handles mixed PDFs where some
    pages are text and some are scanned images). Returns IMAGE_ONLY_SENTINEL
    only if NO text can be recovered from ANY page."""
    try:
        doc = fitz.open(str(pdf_path))
        parts = []
        empty_page_indices = []  # 0-based indices of pages with no text
        total_pages = len(doc)

        for i, page in enumerate(doc, 1):
            t = page.get_text()
            if t.strip():
                parts.append((i, f"--- Page {i} ---\n{t}"))
            else:
                empty_page_indices.append(i - 1)  # 0-based for fitz

        # Try OCR on any empty pages (not just when ALL pages are empty).
        # This handles mixed PDFs where cover pages have text but
        # attachment pages are scanned images.
        if empty_page_indices:
            log.info(f"  {len(empty_page_indices)} of {total_pages} pages have no embedded text — attempting OCR…")
            try:
                for idx in empty_page_indices:
                    page = doc[idx]
                    page_num = idx + 1
                    try:
                        tp = page.get_textpage_ocr(full=True)
                        ot = page.get_text(textpage=tp) or ""
                    except Exception:
                        ot = ""
                    if ot.strip():
                        parts.append((page_num, f"--- Page {page_num} (OCR) ---\n{ot}"))
                    else:
                        # Note the empty page so the AI knows content existed
                        parts.append((page_num, f"--- Page {page_num} ---\n[Scanned image — OCR could not extract text. This page may contain tables, charts, or handwritten content.]"))
            except Exception as oe:
                log.warning(f"  OCR unavailable or failed: {oe}")
                # Still note the empty pages
                for idx in empty_page_indices:
                    page_num = idx + 1
                    parts.append((page_num, f"--- Page {page_num} ---\n[Scanned image — OCR unavailable. This page may contain tables, charts, or other visual content.]"))

        doc.close()

        if not parts:
            return IMAGE_ONLY_SENTINEL

        # Sort by page number to maintain document order
        parts.sort(key=lambda x: x[0])
        text = "\n".join(p[1] for p in parts)

        # Raised from 30k → 80k (April 2026): the analyzer does its own
        # section-aware trimming down to its budget. Capping too aggressively
        # here caused fiscal-impact attachments and legislative history pages
        # to get dropped before the analyzer ever saw them.
        return text[:80000] if len(text) > 80000 else text
    except Exception as e:
        log.error(f"  PDF extract failed: {e}")
        return ""
