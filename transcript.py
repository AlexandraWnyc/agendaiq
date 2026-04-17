"""
transcript.py — Meeting transcript extraction and per-item segmentation.

Pipeline:
  1. Search the Miami-Dade BCC YouTube channel for a meeting video
     matching a given committee name + date (fuzzy).
  2. Download the auto-generated English captions via yt-dlp.
  3. Send the full transcript + the agenda item list to Claude AI,
     which segments the transcript by item and summarizes the
     discussion for each.
  4. Store per-item discussion summaries in the database.

Designed to run as a post-meeting backfill — agendas are scraped
before the meeting; transcripts become available 1-2 days after.
"""

import os, re, json, logging, subprocess, tempfile, time, io
from pathlib import Path
from datetime import datetime, timedelta
from difflib import SequenceMatcher

log = logging.getLogger("oca-agent")

# ── Constants ────────────────────────────────────────────────
YOUTUBE_CHANNEL_ID = "UCjwHcRTA0ZuOdsXxEBKnq0A"  # @miami-dadebcc6863
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@miami-dadebcc6863"

# Granicus streaming archive
GRANICUS_ARCHIVE_URL = "https://miamidade.granicus.com/ViewPublisher.php?view_id=2"
GRANICUS_SEARCH_URL = "https://miamidade.granicus.com/ViewSearchResults.php"
GRANICUS_RSS_URL = "https://miamidade.granicus.com/ViewPublisherRSS.php?view_id=2"

# Known committee name aliases — maps canonical DB names to patterns
# that might appear in YouTube video titles.  The fuzzy matcher uses
# these PLUS free-form matching, so even unlisted variants get caught.
COMMITTEE_ALIASES = {
    # BCC
    "Board of County Commissioners": [
        "Board of County Commissioners",
        "BCC Regular", "BCC Meeting", "County Commission",
    ],
    # Zoning
    "Comprehensive Development Master Plan & Zoning": [
        "Master Plan & Zoning", "Master Plan and Zoning",
        "Zoning", "CDMP", "Comprehensive Development",
    ],
    # Standing committees (names shift over time)
    "Government Operations": [
        "Government Operations", "Gov Operations", "Gov Ops",
    ],
    "Infrastructure, Innovation, and Technology": [
        "Infrastructure", "Innovation and Technology",
        "Infrastructure, Innovation",
    ],
    "Housing and Community Development": [
        "Housing", "Community Development",
        "Housing and Community", "Housing Committee",
    ],
    "Public Safety and Rehabilitation": [
        "Public Safety", "Rehabilitation",
    ],
    "Intergovernmental and Economic Impact": [
        "Intergovernmental", "Economic Impact",
        "Intergovernmental and Economic",
    ],
    "Recreation, Tourism, and Resiliency": [
        "Recreation", "Tourism", "Resiliency",
        "Recreation, Tourism",
    ],
    "Transportation and Finance": [
        "Transportation", "Finance",
        "Transportation and Finance",
    ],
    "Aviation and Seaport": [
        "Aviation", "Seaport",
        "Aviation and Seaport",
    ],
    "Health Care and County Operations": [
        "Health Care", "County Operations",
        "Health Care and County",
    ],
    "Parks and Culture": [
        "Parks", "Culture", "Parks and Culture",
    ],
    "Trade and Tourism": [
        "Trade", "Trade and Tourism",
    ],
}

# Flatten for reverse lookup
_ALIAS_FLAT = {}
for canonical, aliases in COMMITTEE_ALIASES.items():
    for a in aliases:
        _ALIAS_FLAT[a.lower()] = canonical


# ── Granicus MP3 + Whisper transcription ─────────────────────

