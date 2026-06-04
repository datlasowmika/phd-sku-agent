"""
PHD Premium SKU Agent
---------------------
Input  : Google Sheet (or local CSV) with restaurant names
Output : Same sheet updated with likely SKUs, evidence dishes, lead score

LLM providers supported (all free):
  - Groq       (default) : GROQ_API_KEY        — free forever, very fast
  - Gemini                : GEMINI_API_KEY      — free forever, 1500/day
  - Claude (Anthropic)   : ANTHROPIC_API_KEY   — paid, best quality

Requirements:
  pip install playwright groq google-generativeai anthropic gspread \
              google-auth google-auth-oauthlib pandas openpyxl python-dotenv
  playwright install chromium
"""

import os
import re
import json
import time
import asyncio
import random
import logging
from pathlib import Path
from dotenv import load_dotenv

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── optional Google Sheets support ──────────────────────────────────────────
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_OK = True
except ImportError:
    GSPREAD_OK = False

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("sku_agent")

# ── PHD Premium SKU catalogue ────────────────────────────────────────────────
PHD_SKUS = [
    "Avocado",
    "Blueberry",
    "Cherry Tomato",
    "Parsley",
    "Thai Asparagus",
    "Indian Asparagus",
    "Lemon Grass",
    "Thai Basil",
    "Italian Lemon",
    "Thai Bird Chilli",
    "Shiso Leaves",
    "Rosemary",
    "Shiitake Mushroom",
]

# ── Claude prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a procurement analyst for PHD, a premium fresh-produce supplier.
Your job: read a restaurant menu and identify which of PHD's premium SKUs the restaurant
is LIKELY to purchase, based on the dishes and descriptions you can see.

PHD Premium SKUs:
""" + "\n".join(f"- {s}" for s in PHD_SKUS) + """

Rules:
1. Only flag a SKU if there is genuine evidence in the menu (dish name, description, cuisine style).
2. For each matched SKU, list the specific dish(es) that imply it.
3. Score confidence: HIGH (dish explicitly names the ingredient), MEDIUM (cuisine strongly implies it),
   LOW (possible but indirect).
4. Give an overall lead score: HOT (3+ HIGH matches), WARM (1-2 HIGH or 3+ MEDIUM), COLD (LOW only or none).
5. Respond ONLY with a valid JSON object — no markdown, no preamble.

