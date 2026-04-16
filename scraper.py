"""
scraper.py — Miami-Dade agenda scraping for OCA Agenda Intelligence v6
Preserves all v5 scraper logic. Minor refactor: standalone module.
"""
import re, logging, time
from pathlib import Path
from datetime import datetime
from paths import PDF_CACHE_DIR
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF

log = logging.getLogger("oca-agent")

BASE_URL = "https://www.miamidade.gov/govaction/"

# Fallback committee list (current term Feb 2025+)
COMMITTEES = {
    "Appropriations Committee": "APC",
    "Aviation and Seaport Committee": "AASC",
    "BCC - Comprehensive Development Master Plan & Zoning": "CDMZ",
    "Board of County Commissioners": "CC",
    "Government Efficiency & Transparency Ad Hoc Cmte": "GETC",
    "Housing Committee": "HOUS",
    "Infrastructure, Innovation & Technology Committee": "IITC",
    "Intergovernmental and Economic Impact Committee": "IEIC",
    "Joint Appropriations & Gov Eff & Transparency Cmte": "JAGE",
    "Policy Council": "PC",
    "Recreation, Tourism, and Resiliency Committee": "RTRC",
    "Safety and Health Committee": "SHC",
    "Transportation Cmte": "TRNS",
}


class MiamiDadeScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "OCA-Agenda-Agent/6.0"})
        self._form_cache = {}

    def get_committees(self):
        log.info("Fetching committee list...")
        try:
            resp = self.session.get(
                BASE_URL + "agendas.asp?Action=Agendas&Oper=DisplayList", timeout=30
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
        return COMMITTEES

    def get_agenda_dates(self, committee_name, committee_code):
        url = (f"{BASE_URL}agendas.asp?Action=Agendas&Oper=DisplayAgenda"
               f"&Agenda={committee_code}&AgendaName={quote_plus(committee_name)}")
        log.info(f"  Checking dates for {committee_name}...")
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            dates, date_to_id = [], {}
            for select in soup.find_all("select"):
                for opt in select.find_all("option"):
                    oid = opt.get("value", "").strip()
                    otxt = opt.get_text(strip=True)
                    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", otxt)
                    if m:
                        dates.append(m.group(1))
                        date_to_id[m.group(1)] = oid
                if dates:
                    break
            self._form_cache[committee_code] = {"date_to_id": date_to_id}
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
        date_to_id = form_info.get("date_to_id", {})
        meeting_id = date_to_id.get(date, "")
        if not meeting_id:
            return []
        agenda_url = (f"{BASE_URL}legistarfiles/SourceCode/searchforpdf.asp"
                      f"?documentKey={meeting_id}&documenttype=agenda")
        log.info(f"  Fetching agenda: {committee_name} ({date})")
        try:
            resp = self.session.get(agenda_url, timeout=60, allow_redirects=True)
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "").lower()
            if "pdf" in ct or resp.url.lower().endswith(".pdf"):
                pp = PDF_CACHE_DIR / f"agenda_{committee_code}_{meeting_id}.pdf"
                pp.write_bytes(resp.content)
                return [{
                    "committee_item_number": "1",
                    "matter_id": meeting_id,
                    "item_number": "Full Agenda",
                    "title": committee_name,
                    "pdf_url": resp.url,
                    "pdf_path": str(pp),
                    "page_text": "",
                }]
            return self._parse_committee_agenda(resp.text, resp.url)
        except Exception as e:
            log.error(f"  Failed: {e}")
            return []

    def _parse_committee_agenda(self, html, base_url):
        """Parse committee agenda HTML — captures sub-items like 3A1, 1G1."""
        soup = BeautifulSoup(html, "html.parser")
        items = []
        current_comm_item = ""

        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            first_text = tds[0].get_text(strip=True)

            m = re.match(
                r'^(\d+[A-Z]\d*(?:\s+(?:SUPPLEMENT|SUBSTITUTE|SUB))?)\s*$',
                first_text, re.I
            )
            if m:
                current_comm_item = m.group(1).strip().upper()

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

                    items.append({
                        "committee_item_number": current_comm_item,
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
        bridge_url = (f"{BASE_URL}legistarfiles/SourceCode/searchforpdf.asp"
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
                log.info(f"    Bridge resolved: {matter_url[:120]}")
                return [matter_url]
            # Also check for a direct link/redirect to matter.asp
            for link in BeautifulSoup(resp.text, "html.parser").find_all("a", href=True):
                if "matter.asp" in link["href"].lower():
                    resolved = urljoin(bridge_url, link["href"])
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
                    log.debug(f"    matter.asp too short ({len(resp.text)} chars), trying next URL")
                    continue
                log.debug(f"    matter.asp OK: {len(resp.text)} chars from {url}")
                # Store the final matter.asp URL (not the bridge) as the detail link
                item["detail_url"] = url
                soup = BeautifulSoup(resp.text, "html.parser")
                page_text = soup.get_text(separator="\n", strip=True)
                log.debug(f"    page_text: {len(page_text)} chars")

                # Approach A: label and value in same TD
                fields_same_td = {}
                for bold in soup.find_all(["b", "strong"]):
                    ptd = bold.find_parent("td")
                    if ptd:
                        label = bold.get_text(strip=True).rstrip(":")
                        full = ptd.get_text(strip=True)
                        if ":" in full and label:
                            val = full.split(":", 1)[1].strip()
                            if val:
                                fields_same_td[label] = val

                # Approach B: label in one TD, value in next sibling TD
                fields_sibling = {}
                for bold in soup.find_all(["b", "strong"]):
                    label = bold.get_text(strip=True).rstrip(":")
                    if not label:
                        continue
                    ptd = bold.find_parent("td")
                    if not ptd:
                        continue
                    td_text = ptd.get_text(strip=True)
                    has_value = ":" in td_text and len(td_text.split(":", 1)[1].strip()) > 0
                    if not has_value:
                        next_td = ptd.find_next_sibling("td")
                        if next_td:
                            val = next_td.get_text(strip=True)
                            if val:
                                fields_sibling[label] = val

                fields = {}
                fields.update(fields_same_td)
                fields.update(fields_sibling)

                item["short_title"] = fields.get("File Name", item.get("title", ""))
                item["title"] = item["short_title"]
                item["full_title"] = fields.get("Title", "")
                item["sponsor"] = fields.get("Sponsors", "")
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
                    log.debug(f"    Leg history found at pos {lh_idx}, len={len(leg_hist)}")
                else:
                    log.debug(f"    No 'Legislative History' in page_text (len={len(page_text)})")
                    # Try finding it in the raw HTML as well
                    raw_lower = resp.text.lower()
                    raw_idx = raw_lower.find("legislative history")
                    if raw_idx >= 0:
                        log.info(f"    Found 'legislative history' in raw HTML at pos {raw_idx} but NOT in parsed text!")
                item["legislative_history_raw"] = leg_hist

                if "Legislative Text" in page_text:
                    idx = page_text.index("Legislative Text")
                    routing.append("\n" + page_text[idx:idx + 3000])

                item["routing_info"] = "\n".join(routing)
                item["page_text"] = item["routing_info"]

                if not item.get("pdf_url"):
                    for lk in soup.find_all("a", href=True):
                        if ".pdf" in lk["href"].lower():
                            item["pdf_url"] = urljoin(url, lk["href"])
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
    if the PDF is image-only. Returns IMAGE_ONLY_SENTINEL if no text can be
    recovered (including when OCR is unavailable), so callers can handle it."""
    try:
        doc = fitz.open(str(pdf_path))
        parts = []
        empty_pages = 0
        total_pages = len(doc)
        for i, page in enumerate(doc, 1):
            t = page.get_text()
            if t.strip():
                parts.append(f"--- Page {i} ---\n{t}")
            else:
                empty_pages += 1

        # If every (or nearly every) page is empty, this is an image-only
        # PDF — try OCR via PyMuPDF's built-in Tesseract bridge.
        if total_pages > 0 and empty_pages == total_pages:
            log.info(f"  PDF has no embedded text ({total_pages} pages) — attempting OCR…")
            try:
                ocr_parts = []
                for i, page in enumerate(doc, 1):
                    try:
                        tp = page.get_textpage_ocr(full=True)
                        ot = page.get_text(textpage=tp) or ""
                    except Exception:
                        ot = ""
                    if ot.strip():
                        ocr_parts.append(f"--- Page {i} (OCR) ---\n{ot}")
                if ocr_parts:
                    doc.close()
                    text = "\n".join(ocr_parts)
                    return text[:30000] if len(text) > 30000 else text
            except Exception as oe:
                log.warning(f"  OCR unavailable or failed: {oe}")
            doc.close()
            # No text AND no OCR — flag it for manual handling.
            return IMAGE_ONLY_SENTINEL

        doc.close()
        text = "\n".join(parts)
        return text[:30000] if len(text) > 30000 else text
    except Exception as e:
        log.error(f"  PDF extract failed: {e}")
        return ""