def search_granicus_mp3(committee_name: str, meeting_date: str) -> dict | None:
    """Search Granicus streaming archive for an MP3 matching committee + date.

    Strategy 1: Search endpoint (returns ALL results, covers full archive)
    Strategy 2: HTML scraper on main page (last ~10 meetings)

    Returns: {"mp3_url": str, "title": str, "page_url": str} or None
    """
    import requests
    from bs4 import BeautifulSoup

    try:
        target_dt = datetime.strptime(meeting_date, "%Y-%m-%d")
    except ValueError:
        log.warning(f"  Invalid meeting date: {meeting_date}")
        return None

    date_patterns = [
        target_dt.strftime("%b") + f" {target_dt.day}, {target_dt.year}",
        target_dt.strftime("%b %d, %Y"),
        target_dt.strftime("%b") + f"  {target_dt.day}, {target_dt.year}",
        target_dt.strftime("%B %d, %Y"),
        f"{target_dt.month}/{target_dt.day}/{target_dt.year}",
    ]

    log.info(f"  Searching Granicus for '{committee_name}' on {meeting_date}")

    # ── Strategy 1: Granicus keyword search (covers entire archive) ──
    # Extract first keyword from committee name for broad search
    search_words = [w for w in committee_name.split()
                    if w.lower() not in ('of', 'and', 'the', 'for', 'in', 'committee',
                                         'cmte', 'ad', 'hoc', 'miami-dade')]
    search_term = search_words[0] if search_words else committee_name.split()[0]

    for strategy_name, fetch_url in [
        ("search", f"{GRANICUS_SEARCH_URL}?view_id=2&keywords={search_term}"),
        ("main_page", GRANICUS_ARCHIVE_URL),
    ]:
        log.info(f"  Granicus {strategy_name}: {fetch_url[:100]}")
        try:
            resp = requests.get(fetch_url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"  Granicus {strategy_name} fetch failed: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        best_match = None
        best_score = 0
        candidates_checked = 0

        # Find all MP3 links
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if ".mp3" not in href.lower():
                continue

            row = link.find_parent("tr")
            if not row:
                continue

            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            row_name = cells[0].get_text(strip=True)
            row_date_text = cells[1].get_text(strip=True)

            # Date match — check all patterns
            date_match = any(dp.lower() in row_date_text.lower() for dp in date_patterns)
            if not date_match:
                continue

            candidates_checked += 1
            name_score = _committee_match_score(committee_name, row_name)
            log.info(f"  Candidate: '{row_name}' date='{row_date_text}' score={name_score:.2f}")

            if name_score < 0.25:  # lowered threshold
                continue

            total_score = name_score + 1.0
            if total_score > best_score:
                best_score = total_score
                best_match = {
                    "mp3_url": href if href.startswith("http") else f"https://miamidade.granicus.com/{href}",
                    "title": row_name,
                    "page_url": fetch_url,
                    "match_score": total_score,
                }

        log.info(f"  Granicus {strategy_name}: {candidates_checked} date-matched rows checked")

        if best_match:
            log.info(f"  Found MP3: {best_match['mp3_url'][:80]}... (score={best_match['match_score']:.2f})")
            return best_match

    # ── Strategy 3: Try searching for the meeting date directly ──
    date_search = target_dt.strftime("%m/%d/%Y")
    try:
        log.info(f"  Granicus date search: {date_search}")
        resp = requests.get(
            f"{GRANICUS_SEARCH_URL}?view_id=2&keywords={date_search}",
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if ".mp3" not in href.lower():
                continue
            row = link.find_parent("tr")
            if not row:
                continue
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            row_name = cells[0].get_text(strip=True)
            name_score = _committee_match_score(committee_name, row_name)
            log.info(f"  Date-search candidate: '{row_name}' score={name_score:.2f}")
            if name_score >= 0.25:
                mp3_url = href if href.startswith("http") else f"https://miamidade.granicus.com/{href}"
                log.info(f"  Found MP3 via date search: {mp3_url[:80]}")
                return {
                    "mp3_url": mp3_url,
                    "title": row_name,
                    "page_url": f"{GRANICUS_SEARCH_URL}?view_id=2&keywords={date_search}",
                    "match_score": name_score + 1.0,
                }
    except Exception as e:
        log.warning(f"  Granicus date search failed: {e}")

    log.info(f"  No MP3 found on Granicus for '{committee_name}' {meeting_date}")
    return None


def download_mp3(mp3_url: str, output_dir: Path) -> Path | None:
    """Download an MP3 file from Granicus or similar source.

    Returns the local file path, or None on failure.
    """
    import requests

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = "meeting_audio.mp3"
    local_path = output_dir / filename

    log.info(f"  Downloading MP3: {mp3_url[:80]}…")

    try:
        resp = requests.get(mp3_url, timeout=300, stream=True)
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
                downloaded += len(chunk)

        size_mb = local_path.stat().st_size / (1024 * 1024)
        log.info(f"  Downloaded {size_mb:.1f} MB to {local_path}")
        return local_path

    except Exception as e:
        log.error(f"  MP3 download failed: {e}")
        return None


def transcribe_with_whisper(audio_path: Path, api_key: str = None) -> str:
    """Transcribe an audio file using OpenAI Whisper API.

    Handles files > 25MB by splitting into chunks.
    Returns timestamped transcript string.
    """
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        log.error("  No OpenAI API key — set OPENAI_API_KEY env var")
        return ""

    import openai
    client = openai.OpenAI(api_key=api_key)

    file_size = audio_path.stat().st_size
    max_size = 24 * 1024 * 1024  # 24MB to be safe (limit is 25MB)

    if file_size <= max_size:
        # Single file — straightforward
        return _whisper_single(client, audio_path)
    else:
        # Split into chunks and transcribe each
        return _whisper_chunked(client, audio_path, max_size)


def _whisper_single(client, audio_path: Path) -> str:
    """Transcribe a single audio file with Whisper."""
    log.info(f"  Transcribing {audio_path.name} ({audio_path.stat().st_size / 1024 / 1024:.1f} MB)…")

    try:
        with open(audio_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )

        lines = []
        if hasattr(result, 'segments') and result.segments:
            for seg in result.segments:
                ts = _seconds_to_timestamp(seg.get("start", seg.start if hasattr(seg, 'start') else 0))
                text = seg.get("text", seg.text if hasattr(seg, 'text') else "").strip()
                if text:
                    lines.append(f"[{ts}] {text}")
        elif hasattr(result, 'text') and result.text:
            # Fallback: no segments, just full text
            lines.append(f"[00:00:00] {result.text}")

        transcript = "\n".join(lines)
        log.info(f"  Whisper transcription: {len(lines)} segments, {len(transcript)} chars")
        return transcript

    except Exception as e:
        log.error(f"  Whisper transcription failed: {e}")
        return ""


def _whisper_chunked(client, audio_path: Path, max_size: int) -> str:
    """Split a large audio file into chunks and transcribe each."""
    log.info(f"  Audio too large ({audio_path.stat().st_size / 1024 / 1024:.1f} MB), splitting…")

    # Use ffmpeg to split into chunks
    chunk_dir = audio_path.parent / "chunks"
    chunk_dir.mkdir(exist_ok=True)

    # Calculate chunk duration based on file size
    file_size = audio_path.stat().st_size
    # Estimate: 1 minute of MP3 ≈ 1MB at 128kbps
    total_duration_est = file_size / (128 * 1024 / 8)  # seconds
    chunk_duration = int(max_size / file_size * total_duration_est * 0.9)  # 90% of theoretical max
    chunk_duration = max(300, min(chunk_duration, 1500))  # 5-25 minutes per chunk

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", str(audio_path),
                "-f", "segment",
                "-segment_time", str(chunk_duration),
                "-c", "copy",
                str(chunk_dir / "chunk_%03d.mp3"),
            ],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        log.error("  ffmpeg not available — cannot split large audio files")
        # Try to transcribe as-is (may fail if > 25MB)
        return _whisper_single(client, audio_path)
    except Exception as e:
        log.error(f"  ffmpeg split failed: {e}")
        return _whisper_single(client, audio_path)

    chunks = sorted(chunk_dir.glob("chunk_*.mp3"))
    if not chunks:
        log.warning("  No chunks produced by ffmpeg")
        return _whisper_single(client, audio_path)

    log.info(f"  Split into {len(chunks)} chunks ({chunk_duration}s each)")

    all_lines = []
    time_offset = 0.0
    for chunk_path in chunks:
        log.info(f"    Transcribing chunk {chunk_path.name} (offset={time_offset:.0f}s)…")
        try:
            with open(chunk_path, "rb") as f:
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )

            if hasattr(result, 'segments') and result.segments:
                for seg in result.segments:
                    start = (seg.get("start", 0) if isinstance(seg, dict) else seg.start) + time_offset
                    text = (seg.get("text", "") if isinstance(seg, dict) else seg.text).strip()
                    if text:
                        all_lines.append(f"[{_seconds_to_timestamp(start)}] {text}")
            elif hasattr(result, 'text') and result.text:
                all_lines.append(f"[{_seconds_to_timestamp(time_offset)}] {result.text}")

            # Get chunk duration for next offset
            if hasattr(result, 'duration') and result.duration:
                time_offset += result.duration
            else:
                time_offset += chunk_duration

        except Exception as e:
            log.warning(f"    Chunk {chunk_path.name} failed: {e}")
            time_offset += chunk_duration

    transcript = "\n".join(all_lines)
    log.info(f"  Full transcription: {len(all_lines)} segments, {len(transcript)} chars")
    return transcript


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = re.sub(r'[^\w\s]', ' ', s.lower())
    return re.sub(r'\s+', ' ', s).strip()