JSON schema:
{
  "lead_score": "HOT|WARM|COLD",
  "lead_score_reason": "one sentence",
  "matched_skus": [
    {
      "sku": "<SKU name>",
      "confidence": "HIGH|MEDIUM|LOW",
      "evidence_dishes": ["dish 1", "dish 2"]
    }
  ],
  "unmatched_skus": ["<SKU not found>"],
  "menu_cuisine_summary": "2-3 word cuisine type",
  "total_dishes_scanned": <integer>
}
"""


# ════════════════════════════════════════════════════════════════════════════
#  SCRAPER
# ════════════════════════════════════════════════════════════════════════════

class MenuScraper:
    """Playwright-based scraper that finds a restaurant on Swiggy/Zomato
    given only its name, then extracts the full menu text."""

    SWIGGY_SEARCH = "https://www.swiggy.com/search?query={query}"
    ZOMATO_SEARCH = "https://www.zomato.com/search?q={query}&location=Bangalore"

    def __init__(self, headless: bool = True):
        self.headless = headless

    async def _human_delay(self, lo=0.8, hi=2.2):
        await asyncio.sleep(random.uniform(lo, hi))

    async def _get_swiggy_menu(self, page, restaurant_name: str) -> str:
        """Search Swiggy and return menu text from the first matching result."""
        try:
            query = restaurant_name.replace(" ", "%20")
            await page.goto(
                f"https://www.swiggy.com/search?query={query}",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await self._human_delay()

            # Click first restaurant card
            card = page.locator("a[href*='/restaurants/']").first
            await card.wait_for(timeout=8000)
            href = await card.get_attribute("href")
            if not href:
                return ""

            url = f"https://www.swiggy.com{href}" if href.startswith("/") else href
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await self._human_delay(1.5, 3.0)

            # Collect all menu item text
            items = await page.locator("[class*='item-name'], [class*='ItemName'], [class*='dish-name'], [data-testid*='item']").all_text_contents()
            descs = await page.locator("[class*='item-desc'], [class*='ItemDesc'], [class*='dish-desc']").all_text_contents()
            all_text = "\n".join(items + descs)
            log.info(f"Swiggy: got {len(items)} items for '{restaurant_name}'")
            return all_text

        except PWTimeout:
            log.warning(f"Swiggy timeout for '{restaurant_name}'")
            return ""
        except Exception as e:
            log.warning(f"Swiggy error for '{restaurant_name}': {e}")
            return ""

    async def _get_zomato_menu(self, page, restaurant_name: str) -> str:
        """Search Zomato and return menu text from the first matching result."""
        try:
            query = restaurant_name.replace(" ", "+")
            await page.goto(
                f"https://www.zomato.com/search?q={query}&location=Bangalore",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await self._human_delay()

            # Click first restaurant result
            card = page.locator("a[href*='/bangalore/']").first
            await card.wait_for(timeout=8000)
            href = await card.get_attribute("href")
            if not href:
                return ""

            url = f"https://www.zomato.com{href}" if href.startswith("/") else href
            # Navigate to order/menu tab
            if "/order" not in url:
                url = url.rstrip("/") + "/order"

            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await self._human_delay(1.5, 3.0)

            # Scroll to load lazy menu sections
            for _ in range(4):
                await page.keyboard.press("End")
                await asyncio.sleep(0.8)

            items = await page.locator("[class*='sc-'][class*='Name'], h4, [class*='item-name']").all_text_contents()
            descs = await page.locator("p[class*='sc-'], [class*='item-desc']").all_text_contents()
            all_text = "\n".join(items + descs)
            log.info(f"Zomato: got {len(items)} items for '{restaurant_name}'")
            return all_text

        except PWTimeout:
            log.warning(f"Zomato timeout for '{restaurant_name}'")
            return ""
        except Exception as e:
            log.warning(f"Zomato error for '{restaurant_name}': {e}")
            return ""

    async def scrape(self, restaurant_name: str) -> dict:
        """Try Swiggy first, fall back to Zomato. Returns dict with menu text and source."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-IN",
            )
            page = await context.new_page()

            # Block images/fonts to speed up loading
            await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf}", lambda r: r.abort())

            menu_text = await self._get_swiggy_menu(page, restaurant_name)
            source = "Swiggy"

            if not menu_text or len(menu_text.strip()) < 50:
                log.info(f"Swiggy empty — trying Zomato for '{restaurant_name}'")
                menu_text = await self._get_zomato_menu(page, restaurant_name)
                source = "Zomato"

            await browser.close()

        return {
            "restaurant_name": restaurant_name,
            "menu_text": menu_text.strip(),
            "source": source,
            "menu_found": len(menu_text.strip()) > 50,
        }


# ════════════════════════════════════════════════════════════════════════════
#  SKU MATCHER  (Claude API)
# ════════════════════════════════════════════════════════════════════════════

