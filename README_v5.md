# OCA Agenda Intelligence Agent v5

Automated committee agenda analysis for the Office of the Commission Auditor. Scrapes Miami-Dade County BCC committee agendas, downloads legislative PDFs, and generates standardized reports using the Claude API.

## What's New in v5

### Bug Fixes
1. **Clean output** — No markdown symbols (`**`, `##`, `*`) in Word docs or Excel. No Claude meta-commentary ("Based on the provided documents..."). Proper Word formatting with bold labels and centered-dot bullets.
2. **Sub-item capture** — Committee item numbers now capture `1G1`, `3A1`, `3A9 SUPPLEMENT`, not just section headers like `1G` or `3A`.
3. **Robust Part 1/Part 2 split** — Multiple split markers tried. Part 1 is guaranteed to have content (minimum 50 chars). Excel Part 1 column is never empty.
4. **Notes and Title parsing** — Dual-approach parsing of matter.asp pages (same-TD and sibling-TD patterns) correctly extracts Notes, full Title, and all routing fields.

### New Features
5. **Watch Points summary** — Each Part 1 analysis ends with a 1-2 sentence Watch Points section. Extracted into its own Excel column.
6. **Skip already-processed** — Compares live item count vs existing Excel rows. Fully processed committees are skipped with a log message.
7. **Incremental processing** — Only NEW items (by matter ID) are analyzed. Results are appended to existing Excel and Word files.
8. **AGENDA_UPDATES.txt** — Timestamped notification log in the output directory showing what was added in each run.
9. **Highlighted new rows** — Appended rows in Excel get a yellow highlight so you can spot what's new at a glance.

### New Interface
- **Gradio web app** — Run with `--web` flag for a browser-based interface with committee checkboxes, progress tracking, and file downloads.

## Setup

### 1. Install Python 3.9+
```bash
python3 --version
```

### 2. Install Dependencies
```bash
pip install -r requirements_v5.txt
```

### 3. Configure API Key
Create a `.env` file:
```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

## Usage

### Command Line

```bash
# Single date, all committees
python oca_agenda_agent_v5.py --date 4/15/2026

# Single date, one committee
python oca_agenda_agent_v5.py --date 4/15/2026 --committee "Safety"

# Date range
python oca_agenda_agent_v5.py --from-date 4/1/2026

# Custom output directory
python oca_agenda_agent_v5.py --date 4/15/2026 --output-dir april_reports

# Verbose logging
python oca_agenda_agent_v5.py --date 4/15/2026 -v
```

### Web Interface (Gradio)

```bash
python oca_agenda_agent_v5.py --web
```
Opens at http://localhost:7860 with:
- Date picker
- Committee checkboxes (leave empty = all)
- Progress bar
- File downloads
- Live update log

### Incremental Re-runs

Run the same command again after new items appear:
```bash
python oca_agenda_agent_v5.py --date 4/15/2026
# First run: processes all 12 items
# Second run: "SKIPPING: All 12 items already processed"
# After 2 new items posted: processes only the 2 new ones
```

## Output Structure

```
output/
  Safety_and_Health_Committee_Part1_Tracking.xlsx    # Excel tracking
  Safety_and_Health_Committee_Part2_Research.docx     # Word research briefs
  Appropriations_Committee_Part1_Tracking.xlsx
  Appropriations_Committee_Part2_Research.docx
  AGENDA_UPDATES.txt                                 # Change log
```

### Excel (Part 1) Columns
| Column | Content |
|--------|---------|
| Cmte Agenda Date | Committee meeting date |
| Cmte Item # | Sub-item number (3A1, not just 3A) |
| BCC Agenda Date | Board meeting date |
| BCC Item # | Board item number |
| File # | Legislative matter ID |
| Short Title | File name from matter.asp |
| Full Title | Complete title text |
| Notes | Notes field from matter.asp |
| Leg History (AI Summary) | AI-condensed legislative history |
| File Type | Resolution, Ordinance, etc. |
| Status | Current status |
| Control | Controlling body |
| AI Summary (Part 1) | OCA Agenda Debrief summary |
| Watch Points | What Commissioners should focus on |

### Word (Part 2) Format
- Committee header with generation timestamp
- Per-item sections with bold labels, centered-dot bullets
- Research context with inline source citations
- Watch Points per item
- Clean separators between items

## Cost Estimate

| Scope | Estimated Cost |
|-------|---------------|
| Per item | ~$0.02-0.05 |
| Per committee (15 items) | ~$0.30-0.75 |
| Full scan (13 committees) | ~$3-15 |
| Incremental re-run (2 new items) | ~$0.04-0.10 |

## Upgrading from v4

Drop-in replacement. Just use `oca_agenda_agent_v5.py` instead of `oca_agenda_agent.py`. Existing output files are compatible — v5 will detect them and append incrementally.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `No module named 'gradio'` | `pip install gradio` (only needed for `--web`) |
| `Rate limit error` | Built-in 20s delay + 60s retry. Reduce committees if persistent. |
| `All items already processed` | Working as intended. Delete the Excel to reprocess. |
| `PDF extraction returned empty` | Some items are scanned images. OCR support planned. |
| Yellow highlight not showing | Open in Excel (not Google Sheets) for full formatting. |