def _extract_date_from_title(title: str):
    """Try to extract a date from a YouTube video title.
    Common formats:
      '04.15.2026 - Committee Name'
      'March 17, 2026 - BCC Meeting'
      '2026-04-15 Committee'
    """
    # MM.DD.YYYY
    m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', title)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).strftime('%Y-%m-%d')
        except ValueError:
            pass
    # Month DD, YYYY
    m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s*(\d{4})', title, re.I)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").strftime('%Y-%m-%d')
        except ValueError:
            pass
    # YYYY-MM-DD
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', title)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _committee_match_score(db_body_name: str, video_title: str) -> float:
    """Score how well a YouTube video title matches a committee name.
    Returns 0.0–1.0.  Uses multiple signals:
      1. Direct substring match of known aliases
      2. Keyword overlap (Jaccard)
      3. SequenceMatcher ratio on normalized strings
    The max of all signals is returned.
    """
    title_norm = _normalize(video_title)
    body_norm = _normalize(db_body_name)
    scores = []

    # 1. Direct alias match — strongest signal
    for alias_key, canonical in _ALIAS_FLAT.items():
        if alias_key in title_norm:
            # How close is the canonical to our DB body name?
            canon_score = SequenceMatcher(None, _normalize(canonical), body_norm).ratio()
            if canon_score > 0.6:
                scores.append(0.85 + 0.15 * canon_score)

    # 2. Direct body name substring
    if body_norm in title_norm or title_norm in body_norm:
        scores.append(0.95)

    # 3. Keyword overlap (Jaccard similarity)
    body_words = set(body_norm.split()) - {'of', 'and', 'the', 'for', 'in', 'committee', 'meeting'}
    title_words = set(title_norm.split()) - {'of', 'and', 'the', 'for', 'in', 'committee', 'meeting'}
    if body_words and title_words:
        intersection = body_words & title_words
        union = body_words | title_words
        jaccard = len(intersection) / len(union) if union else 0
        # Weight by how many body words were found
        body_coverage = len(intersection) / len(body_words) if body_words else 0
        scores.append(max(jaccard, body_coverage * 0.9))

    # 4. SequenceMatcher on full normalized strings (catch-all)
    # Strip date portion from title first
    title_no_date = re.sub(r'\d{2}\.\d{2}\.\d{4}\s*[-–—]?\s*', '', title_norm).strip()
    ratio = SequenceMatcher(None, body_norm, title_no_date).ratio()
    scores.append(ratio)

    return max(scores) if scores else 0.0


# ── YouTube search + transcript download ─────────────────────