class SKUMatcher:
    """
    Supports three LLM providers — auto-detected from environment variables.
    Priority: Groq → Gemini → Claude (Anthropic)
    Set the relevant API key in your .env file.
    """

    def __init__(self, provider: str = "auto"):
        self.provider = self._resolve_provider(provider)
        self.client   = self._build_client()
        log.info(f"LLM provider: {self.provider.upper()}")

    def _resolve_provider(self, provider: str) -> str:
        if provider != "auto":
            return provider.lower()
        if os.environ.get("GROQ_API_KEY"):
            return "groq"
        if os.environ.get("GEMINI_API_KEY"):
            return "gemini"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "claude"
        raise EnvironmentError(
            "No API key found. Set one of: GROQ_API_KEY, GEMINI_API_KEY, or ANTHROPIC_API_KEY in your .env file.\n"
            "  Groq (free):   https://console.groq.com\n"
            "  Gemini (free): https://aistudio.google.com/app/apikey"
        )

    def _build_client(self):
        if self.provider == "groq":
            from groq import Groq
            return Groq(api_key=os.environ["GROQ_API_KEY"])

        elif self.provider == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            return genai.GenerativeModel(
                model_name="gemini-1.5-flash",
                system_instruction=SYSTEM_PROMPT,
            )

        elif self.provider == "claude":
            import anthropic
            return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def _call_llm(self, user_msg: str) -> str:
        """Send prompt, return raw text response."""
        if self.provider == "groq":
            resp = self.client.chat.completions.create(
                model="llama-3.1-70b-versatile",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=1000,
                temperature=0.1,
            )
            return resp.choices[0].message.content.strip()

        elif self.provider == "gemini":
            resp = self.client.generate_content(user_msg)
            return resp.text.strip()

        elif self.provider == "claude":
            import anthropic
            resp = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            return resp.content[0].text.strip()

    def match(self, restaurant_name: str, menu_text: str) -> dict:
        if not menu_text:
            return {
                "lead_score": "COLD",
                "lead_score_reason": "No menu data found",
                "matched_skus": [],
                "unmatched_skus": PHD_SKUS.copy(),
                "menu_cuisine_summary": "Unknown",
                "total_dishes_scanned": 0,
                "error": "no_menu",
            }

        user_msg = f"""Restaurant: {restaurant_name}

--- MENU TEXT ---
{menu_text[:6000]}
--- END MENU ---

Analyse this menu and return the JSON."""

        try:
            raw = self._call_llm(user_msg)
            raw = re.sub(r"^```json\s*|^```\s*|```$", "", raw, flags=re.MULTILINE).strip()
            return json.loads(raw)

        except json.JSONDecodeError as e:
            log.error(f"JSON parse error for {restaurant_name}: {e}")
            return {"error": "json_parse_error", "lead_score": "COLD", "matched_skus": [], "unmatched_skus": PHD_SKUS.copy()}
        except Exception as e:
            log.error(f"LLM API error for {restaurant_name}: {e}")
            return {"error": str(e), "lead_score": "COLD", "matched_skus": [], "unmatched_skus": PHD_SKUS.copy()}


# ════════════════════════════════════════════════════════════════════════════
#  GOOGLE SHEETS  I/O
# ════════════════════════════════════════════════════════════════════════════

def load_from_gsheet(sheet_url: str, creds_file: str) -> pd.DataFrame:
    """Read restaurant names from a Google Sheet. Returns a DataFrame."""
    if not GSPREAD_OK:
        raise RuntimeError("gspread not installed. Run: pip install gspread google-auth")
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(sheet_url)
    ws = sh.get_worksheet(0)
    data = ws.get_all_records()
    return pd.DataFrame(data)


def write_to_gsheet(df: pd.DataFrame, sheet_url: str, creds_file: str, output_tab: str = "SKU Results"):
    """Write results to a new tab in the same Google Sheet."""
    if not GSPREAD_OK:
        raise RuntimeError("gspread not installed.")
    scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(sheet_url)

    try:
        ws = sh.worksheet(output_tab)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=output_tab, rows=len(df) + 2, cols=len(df.columns) + 2)

    ws.update([df.columns.tolist()] + df.values.tolist())
    log.info(f"Results written to Google Sheet tab: '{output_tab}'")


# ════════════════════════════════════════════════════════════════════════════
#  RESULT FLATTENER
# ════════════════════════════════════════════════════════════════════════════

