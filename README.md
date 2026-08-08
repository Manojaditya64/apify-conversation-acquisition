# Apify conversation acquisition pipeline

Daily LinkedIn comment queue built around [harvestapi/linkedin-post-search](https://apify.com/harvestapi/linkedin-post-search).

**Flow:** Apify scout (keyword search) → merge local influencer JSON → Python hard filters → LLM intelligence → composite rank → thread enrich → 5 comment variants → CSV + HTML inbox.

Companion article: [How we built a daily LinkedIn comment queue for a B2B client](https://github.com/Manojaditya64/apify-conversation-acquisition) (Apify Content Program).

## Setup

```bash
cp .env.example .env
# Add APIFY_API_TOKEN and DEEPSEEK_API_KEY
```

No pip dependencies required for the core pipeline (stdlib + `urllib`). Uses DeepSeek HTTP API for intelligence and comments.

## Run

```bash
# Full V2 pipeline (scout + filter + LLM + comments)
python run-conversation-acquisition.py --max-posts 12

# Scout only, no LLM cost
python run-conversation-acquisition.py --scrape-only

# Re-rank from today's scrape without re-billing Apify
python run-conversation-acquisition.py --reuse-scrape

# Skip influencer feed merge
python run-conversation-acquisition.py --no-influencer-feed

# Build HTML inbox from latest queue CSV
python build-comment-inbox.py
```

## Influencer feed (optional)

Place daily scrape output at `data/influencer-feed/YYYY-MM-DD.json` (see `data/influencer-feed.example.json`). Scrape with [harvestapi/linkedin-profile-posts](https://apify.com/harvestapi/linkedin-profile-posts) in a separate morning job.

## Actors

| Role | Actor | Link |
|------|-------|------|
| Scout + thread enrich | [harvestapi/linkedin-post-search](https://apify.com/harvestapi/linkedin-post-search) | `buIWk2uOUzTmcLsuB` |
| Influencer feed (optional) | [harvestapi/linkedin-profile-posts](https://apify.com/harvestapi/linkedin-profile-posts) | Store Actor |

## Output

- `storage/conversation-acquisition/daily-queue-v2-YYYY-MM-DD.csv`
- `storage/conversation-acquisition/run-summary-v2-YYYY-MM-DD.json`
- `storage/conversation-acquisition/inbox.html` (after `build-comment-inbox.py`)

## Config

- `config/ranking-weights.json` — composite score weights
- `prompts/` — LLM system prompts (customize for your voice)

## License

ISC
