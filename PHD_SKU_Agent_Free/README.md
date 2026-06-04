# PHD SKU Agent — Setup & Usage

## What it does
1. Takes a list of restaurant names (CSV or Google Sheet)
2. Searches each on Swiggy → falls back to Zomato automatically
3. Scrapes the full menu using a real Chrome browser (Playwright)
4. Sends menu text to an LLM which maps dishes to your 13 PHD premium SKUs
5. Outputs a colour-coded Excel + optionally writes back to your Google Sheet

---

## FREE LLM Options (pick one)

### Option 1 — Groq ✅ RECOMMENDED (free forever)
1. Sign up at https://console.groq.com (free, no credit card)
2. Create an API key
3. Add to `.env`:
```
GROQ_API_KEY=gsk_xxxxxxxxxxxx
```
Uses **Llama 3.1 70B** — 14,400 free requests/day. More than enough.

---

### Option 2 — Google Gemini (free forever)
1. Go to https://aistudio.google.com/app/apikey
2. Click "Create API Key" (free, sign in with Google)
3. Add to `.env`:
```
GEMINI_API_KEY=AIzaxxxxxxxxxxxxxxx
```
Uses **Gemini 1.5 Flash** — 1,500 free requests/day.

---

### Option 3 — Claude / Anthropic (paid, best quality)
```
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx
```
~₹0.25 per restaurant. Only use if you want highest accuracy.

> The agent auto-detects which key you've set. Priority: Groq → Gemini → Claude.
> You only need ONE key.

---

## Setup (one-time, ~5 minutes)

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Running the agent

### From a CSV file (simplest)
Your CSV needs a `Restaurant Name` column:
```
Restaurant Name
Shokudo Jayanagar
Yuki Cocktail Bar
Toast and Tonic
```

```bash
python sku_agent.py --csv my_restaurants.csv
```

### From a Google Sheet
**Step 1 — Create a service account:**
1. Go to https://console.cloud.google.com
2. New project → Enable "Google Sheets API" + "Google Drive API"
3. IAM & Admin → Service Accounts → Create → Download JSON key
4. Share your Google Sheet with the service account email (Editor access)

**Step 2 — Run:**
```bash
python sku_agent.py \
  --gsheet "https://docs.google.com/spreadsheets/d/YOUR_ID/edit" \
  --creds  service_account.json
```

Results appear in a new tab called **"SKU Results"** in your sheet.

---

## Optional flags
| Flag | Default | Purpose |
|------|---------|---------|
| `--provider groq` | auto | Force a specific LLM (groq/gemini/claude) |
| `--out results.xlsx` | `sku_results.xlsx` | Output filename |
| `--col "Name"` | `Restaurant Name` | Column with restaurant names |
| `--show-browser` | hidden | See Chrome scraping live (debug) |
| `--delay 5` | `3.0` | Seconds between restaurants |

---

## Output columns
| Column | What it means |
|--------|--------------|
| Lead Score | HOT / WARM / COLD |
| Lead Score Reason | Why the LLM gave that score |
| HIGH Confidence SKUs | Dish explicitly names the ingredient |
| MEDIUM Confidence SKUs | Cuisine type strongly implies it |
| LOW Confidence SKUs | Possible but indirect evidence |
| Evidence Dishes | Actual dish names that triggered each SKU |
| Dishes Scanned | How many menu items were analysed |
| Menu Source | Swiggy or Zomato |

---

## Troubleshooting
- **"Menu Found: No"** — restaurant not on Swiggy/Zomato, or name spelling differs
- **Scraping blocked** — run with `--show-browser` to debug; add `--delay 6`
- **JSON parse error** — Groq/Gemini returned malformed JSON; retry, it's rare