def flatten_result(restaurant_name: str, scrape: dict, match: dict) -> dict:
    """Convert raw dicts into flat columns suitable for a spreadsheet."""
    matched = match.get("matched_skus", [])
    high    = [m["sku"] for m in matched if m.get("confidence") == "HIGH"]
    medium  = [m["sku"] for m in matched if m.get("confidence") == "MEDIUM"]
    low     = [m["sku"] for m in matched if m.get("confidence") == "LOW"]

    # Build evidence string: "Avocado (HIGH): Avo Toast, Guac Bowl | Rosemary (MEDIUM): Focaccia"
    evidence_parts = []
    for m in matched:
        dishes = ", ".join(m.get("evidence_dishes", []))
        evidence_parts.append(f"{m['sku']} ({m['confidence']}): {dishes}")
    evidence_str = " | ".join(evidence_parts)

    return {
        "Restaurant Name":      restaurant_name,
        "Menu Source":          scrape.get("source", "—"),
        "Menu Found":           "Yes" if scrape.get("menu_found") else "No",
        "Cuisine Type":         match.get("menu_cuisine_summary", "—"),
        "Lead Score":           match.get("lead_score", "COLD"),
        "Lead Score Reason":    match.get("lead_score_reason", "—"),
        "HIGH Confidence SKUs": ", ".join(high) if high else "—",
        "MEDIUM Confidence SKUs": ", ".join(medium) if medium else "—",
        "LOW Confidence SKUs":  ", ".join(low) if low else "—",
        "All Matched SKUs":     ", ".join([m["sku"] for m in matched]) if matched else "—",
        "Evidence Dishes":      evidence_str if evidence_str else "—",
        "Dishes Scanned":       match.get("total_dishes_scanned", 0),
        "Unmatched SKUs":       ", ".join(match.get("unmatched_skus", [])),
        "Error":                match.get("error", ""),
    }


# ════════════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ════════════════════════════════════════════════════════════════════════════

async def run_pipeline(
    input_source,
    output_xlsx: str = "sku_results.xlsx",
    gsheet_url: str = None,
    gsheet_creds: str = None,
    restaurant_col: str = "Restaurant Name",
    headless: bool = True,
    delay_between: float = 3.0,
    provider: str = "auto",
):
    # ── Load input ──────────────────────────────────────────────────────────
    if isinstance(input_source, pd.DataFrame):
        df_in = input_source
    elif isinstance(input_source, str) and input_source.endswith(".csv"):
        df_in = pd.read_csv(input_source)
    elif isinstance(input_source, str) and "docs.google.com" in input_source:
        df_in = load_from_gsheet(input_source, gsheet_creds)
    else:
        raise ValueError("input_source must be a DataFrame, CSV path, or Google Sheet URL")

    if restaurant_col not in df_in.columns:
        raise ValueError(f"Column '{restaurant_col}' not found. Available: {list(df_in.columns)}")

    restaurants = df_in[restaurant_col].dropna().tolist()
    log.info(f"Processing {len(restaurants)} restaurants...")

    scraper = MenuScraper(headless=headless)
    matcher = SKUMatcher(provider=provider)
    rows = []

    for i, name in enumerate(restaurants, 1):
        log.info(f"[{i}/{len(restaurants)}] {name}")

        # Scrape
        scrape_result = await scraper.scrape(name)

        # Match SKUs
        match_result = matcher.match(name, scrape_result["menu_text"])

        # Flatten
        row = flatten_result(name, scrape_result, match_result)
        rows.append(row)

        log.info(
            f"  -> {row['Lead Score']} | "
            f"HIGH: {row['HIGH Confidence SKUs']} | "
            f"MEDIUM: {row['MEDIUM Confidence SKUs']}"
        )

        # Polite delay between restaurants to avoid rate-limiting
        if i < len(restaurants):
            await asyncio.sleep(delay_between + random.uniform(0, 1.5))

    df_out = pd.DataFrame(rows)

    # ── Write XLSX ───────────────────────────────────────────────────────────
    _write_styled_xlsx(df_out, output_xlsx)
    log.info(f"Results saved to: {output_xlsx}")

    # ── Write back to Google Sheet ───────────────────────────────────────────
    if gsheet_url and gsheet_creds:
        write_to_gsheet(df_out, gsheet_url, gsheet_creds)

    return df_out