def search_youtube_videos(committee_name: str, meeting_date: str,
                          max_results: int = 10,
                          date_window_days: int = 7) -> list[dict]:
    """Search the BCC YouTube channel for videos matching a meeting.

    Uses yt-dlp to search the channel.  Returns a list of dicts:
      [{video_id, title, upload_date, duration, match_score, date_match}, ...]
    sorted by match_score descending.

    Args:
        committee_name: Body name from the meetings table (e.g. "Government Operations")
        meeting_date: ISO date string (e.g. "2026-04-15")
        max_results: How many candidates to fetch from YouTube
        date_window_days: How many days around meeting_date to accept
    """
    # Build a search query from the most distinctive words
    query_words = []
    # Add date in MM.DD.YYYY format (common in titles)
    try:
        dt = datetime.strptime(meeting_date, "%Y-%m-%d")
        query_words.append(dt.strftime("%m.%d.%Y"))
    except ValueError:
        pass
    # Add committee keywords (skip generic words)
    skip = {'of', 'and', 'the', 'for', 'in', 'board', 'county', 'commissioners',
            'committee', 'meeting', 'miami', 'dade', 'bcc'}
    for word in committee_name.split():
        if word.lower() not in skip and len(word) > 2:
            query_words.append(word)

    search_query = " ".join(query_words)
    log.info(f"  YouTube search: '{search_query}' on channel {YOUTUBE_CHANNEL_URL}")

    result = None
    # Strategy 1: Use the channel's search endpoint (most reliable)
    channel_search_url = f"{YOUTUBE_CHANNEL_URL}/search?query={'+'.join(query_words)}"
    try:
        result = subprocess.run(
            [
                "yt-dlp", "--flat-playlist", "--print-json",
                "--playlist-end", str(max_results),
                channel_search_url,
            ],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        log.warning("  YouTube channel search timed out")
    except FileNotFoundError:
        log.error("  yt-dlp not installed — run: pip install yt-dlp")
        return []

    # Strategy 2: If channel search failed, try global YouTube search
    if not result or result.returncode != 0 or not result.stdout.strip():
        log.info("  Channel search returned nothing, trying global ytsearch…")
        try:
            result = subprocess.run(
                [
                    "yt-dlp", "--flat-playlist", "--print-json",
                    "--playlist-end", str(max_results),
                    f"ytsearch{max_results}:miami dade {search_query}",
                ],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            log.warning("  YouTube global search timed out")
        except FileNotFoundError:
            pass

    # Strategy 3: If still no results, list recent channel videos and match locally
    if not result or result.returncode != 0 or not result.stdout.strip():
        log.info("  Falling back to channel video listing…")
        try:
            result = subprocess.run(
                [
                    "yt-dlp", "--flat-playlist", "--print-json",
                    "--playlist-end", str(max_results * 5),
                    f"{YOUTUBE_CHANNEL_URL}/videos",
                ],
                capture_output=True, text=True, timeout=90,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            log.warning("  All YouTube search strategies failed")
            return []

    if not result or not result.stdout.strip():
        return []

    # Parse JSON lines output
    videos = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            v = json.loads(line)
            videos.append(v)
        except json.JSONDecodeError:
            continue

    if not videos:
        log.info("  No YouTube videos found")
        return []

    # Score and filter
    target_date = None
    try:
        target_date = datetime.strptime(meeting_date, "%Y-%m-%d")
    except ValueError:
        pass

    scored = []
    for v in videos:
        vid = v.get("id") or v.get("url", "").split("v=")[-1].split("&")[0]
        title = v.get("title", "")
        upload = v.get("upload_date", "")  # YYYYMMDD
        duration = v.get("duration") or 0

        # Skip very short videos (< 5 min) — likely clips, not meetings
        if duration and duration < 300:
            continue

        # Date matching
        title_date = _extract_date_from_title(title)
        date_match = False
        date_score = 0.0
        if title_date and target_date:
            try:
                td = datetime.strptime(title_date, "%Y-%m-%d")
                delta = abs((td - target_date).days)
                if delta == 0:
                    date_score = 1.0
                    date_match = True
                elif delta <= date_window_days:
                    date_score = max(0, 1.0 - delta * 0.15)
                    date_match = True
            except ValueError:
                pass

        # Committee name matching
        name_score = _committee_match_score(committee_name, title)

        # Combined score: date is most important, name disambiguates
        if date_match and name_score > 0.3:
            combined = 0.5 * date_score + 0.5 * name_score
        elif date_match:
            combined = 0.4 * date_score + 0.1  # date match but weak name
        elif name_score > 0.7:
            combined = 0.3 * name_score  # name match but no date match
        else:
            combined = 0.1 * name_score  # weak overall

        scored.append({
            "video_id": vid,
            "title": title,
            "upload_date": upload,
            "duration": duration,
            "date_match": date_match,
            "date_score": round(date_score, 3),
            "name_score": round(name_score, 3),
            "match_score": round(combined, 3),
            "title_date": title_date,
            "url": f"https://www.youtube.com/watch?v={vid}",
        })

    scored.sort(key=lambda x: x["match_score"], reverse=True)
    return scored[:5]


def download_transcript(video_id: str, output_dir: Path = None) -> str:
    """Download captions for a YouTube video.

    Uses youtube-transcript-api (lightweight HTTP, no JS runtime needed)
    with yt-dlp as a fallback.

    Returns the transcript as a single string with timestamps, e.g.:
      [00:00:05] Good morning everyone welcome to the meeting
      [00:00:12] We'll begin with roll call
      ...

    Returns empty string if no captions available.
    """
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp())
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Strategy 1: youtube-transcript-api with optional proxy
    # YouTube blocks cloud provider IPs. Use YOUTUBE_PROXY env var to route
    # through a residential proxy (e.g. "http://user:pass@proxy:port").
    proxy_url = os.environ.get("YOUTUBE_PROXY", "")
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        import requests as _req

        # Configure proxy if available
        ytt_kwargs = {}
        if proxy_url:
            log.info(f"  Using proxy for YouTube: {proxy_url[:30]}…")
            session = _req.Session()
            session.proxies = {"http": proxy_url, "https": proxy_url}
            ytt_api = YouTubeTranscriptApi(http_client=session)
        else:
            ytt_api = YouTubeTranscriptApi()

        log.info(f"  Fetching transcript via youtube-transcript-api for {video_id}")

        # Try to get English transcript (auto-generated or manual)
        transcript = None
        try:
            transcript = ytt_api.fetch(video_id, languages=["en"])
        except Exception:
            pass

        if not transcript:
            try:
                transcript_list = ytt_api.list(video_id)
                for t in transcript_list:
                    lang = (t.language_code or "").lower()
                    if lang.startswith("en"):
                        transcript = t.fetch()
                        break
                if not transcript:
                    for t in transcript_list:
                        if t.is_translatable:
                            transcript = t.translate("en").fetch()
                            break
            except Exception as e:
                log.info(f"  youtube-transcript-api list/translate failed: {e}")

        if transcript and hasattr(transcript, 'snippets') and transcript.snippets:
            lines = []
            for snippet in transcript.snippets:
                ts = _seconds_to_timestamp(snippet.start)
                text = snippet.text.replace("\n", " ").strip()
                if text:
                    lines.append(f"[{ts}] {text}")
            result_text = "\n".join(lines)
            if result_text:
                log.info(f"  youtube-transcript-api: got {len(lines)} lines")
                return result_text

        log.info(f"  youtube-transcript-api: no transcript content returned")

    except ImportError:
        log.info("  youtube-transcript-api not installed, falling back to yt-dlp")
    except Exception as e:
        log.warning(f"  youtube-transcript-api failed: {e}")

    # ── Strategy 2: yt-dlp fallback
    out_template = str(output_dir / "transcript")
    url = f"https://www.youtube.com/watch?v={video_id}"

    yt_strategies = [
        ["--write-auto-sub", "--sub-lang", "en"],
        ["--write-sub", "--sub-lang", "en"],
        ["--write-sub", "--write-auto-sub", "--sub-lang", "en,en-US,en-orig,es"],
    ]

    for i, sub_args in enumerate(yt_strategies):
        for old_vtt in output_dir.glob("*.vtt"):
            old_vtt.unlink()
        try:
            cmd = ["yt-dlp"] + sub_args + [
                "--skip-download", "--sub-format", "vtt",
                "-o", out_template, url,
            ]
            log.info(f"  yt-dlp strategy {i+1}: {' '.join(cmd)}")
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            vtt_files = list(output_dir.glob("*.vtt"))
            if vtt_files:
                log.info(f"  yt-dlp strategy {i+1} found VTT: {vtt_files[0].name}")
                return _parse_vtt(vtt_files[0].read_text(encoding="utf-8", errors="replace"))
            log.info(f"  yt-dlp strategy {i+1}: no VTT. stderr: {proc.stderr[:300]}")
        except subprocess.TimeoutExpired:
            log.warning(f"  yt-dlp timed out (strategy {i+1})")
        except FileNotFoundError:
            log.error("  yt-dlp not installed")
            break

    log.warning(f"  No transcript found for {video_id} after all strategies")
    return ""


def _seconds_to_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _parse_vtt(vtt_text: str) -> str:
    """Parse a WebVTT file into clean timestamped lines.

    Input (VTT format):
      00:00:05.000 --> 00:00:08.000
      Good morning everyone
      welcome to the meeting

    Output:
      [00:00:05] Good morning everyone welcome to the meeting
    """
    lines = vtt_text.split("\n")
    segments = []
    current_time = None
    current_text = []

    # VTT timestamp pattern
    ts_pattern = re.compile(r'(\d{2}:\d{2}:\d{2})\.\d{3}\s*-->')

    for line in lines:
        line = line.strip()

        # Skip VTT header, NOTE blocks, style blocks
        if line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("STYLE"):
            continue
        if not line or re.match(r'^\d+$', line):  # sequence numbers or blank
            continue

        ts_match = ts_pattern.match(line)
        if ts_match:
            # Save previous segment
            if current_time and current_text:
                text = " ".join(current_text).strip()
                # Remove VTT formatting tags
                text = re.sub(r'<[^>]+>', '', text)
                text = re.sub(r'\s+', ' ', text).strip()
                if text:
                    segments.append(f"[{current_time}] {text}")
            current_time = ts_match.group(1)
            current_text = []
        elif current_time is not None:
            # This is caption text
            clean = re.sub(r'<[^>]+>', '', line).strip()
            if clean:
                current_text.append(clean)

    # Last segment
    if current_time and current_text:
        text = " ".join(current_text).strip()
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            segments.append(f"[{current_time}] {text}")

    # Deduplicate consecutive segments with identical text
    # (VTT often has overlapping cues for the same speech)
    deduped = []
    prev_text = ""
    for seg in segments:
        # Extract just the text portion after the timestamp
        text_part = seg.split("] ", 1)[1] if "] " in seg else seg
        if text_part != prev_text:
            deduped.append(seg)
            prev_text = text_part

    return "\n".join(deduped)


# ── AI-powered transcript segmentation ───────────────────────

def segment_transcript_by_items(transcript: str, items: list[dict],
                                 committee_name: str, meeting_date: str,
                                 api_key: str = None) -> dict:
    """Use Claude to segment a meeting transcript by agenda items.

    Args:
        transcript: Full timestamped transcript string
        items: List of agenda items, each with keys:
            file_number, short_title, committee_item_number, appearance_title
        committee_name: For context
        meeting_date: For context
        api_key: Anthropic API key (falls back to env var)

    Returns:
        Dict mapping file_number -> {
            "discussion_summary": str,
            "timestamp_start": str,  # "HH:MM:SS"
            "timestamp_end": str,
            "speakers": [str],
            "vote_result": str or None,
            "amendments": str or None,
        }
    """
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.error("  No API key for transcript segmentation")
        return {}

    # Build the item reference list
    item_lines = []
    for it in items:
        fn = it.get("file_number", "")
        cn = it.get("committee_item_number", "")
        title = it.get("short_title") or it.get("appearance_title", "")
        item_lines.append(f"  - Item #{cn} (File #{fn}): {title}")
    items_block = "\n".join(item_lines)

    # Trim transcript if too long (Claude Haiku context ~200k tokens)
    # ~4 chars per token, keep under 150k tokens worth
    max_chars = 500_000
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars] + "\n[TRANSCRIPT TRUNCATED]"

    prompt = f"""You are an expert legislative analyst reviewing the transcript of a {committee_name} meeting held on {meeting_date} for the Miami-Dade County Office of the County Attorney.

Below is the full auto-generated transcript with timestamps, followed by the agenda items discussed at this meeting.

Your task: For EACH agenda item, find where it was discussed and produce a thorough analysis of the discussion that captures not just WHAT was said but HOW it was said — the sentiment, tone, intent, and political dynamics.

AGENDA ITEMS:
{items_block}

TRANSCRIPT:
{transcript}

---

For each item, output a JSON object. Items NOT discussed (consent agenda, deferred) should still be noted.

Output ONLY a JSON array with this structure:
[
  {{
    "file_number": "...",
    "item_number": "...",
    "discussed": true/false,
    "timestamp_start": "HH:MM:SS",
    "timestamp_end": "HH:MM:SS",
    "discussion_summary": "3-5 sentence summary of the substantive discussion — what was said, key arguments, specific dollar amounts, dates, or conditions mentioned",
    "speakers": ["Commissioner Name", ...],
    "speaker_positions": {{
      "Commissioner Name": "Supportive — argued this would benefit District 2 residents",
      "Commissioner Other": "Opposed — raised fiscal concerns about the $5M allocation"
    }},
    "sentiment": {{
      "overall": "supportive|opposed|divided|neutral|contentious",
      "description": "Brief characterization of the room's mood — was there consensus, heated debate, disinterest?"
    }},
    "tone_analysis": {{
      "tone": "collaborative|adversarial|procedural|passionate|perfunctory|skeptical",
      "notable_moments": "Quote or paraphrase any particularly forceful, emotional, or politically significant statements"
    }},
    "intent_signals": {{
      "sponsor_intent": "What was the sponsor trying to achieve? Any backroom deals or compromises hinted at?",
      "opposition_intent": "What drove any opposition? Policy disagreement, political positioning, fiscal concerns?",
      "future_implications": "Did anyone signal future actions — amendments at BCC, legal challenges, community pushback?"
    }},
    "public_comment": "Summary of any public comment on this item, including tone and sentiment of speakers" or null,
    "vote_result": "Passed 9-3" or "Deferred" or "Withdrawn" or null,
    "amendments": "Brief description of any amendments, who proposed them, and whether they were accepted" or null,
    "concerns_raised": ["Specific concern 1", "Specific concern 2"],
    "consent_agenda": true/false
  }},
  ...
]

Guidelines:
- The chair often announces items by number ("Item 2A") or file number
- Listen for the clerk reading the item title
- "Consent agenda" items are typically passed as a group at the start — note which items were pulled from consent for separate discussion
- For EACH speaker, note whether they spoke FOR or AGAINST, and characterize their tone
- Capture specific dollar amounts, dates, deadlines, or conditions mentioned
- Note any references to prior meetings, ongoing disputes, or political context
- Flag if commissioners expressed concerns that could become legal issues
- If public commenters spoke, capture their sentiment and key points
- Note procedural actions: substitutions, amendments from the floor, points of order
- If an item's discussion is very brief (just reading title + vote), note that but still analyze the vote split
- If you cannot find a clear discussion for an item, set discussed=false
"""

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16384,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        # Extract JSON from response (may be wrapped in markdown)
        json_match = re.search(r'\[[\s\S]*\]', text)
        if json_match:
            segments = json.loads(json_match.group())
        else:
            log.warning("  Could not parse AI segmentation response")
            return {}

        # Convert to file_number -> data mapping
        result = {}
        for seg in segments:
            fn = seg.get("file_number", "")
            if fn:
                result[fn] = seg

        usage = response.usage
        log.info(f"  Transcript segmentation: {usage.input_tokens} in / {usage.output_tokens} out")
        return result

    except Exception as e:
        log.error(f"  Transcript segmentation failed: {e}")
        return {}


# ── Database integration ─────────────────────────────────────

def store_transcript_notes(meeting_id: int, segments: dict,
                           video_url: str, video_title: str):
    """Store per-item transcript summaries in the database.

    Appends to analyst_working_notes with a clear label:
      [Meeting Discussion — Committee Name 2026-04-15]
      Summary of what was discussed...
      Speakers: Commissioner X, Commissioner Y
      Vote: Passed 9-3
    """
    from db import get_db
    from repository import get_appearances_for_meeting, get_meeting_by_id
    from utils import now_iso

    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        log.error(f"  Meeting {meeting_id} not found")
        return 0

    body_name = meeting.get("body_name", "")
    meeting_date = meeting.get("meeting_date", "")
    appearances = get_appearances_for_meeting(meeting_id)
    if not appearances:
        log.info(f"  No appearances for meeting {meeting_id}")
        return 0

    updated = 0
    for app in appearances:
        fn = app.get("file_number", "")
        seg = segments.get(fn)
        if not seg:
            continue

        if not seg.get("discussed", False) and not seg.get("consent_agenda", False):
            continue

        # Build the note — rich format with sentiment/tone/intent
        parts = []
        label = f"[Meeting Discussion — {body_name} {meeting_date}]"
        parts.append(label)

        if seg.get("consent_agenda"):
            parts.append("Passed on consent agenda (no individual discussion).")
        elif seg.get("discussion_summary"):
            parts.append(seg["discussion_summary"])

        # Sentiment & Tone
        sentiment = seg.get("sentiment", {})
        tone = seg.get("tone_analysis", {})
        if sentiment.get("overall") or sentiment.get("description"):
            s_line = f"Sentiment: {sentiment.get('overall', '').upper()}"
            if sentiment.get("description"):
                s_line += f" — {sentiment['description']}"
            parts.append(s_line)
        if tone.get("tone"):
            t_line = f"Tone: {tone['tone']}"
            if tone.get("notable_moments"):
                t_line += f"\n  Notable: {tone['notable_moments']}"
            parts.append(t_line)

        # Speaker positions
        sp = seg.get("speaker_positions", {})
        if sp:
            pos_lines = ["Speaker Positions:"]
            for name, position in sp.items():
                pos_lines.append(f"  • {name}: {position}")
            parts.append("\n".join(pos_lines))
        elif seg.get("speakers"):
            parts.append(f"Speakers: {', '.join(seg['speakers'])}")

        # Intent signals
        intent = seg.get("intent_signals", {})
        if any(intent.get(k) for k in ["sponsor_intent", "opposition_intent", "future_implications"]):
            intent_lines = ["Intent & Implications:"]
            if intent.get("sponsor_intent"):
                intent_lines.append(f"  Sponsor: {intent['sponsor_intent']}")
            if intent.get("opposition_intent"):
                intent_lines.append(f"  Opposition: {intent['opposition_intent']}")
            if intent.get("future_implications"):
                intent_lines.append(f"  Looking Ahead: {intent['future_implications']}")
            parts.append("\n".join(intent_lines))

        # Concerns
        concerns = seg.get("concerns_raised", [])
        if concerns:
            parts.append("Concerns Raised:\n" + "\n".join(f"  • {c}" for c in concerns))

        # Public comment
        if seg.get("public_comment"):
            parts.append(f"Public Comment: {seg['public_comment']}")

        # Vote & amendments
        if seg.get("vote_result"):
            parts.append(f"Vote: {seg['vote_result']}")
        if seg.get("amendments"):
            parts.append(f"Amendments: {seg['amendments']}")

        # Video link with timestamp
        if seg.get("timestamp_start"):
            ts = seg["timestamp_start"]
            te = seg.get("timestamp_end", "")
            parts.append(f"Video: {video_url}&t={_ts_to_seconds(ts)}s ({ts}–{te})")

        note_block = "\n".join(parts)

        # Store in dedicated transcript_analysis column AND append to analyst notes
        with get_db() as conn:
            now = now_iso()

            # Store in the per-appearance transcript column
            conn.execute(
                """UPDATE appearances
                   SET transcript_analysis=?, transcript_video_url=?,
                       transcript_updated_at=?, updated_at=?
                   WHERE id=?""",
                (note_block, video_url, now, now, app["id"])
            )

            # Also append to analyst_working_notes for backward compat
            existing = conn.execute(
                "SELECT analyst_working_notes FROM appearances WHERE id=?",
                (app["id"],)
            ).fetchone()
            old = (existing["analyst_working_notes"] or "") if existing else ""

            # Don't duplicate if we already stored a transcript note for this meeting
            if label not in old:
                new_notes = (old + "\n\n" + note_block).strip() if old else note_block
                conn.execute(
                    "UPDATE appearances SET analyst_working_notes=? WHERE id=?",
                    (new_notes, app["id"])
                )

            updated += 1
            log.info(f"    Stored transcript notes for File# {fn}")

    # Video URL is returned in the result dict and stored per-item in transcript_analysis.
    # (meetings table has no notes column — no need to duplicate it there.)

    return updated


def _ts_to_seconds(ts: str) -> int:
    """Convert HH:MM:SS to total seconds for YouTube ?t= parameter."""
    parts = ts.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0


# ── High-level orchestrator ──────────────────────────────────

def backfill_transcript(meeting_id: int, output_dir: Path = None,
                        video_url: str = None,
                        raw_transcript: str = None,
                        emit=None) -> dict:
    """Full pipeline: find video → download transcript → segment → store.

    Args:
        meeting_id: Database meeting ID
        output_dir: Where to cache transcript files
        video_url: If provided, skip YouTube search and use this URL directly
        raw_transcript: If provided, skip both search AND download — use this text directly
        emit: Optional SSE callback for progress updates

    Returns:
        {"status": "ok"|"error", "video": {...}, "items_updated": int, ...}
    """
    from repository import get_meeting_by_id, get_appearances_for_meeting
    _emit = emit or (lambda *a, **k: None)

    meeting = get_meeting_by_id(meeting_id)
    if not meeting:
        return {"status": "error", "message": "Meeting not found"}

    body_name = meeting["body_name"]
    meeting_date = meeting["meeting_date"]

    appearances = get_appearances_for_meeting(meeting_id)
    if not appearances:
        return {"status": "error", "message": "No items for this meeting"}

    # ── Skip if this meeting already has transcript notes (unless forcing via raw paste)
    if not raw_transcript and not video_url:
        has_transcript = any(
            "[Meeting Discussion" in (a.get("analyst_working_notes") or "")
            for a in appearances
        )
        if has_transcript:
            return {"status": "skipped", "message": "already has transcript notes"}

    # ── Shortcut: if raw transcript was pasted, skip search + download
    if raw_transcript and raw_transcript.strip():
        _emit(f"Using pasted transcript ({len(raw_transcript):,} chars). Segmenting by item…",
              phase="transcript", pct=50)
        transcript = raw_transcript.strip()
        video_id = None
        video_title = "(pasted transcript)"
        final_url = video_url or ""
        transcript_len = len(transcript)

        items_for_ai = []
        for app in appearances:
            items_for_ai.append({
                "file_number": app.get("file_number", ""),
                "committee_item_number": app.get("committee_item_number", ""),
                "short_title": app.get("appearance_title", ""),
            })

        segments = segment_transcript_by_items(
            transcript, items_for_ai, body_name, meeting_date
        )
        if not segments:
            return {"status": "error", "message": "AI segmentation returned no results"}

        _emit(f"Segmented {len(segments)} items. Storing notes…",
              phase="transcript", pct=80)

        updated = store_transcript_notes(meeting_id, segments, final_url, video_title)

        _emit(f"Done — {updated} items updated with meeting discussion notes.",
              phase="transcript", pct=100)

        return {
            "status": "ok",
            "video_id": None,
            "video_title": video_title,
            "video_url": final_url,
            "transcript_length": transcript_len,
            "items_segmented": len(segments),
            "items_updated": updated,
        }

    _created_temp_dir = False
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp())
        _created_temp_dir = True

    transcript = None
    video_id = None
    video_title = ""
    final_url = video_url or ""
    source = "unknown"

    # ── Strategy 1: Granicus MP3 → Whisper (preferred — no IP blocks)
    if not video_url:
        _emit(f"Searching county archives for {body_name} {meeting_date}…",
              phase="transcript", pct=5)
        try:
            mp3_info = search_granicus_mp3(body_name, meeting_date)
            if mp3_info:
                _emit(f"Found recording — downloading MP3…",
                      phase="transcript", pct=10)
                mp3_path = download_mp3(mp3_info["mp3_url"], output_dir)
                if mp3_path:
                    _emit(f"Transcribing with Whisper ({mp3_path.stat().st_size / 1024 / 1024:.0f} MB)…",
                          phase="transcript", pct=20)
                    transcript = transcribe_with_whisper(mp3_path)
                    if transcript:
                        video_title = mp3_info.get("title", body_name)
                        final_url = mp3_info.get("mp3_url", "")
                        source = "granicus+whisper"
                        log.info(f"  Granicus+Whisper success: {len(transcript)} chars")
        except Exception as e:
            log.warning(f"  Granicus+Whisper path failed: {e}")

    # ── Strategy 2: YouTube captions (fallback — may fail from cloud IPs)
    if not transcript:
        _emit(f"Trying YouTube for {body_name} {meeting_date}…",
              phase="transcript", pct=30)

        if video_url:
            m = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', video_url)
            if m:
                video_id = m.group(1)
            else:
                video_id = video_url.split("/")[-1]
            video_title = f"(manually provided: {video_url})"
        else:
            candidates = search_youtube_videos(body_name, meeting_date)
            if candidates:
                best = candidates[0]
                if best["match_score"] >= 0.4:
                    video_id = best["video_id"]
                    video_title = best["title"]
                    final_url = best["url"]

        if video_id:
            _emit(f"Downloading YouTube captions…",
                  phase="transcript", pct=35)
            transcript = download_transcript(video_id, output_dir)
            if transcript:
                source = "youtube"

    if not transcript:
        # Cleanup temp files even on failure
        if _created_temp_dir:
            import shutil
            shutil.rmtree(output_dir, ignore_errors=True)
        return {
            "status": "skipped",
            "message": "No transcript available — no Granicus MP3 or YouTube captions found.",
            "video_id": video_id,
            "video_title": video_title,
            "video_url": final_url,
        }

    transcript_len = len(transcript)
    log.info(f"  Transcript downloaded: {transcript_len} chars")
    _emit(f"Transcript downloaded ({transcript_len:,} chars). Segmenting by item…",
          phase="transcript", pct=50)

    # Step 3: AI segmentation
    items_for_ai = []
    for app in appearances:
        items_for_ai.append({
            "file_number": app.get("file_number", ""),
            "committee_item_number": app.get("committee_item_number", ""),
            "short_title": app.get("appearance_title", ""),
        })

    segments = segment_transcript_by_items(
        transcript, items_for_ai, body_name, meeting_date
    )

    if not segments:
        if _created_temp_dir:
            import shutil
            shutil.rmtree(output_dir, ignore_errors=True)
        return {
            "status": "error",
            "message": "AI segmentation returned no results",
            "video_url": final_url,
            "transcript_length": transcript_len,
        }

    _emit(f"Segmented {len(segments)} items. Storing notes…",
          phase="transcript", pct=80)

    # Step 4: Store in database
    updated = store_transcript_notes(meeting_id, segments, final_url, video_title)

    _emit(f"Done — {updated} items updated with meeting discussion notes.",
          phase="transcript", pct=100)

    # Save raw transcript to disk for reference
    raw_path = output_dir / f"transcript_{meeting_id}_{video_id}.txt"
    try:
        raw_path.write_text(transcript, encoding="utf-8")
    except OSError:
        log.warning("Could not save raw transcript file (disk space?)")
        raw_path = None

    # ── Cleanup: delete MP3 and other large temp files to free disk ──
    try:
        for f in output_dir.glob("*.mp3"):
            f.unlink(missing_ok=True)
            log.info(f"  Cleaned up temp MP3: {f.name}")
        for f in output_dir.glob("*.wav"):
            f.unlink(missing_ok=True)
        if _created_temp_dir:
            import shutil
            shutil.rmtree(output_dir, ignore_errors=True)
            log.info(f"  Cleaned up temp dir: {output_dir}")
    except Exception as cleanup_err:
        log.warning(f"  Cleanup warning: {cleanup_err}")

    return {
        "status": "ok",
        "video_id": video_id,
        "video_title": video_title,
        "video_url": final_url,
        "transcript_length": transcript_len,
        "items_segmented": len(segments),
        "items_updated": updated,
        "transcript_file": str(raw_path) if raw_path else None,
    }