def _write_styled_xlsx(df: pd.DataFrame, path: str):
    """Write a colour-coded Excel file with lead score highlighting."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "SKU Results"

    COLORS = {
        "HOT":  ("FF4757", "FFFFFF"),
        "WARM": ("FFA502", "FFFFFF"),
        "COLD": ("70A1FF", "FFFFFF"),
    }
    HDR_BG = "1A1A2E"
    ALT    = "F4F6FF"
    thin   = Side(style="thin", color="D0D0D0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Header
    for col_i, col_name in enumerate(df.columns, 1):
        c = ws.cell(row=1, column=col_i, value=col_name)
        c.font      = Font(name="Arial", bold=True, color="FFFFFF", size=10)
        c.fill      = PatternFill("solid", start_color=HDR_BG)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border    = border
    ws.row_dimensions[1].height = 28

    # Data rows
    for row_i, row in df.iterrows():
        r = row_i + 2
        for col_i, col_name in enumerate(df.columns, 1):
            val = row[col_name]
            c   = ws.cell(row=r, column=col_i, value=val)
            bg  = ALT if row_i % 2 == 0 else "FFFFFF"
            c.fill      = PatternFill("solid", start_color=bg)
            c.font      = Font(name="Arial", size=9)
            c.alignment = Alignment(vertical="center", wrap_text=True)
            c.border    = border

            # Colour lead score cell
            if col_name == "Lead Score" and val in COLORS:
                bg_hex, fg_hex = COLORS[val]
                c.fill = PatternFill("solid", start_color=bg_hex)
                c.font = Font(name="Arial", size=9, bold=True, color=fg_hex)
                c.alignment = Alignment(horizontal="center", vertical="center")

        ws.row_dimensions[r].height = 36

    # Column widths
    widths = {
        "Restaurant Name": 28, "Menu Source": 12, "Menu Found": 10,
        "Cuisine Type": 18, "Lead Score": 12, "Lead Score Reason": 40,
        "HIGH Confidence SKUs": 28, "MEDIUM Confidence SKUs": 28,
        "LOW Confidence SKUs": 28, "All Matched SKUs": 32,
        "Evidence Dishes": 60, "Dishes Scanned": 14,
        "Unmatched SKUs": 40, "Error": 20,
    }
    for col_i, col_name in enumerate(df.columns, 1):
        ws.column_dimensions[ws.cell(1, col_i).column_letter].width = widths.get(col_name, 18)

    wb.save(path)


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    """
    Usage examples
    ──────────────
    # From a local CSV (must have a 'Restaurant Name' column):
        python sku_agent.py --csv restaurants.csv

    # From a Google Sheet:
        python sku_agent.py --gsheet "https://docs.google.com/..." --creds service_account.json

    # Show browser window (useful for debugging):
        python sku_agent.py --csv restaurants.csv --show-browser
    """

    import argparse
    parser = argparse.ArgumentParser(description="PHD SKU Agent")
    parser.add_argument("--csv",          help="Path to input CSV file")
    parser.add_argument("--gsheet",       help="Google Sheet URL (input)")
    parser.add_argument("--creds",        help="Path to Google service account JSON")
    parser.add_argument("--out",          default="sku_results.xlsx", help="Output Excel file")
    parser.add_argument("--col",          default="Restaurant Name",  help="Column name containing restaurant names")
    parser.add_argument("--show-browser", action="store_true",        help="Run browser in visible mode (debug)")
    parser.add_argument("--delay",        type=float, default=3.0,    help="Seconds to wait between restaurants")
    args = parser.parse_args()

    if not args.csv and not args.gsheet:
        # Demo mode: run on 3 sample restaurants
        log.info("No input given — running in DEMO mode on 3 sample restaurants")
        demo_df = pd.DataFrame({
            "Restaurant Name": ["Shokudo Jayanagar", "Yuki Cocktail Bar", "Toast and Tonic"]
        })
        asyncio.run(run_pipeline(
            input_source=demo_df,
            output_xlsx=args.out,
            headless=not args.show_browser,
            delay_between=args.delay,
        ))
    elif args.csv:
        asyncio.run(run_pipeline(
            input_source=args.csv,
            output_xlsx=args.out,
            restaurant_col=args.col,
            headless=not args.show_browser,
            delay_between=args.delay,
        ))
    else:
        if not args.creds:
            sys.exit("--creds required when using --gsheet")
        asyncio.run(run_pipeline(
            input_source=args.gsheet,
            output_xlsx=args.out,
            gsheet_url=args.gsheet,
            gsheet_creds=args.creds,
            restaurant_col=args.col,
            headless=not args.show_browser,
            delay_between=args.delay,
        ))
