#!/usr/bin/env python3
"""
Sales Follow-up Email Agent - Single Deal Test Version

A streamlined version that processes a single deal for testing purposes.

Usage:
    python test_single_deal.py
    python test_single_deal.py "Deal Name"  # Optional: specify a different deal name

This script is identical to followup_agent.py but only processes one deal
to make testing faster.
"""

import os
import sys
import json
import requests
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse
import anthropic
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
HUBSPOT_BASE_URL = "https://api.hubapi.com"

# Knowledge base paths
CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(__file__), ".chroma")
COLLECTION_NAME = "adopt_ai_knowledge"

# Default test deal - change this or pass as command line argument
TEST_DEAL_NAME = os.getenv("TEST_DEAL_NAME", "Test Deal")

# Deal stages to monitor (comma-separated in env var, REQUIRED)
TARGET_STAGES = [
    stage.strip() 
    for stage in os.getenv("TARGET_STAGES", "").split(",") 
    if stage.strip()
]
if not TARGET_STAGES:
    raise ValueError(
        "TARGET_STAGES environment variable is required.\n"
        "Set it to a comma-separated list of HubSpot deal stage IDs.\n"
        "Find stage IDs in HubSpot: Settings → Objects → Deals → Pipelines → click on a stage to see its ID."
    )

# Slack channels to search for internal context (comma-separated in env var)
DEFAULT_SLACK_CHANNELS = "sales,marketing"
SLACK_CHANNELS = [
    channel.strip() 
    for channel in os.getenv("SLACK_CHANNELS", DEFAULT_SLACK_CHANNELS).split(",") 
    if channel.strip()
]


class HubSpotClient:
    """Client for HubSpot API interactions."""
    
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
    
    def search_deals_by_name(self, deal_name: str, properties: list[str]) -> list[dict]:
        """Search for deals by name."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/deals/search"
        
        payload = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "dealname",
                    "operator": "CONTAINS_TOKEN",
                    "value": deal_name.split(" - ")[0]  # Search by first part of name
                }]
            }],
            "properties": properties,
            "limit": 10
        }
        
        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()
        data = response.json()
        
        # Filter to exact match if possible
        results = data.get("results", [])
        exact_matches = [d for d in results if d["properties"].get("dealname") == deal_name]
        
        return exact_matches if exact_matches else results[:1]
    
    def get_associated_contacts(self, deal_id: str) -> list[dict]:
        """Get contacts associated with a deal."""
        url = f"{HUBSPOT_BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/contacts"
        
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        
        associations = response.json().get("results", [])
        if not associations:
            return []
        
        contact_ids = [a.get("toObjectId") or a.get("id") for a in associations]
        
        contacts_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/batch/read"
        contacts_payload = {
            "inputs": [{"id": cid} for cid in contact_ids],
            "properties": ["email", "firstname", "lastname", "jobtitle", "company", 
                          "notes_last_contacted", "hs_sales_email_last_replied"]
        }
        
        contacts_response = requests.post(contacts_url, headers=self.headers, json=contacts_payload)
        contacts_response.raise_for_status()
        
        return contacts_response.json().get("results", [])
    
    def get_associated_company(self, deal_id: str) -> Optional[dict]:
        """Get the company associated with a deal."""
        url = f"{HUBSPOT_BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/companies"
        
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        
        associations = response.json().get("results", [])
        if not associations:
            return None
        
        company_id = associations[0].get("toObjectId") or associations[0].get("id")
        
        company_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/companies/{company_id}"
        company_response = requests.get(
            company_url, 
            headers=self.headers,
            params={"properties": "name,industry,numberofemployees,description,website"}
        )
        company_response.raise_for_status()
        
        return company_response.json()
    
    def get_deal_emails(self, deal_id: str, limit: int = 50) -> list[dict]:
        """Get emails associated with a deal.
        
        Fetches all email associations (with pagination) and returns the most recent ones.
        """
        return self._get_object_emails("deals", deal_id, limit)
    
    def get_company_emails(self, company_id: str, limit: int = 50) -> list[dict]:
        """Get emails associated with a company.
        
        Fetches all email associations (with pagination) and returns the most recent ones.
        """
        return self._get_object_emails("companies", company_id, limit)
    
    def _get_object_emails(self, object_type: str, object_id: str, limit: int = 50) -> list[dict]:
        """Get emails associated with any CRM object, with pagination support."""
        all_email_ids = []
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/{object_type}/{object_id}/associations/emails"
        
        # Paginate through all associations
        while url:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            
            data = response.json()
            associations = data.get("results", [])
            
            for a in associations:
                email_id = a.get("toObjectId") or a.get("id")
                if email_id:
                    all_email_ids.append(email_id)
            
            # Check for next page
            paging = data.get("paging", {})
            next_link = paging.get("next", {}).get("link")
            url = next_link if next_link else None
        
        if not all_email_ids:
            return []
        
        # Fetch all emails and sort by date to get most recent
        emails = self._fetch_emails_by_ids(all_email_ids)
        
        # Sort by timestamp (most recent first) and return limited results
        def get_email_timestamp(email):
            props = email.get("properties", {})
            ts = props.get("hs_timestamp") or props.get("hs_createdate") or "0"
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    return 0
            return ts / 1000 if ts else 0
        
        emails.sort(key=get_email_timestamp, reverse=True)
        return emails[:limit]
    
    def _fetch_emails_by_ids(self, email_ids: list[str]) -> list[dict]:
        """Fetch email details by IDs.
        
        Handles batching for large numbers of emails (HubSpot limit is 100 per batch).
        """
        if not email_ids:
            return []
        
        # Deduplicate email IDs
        unique_ids = list(set(email_ids))
        
        all_emails = []
        emails_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/emails/batch/read"
        
        # Process in batches of 100 (HubSpot limit)
        batch_size = 100
        for i in range(0, len(unique_ids), batch_size):
            batch_ids = unique_ids[i:i + batch_size]
            
            emails_payload = {
                "inputs": [{"id": eid} for eid in batch_ids],
                "properties": ["hs_email_subject", "hs_email_status", "hs_email_direction",
                              "hs_timestamp", "hs_email_text", "hs_createdate"]
            }
            
            emails_response = requests.post(emails_url, headers=self.headers, json=emails_payload)
            emails_response.raise_for_status()
            
            all_emails.extend(emails_response.json().get("results", []))
        
        return all_emails
    
    def get_deal_notes(self, deal_id: str, limit: int = 5) -> list[dict]:
        """Get notes associated with a deal."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/deals/{deal_id}/associations/notes"
        
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        
        associations = response.json().get("results", [])
        if not associations:
            return []
        
        note_ids = [a.get("toObjectId") or a.get("id") for a in associations[:limit]]
        
        notes_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes/batch/read"
        notes_payload = {
            "inputs": [{"id": nid} for nid in note_ids],
            "properties": ["hs_note_body", "hs_timestamp", "hs_createdate"]
        }
        
        notes_response = requests.post(notes_url, headers=self.headers, json=notes_payload)
        notes_response.raise_for_status()
        
        return notes_response.json().get("results", [])


class SlackClient:
    """Client for Slack API interactions."""
    
    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json"
        }
        self.base_url = "https://slack.com/api"
    
    def search_messages(self, query: str, channels: list[str], limit: int = 10) -> list[dict]:
        """Search for messages in specified channels."""
        channel_filter = " ".join([f"in:#{ch}" for ch in channels])
        full_query = f"{query} {channel_filter}"
        
        url = f"{self.base_url}/search.messages"
        params = {
            "query": full_query,
            "count": limit,
            "sort": "timestamp",
            "sort_dir": "desc"
        }
        
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        if not data.get("ok"):
            error = data.get("error", "Unknown error")
            print(f"   ⚠️ Slack search error: {error}")
            return []
        
        messages = data.get("messages", {}).get("matches", [])
        
        formatted = []
        for msg in messages[:limit]:
            formatted.append({
                "text": msg.get("text", "")[:500],
                "user": msg.get("username", msg.get("user", "Unknown")),
                "channel": msg.get("channel", {}).get("name", "unknown"),
                "timestamp": msg.get("ts", ""),
                "permalink": msg.get("permalink", "")
            })
        
        return formatted
    
    def format_slack_context(self, messages: list[dict]) -> str:
        """Format Slack messages into a readable context string."""
        if not messages:
            return "No relevant Slack discussions found."
        
        formatted_messages = []
        for msg in messages:
            try:
                ts = float(msg["timestamp"])
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                date_str = "Unknown date"
            
            formatted_messages.append(
                f"- [{date_str}] #{msg['channel']} - @{msg['user']}: {msg['text']}"
            )
        
        return "\n".join(formatted_messages)


def extract_domain(url: str) -> str:
    """Extract domain from URL (e.g. https://www.acme.com/path -> acme.com)."""
    if not url or not str(url).strip():
        return ""
    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or ""
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.lower() if netloc else ""
    except Exception:
        return ""


class FirefliesClient:
    """Client for Fireflies.ai API interactions."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.fireflies.ai/graphql"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
    
    def search_transcripts_by_title(self, search_term: str, limit: int = 5) -> list[dict]:
        """Search for meeting transcripts by title (account/company name)."""
        query = """
        query TranscriptsByTitle($title: String!, $limit: Int) {
            transcripts(title: $title, limit: $limit) {
                id
                title
                date
                duration
                summary {
                    overview
                    action_items
                    keywords
                }
            }
        }
        """
        
        variables = {
            "title": search_term,
            "limit": limit
        }
        
        try:
            response = requests.post(
                self.base_url,
                headers=self.headers,
                json={"query": query, "variables": variables}
            )
            response.raise_for_status()
            
            data = response.json()
            
            if "errors" in data:
                print(f"   ⚠️ Fireflies API error: {data['errors']}")
                return []
            
            return data.get("data", {}).get("transcripts", []) or []
            
        except requests.exceptions.RequestException as e:
            print(f"   ⚠️ Fireflies request error: {e}")
            return []
    
    def format_fireflies_context(self, transcripts: list[dict]) -> str:
        """Format Fireflies transcripts into a readable context string."""
        if not transcripts:
            return "No call transcripts found for this contact."
        
        formatted_transcripts = []
        
        for transcript in transcripts:
            # Parse date - Fireflies can return date as string or timestamp
            date_val = transcript.get("date", "Unknown date")
            date_str = "Unknown date"
            if date_val:
                try:
                    if isinstance(date_val, (int, float)):
                        # Unix timestamp (seconds or milliseconds)
                        ts = date_val / 1000 if date_val > 1e12 else date_val
                        dt = datetime.fromtimestamp(ts)
                        date_str = dt.strftime("%Y-%m-%d")
                    elif isinstance(date_val, str):
                        # ISO format string
                        dt = datetime.fromisoformat(date_val.replace("Z", "+00:00"))
                        date_str = dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError, OSError):
                    date_str = str(date_val) if date_val else "Unknown date"
            
            title = transcript.get("title", "Untitled Meeting")
            duration = transcript.get("duration", 0)
            duration_mins = round(duration / 60) if duration else 0
            
            summary = transcript.get("summary", {}) or {}
            overview = summary.get("overview", "No summary available")
            action_items = summary.get("action_items", []) or []
            keywords = summary.get("keywords", []) or []
            
            transcript_text = f"📞 **{title}** ({date_str}, {duration_mins} mins)\n"
            transcript_text += f"   Summary: {overview[:500]}...\n" if len(overview) > 500 else f"   Summary: {overview}\n"
            
            if action_items:
                transcript_text += f"   Action Items: {', '.join(action_items[:5])}\n"
            
            if keywords:
                transcript_text += f"   Keywords: {', '.join(keywords[:10])}\n"
            
            formatted_transcripts.append(transcript_text)
        
        return "\n".join(formatted_transcripts)


class KnowledgeBaseClient:
    """Client for querying the Adopt AI knowledge base via ChromaDB."""
    
    def __init__(self):
        self.client = None
        self.collection = None
        self._initialized = False
        
    def initialize(self) -> bool:
        """Initialize ChromaDB connection. Returns True if successful."""
        if self._initialized:
            return True
            
        try:
            import chromadb
            
            if not os.path.exists(CHROMA_PERSIST_DIR):
                print("   ⚠️ Knowledge base not found. Run 'python index_knowledge_base.py' first.")
                return False
            
            self.client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
            self.collection = self.client.get_collection(COLLECTION_NAME)
            self._initialized = True
            return True
            
        except ImportError:
            print("   ⚠️ ChromaDB not installed. Run 'pip install chromadb'")
            return False
        except ValueError as e:
            print(f"   ⚠️ Knowledge base collection not found: {e}")
            print("   Run 'python index_knowledge_base.py' to create it.")
            return False
        except Exception as e:
            print(f"   ⚠️ Error initializing knowledge base: {e}")
            return False
    
    def search(self, query: str, n_results: int = 5) -> list[dict]:
        """Search the knowledge base for relevant content."""
        if not self._initialized and not self.initialize():
            return []
        
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results
            )
            
            if not results["documents"] or not results["documents"][0]:
                return []
            
            formatted_results = []
            for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                formatted_results.append({
                    "content": doc,
                    "source": meta.get("source", "unknown"),
                    "category": meta.get("category", "general"),
                })
            
            return formatted_results
            
        except Exception as e:
            print(f"   ⚠️ Knowledge base search error: {e}")
            return []
    
    def get_context_for_deal(self, deal_context: dict) -> str:
        """Build a comprehensive knowledge base context for a specific deal."""
        if not self._initialized and not self.initialize():
            return "Knowledge base not available."
        
        all_results = []
        
        # Search by industry
        industry = deal_context.get("company_industry", "")
        if industry and industry != "Unknown":
            results = self.search(f"{industry} industry use case capabilities", n_results=3)
            all_results.extend(results)
        
        # Search by job title/persona
        job_title = deal_context.get("contact_title", "")
        if job_title and job_title != "Unknown":
            results = self.search(f"{job_title} persona messaging value proposition", n_results=3)
            all_results.extend(results)
        
        # Search based on notes/context for specific problems
        notes = deal_context.get("notes", "")
        if notes and notes != "No notes available":
            results = self.search(f"solution for {notes[:200]}", n_results=2)
            all_results.extend(results)
        
        # General capabilities search
        results = self.search("Adopt AI capabilities features platform", n_results=2)
        all_results.extend(results)
        
        # Deduplicate and format
        seen_content = set()
        unique_results = []
        for r in all_results:
            content_hash = hash(r["content"][:100])
            if content_hash not in seen_content:
                seen_content.add(content_hash)
                unique_results.append(r)
        
        return self.format_kb_context(unique_results[:8])
    
    def format_kb_context(self, results: list[dict]) -> str:
        """Format knowledge base results into a context string for the prompt."""
        if not results:
            return "No relevant knowledge base content found."
        
        formatted = []
        for r in results:
            source = r.get("source", "unknown")
            category = r.get("category", "general")
            content = r.get("content", "")[:800]
            
            formatted.append(f"[{category}/{source}]\n{content}")
        
        return "\n\n---\n\n".join(formatted)


def get_last_sent_email_date(emails: list[dict], verbose: bool = False) -> Optional[datetime]:
    """Extract the most recent SENT email date from a list of emails.
    
    Checks multiple HubSpot email properties to identify outbound emails:
    - hs_email_status: SENT
    - hs_email_direction: EMAIL (outbound) vs INCOMING_EMAIL (inbound)
    """
    sent_emails = []
    
    for email in emails:
        props = email.get("properties", {})
        status = props.get("hs_email_status", "")
        direction = props.get("hs_email_direction", "")
        subject = props.get("hs_email_subject", "")[:50] if props.get("hs_email_subject") else "No subject"
        
        # Look for outbound sent emails
        # HubSpot uses "EMAIL" for outbound and "INCOMING_EMAIL" for inbound
        is_sent = (
            status == "SENT" or 
            direction == "EMAIL" or 
            direction == "OUTGOING_EMAIL" or
            (status and status != "BOUNCED" and direction != "INCOMING_EMAIL")
        )
        
        if verbose:
            print(f"      📧 [{status}/{direction}] {subject}")
        
        if is_sent:
            timestamp = props.get("hs_timestamp") or props.get("hs_createdate")
            if timestamp:
                try:
                    if isinstance(timestamp, str):
                        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromtimestamp(timestamp / 1000)
                    sent_emails.append(dt)
                except (ValueError, TypeError):
                    continue
    
    return max(sent_emails) if sent_emails else None


def search_company_news(client: anthropic.Anthropic, company_name: str) -> str:
    """Use Claude's web search to find relevant news about the company."""
    if not company_name or company_name == "Unknown Company":
        return "No company name available for web search."
    
    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3
            }],
            messages=[{
                "role": "user",
                "content": f"""Search for recent news about "{company_name}" related to:
- AI initiatives or investments
- New AI/ML leadership hires (CTO, CDO, VP of AI, etc.)
- Digital transformation projects
- Technology partnerships or vendor selections
- Recent funding, acquisitions, or growth announcements
- Strategic initiatives that might benefit from AI agents

Return a brief, bullet-pointed summary of the most relevant findings for a B2B sales context. 
Focus on information that would be useful for selling an AI agent platform.
If no relevant news is found, say so briefly."""
            }]
        )
        
        result_text = ""
        for block in response.content:
            if hasattr(block, 'text'):
                result_text += block.text + "\n"
        
        if not result_text.strip() and response.stop_reason == "tool_use":
            for block in response.content:
                if hasattr(block, 'type') and block.type == "tool_use":
                    pass
        
        return result_text.strip() if result_text else "No relevant news found."
        
    except anthropic.BadRequestError as e:
        return f"Web search not available: Please enable web search in your Anthropic Console. Error: {str(e)}"
    except anthropic.APIError as e:
        return f"Web search API error: {str(e)}"
    except Exception as e:
        return f"Web search unavailable: {str(e)}"


def generate_followup_email(client: anthropic.Anthropic, deal_context: dict) -> dict:
    """Use Claude to generate a personalized follow-up email."""
    
    days_since = deal_context['days_since_contact']
    is_very_cold = isinstance(days_since, int) and days_since > 180
    
    prompt = f"""## Role & Purpose

You are an AI sales assistant for Adopt AI, specializing in generating personalized, context-rich follow-up emails for sales opportunities. Your goal is to help the sales team re-engage prospects by connecting their specific problems to new product capabilities or relevant success stories from similar customers.

## Deal Context (from HubSpot CRM)

**Deal Name:** {deal_context['deal_name']}
**Stage:** {deal_context['stage']}
**Days Since Last Contact:** {deal_context['days_since_contact']}

**Contact:**
- Name: {deal_context['contact_name']}
- Title: {deal_context['contact_title']}
- Email: {deal_context['contact_email']}

**Company:**
- Name: {deal_context['company_name']}
- Industry: {deal_context['company_industry']}
- Size: {deal_context['company_size']}

**Recent Notes (may contain problems, blockers, or objections):**
{deal_context['notes']}

**Last Email Subject:**
{deal_context['last_email_subject']}

**Internal Slack Discussions (from #sales, #marketing, #designpartners):**
{deal_context.get('slack_context', 'No Slack context available.')}

**Call Recording Transcripts (from Fireflies):**
{deal_context.get('fireflies_context', 'No call transcripts available.')}

**Recent Company News & Intelligence (from web search):**
{deal_context.get('web_research', 'No web research available.')}

## Adopt AI Knowledge Base (Relevant Context)

The following content was retrieved from our knowledge base based on this deal's industry, persona, and context. Use this to craft a more personalized and relevant email:

{deal_context.get('knowledge_base_context', 'No knowledge base content available.')}

## Your Task

### Step 1: Analyze the Context
From the notes, Slack discussions, call transcripts, web research, and deal information above, identify:
1. **Problems/Blockers**: What did they need that we couldn't deliver? Why did the deal stall? Look for "we can't do that yet" moments in Slack and specific pain points mentioned in calls.
2. **Call Insights**: What specific problems, feature requests, or objections were mentioned in call recordings? What business outcomes were they hoping to achieve?
3. **New Capabilities That Apply**: What's changed in our product that addresses their needs?
4. **Internal Insights**: What did the team discuss internally about this deal? Any technical limitations or product feedback mentioned?
5. **Web Intelligence**: Any recent AI initiatives, leadership hires, or strategic moves that create an opening for Adopt?
6. **Similar Deal Insights**: What use cases or outcomes from comparable customers might resonate?

### Step 2: Determine Email Approach

**Scenario A - They had a specific unmet need:**
- Lead with: "When we last spoke, you mentioned needing [X]. Wanted to share that we now [capability]."
- Focus on how the new feature solves their exact problem

**Scenario B - Deal went cold without clear blocker:**
- Lead with: "Circling back—I came across [relevant use case/outcome] that reminded me of your goals around [X]."
- Share a brief success story or business outcome from similar customers

### Step 3: Generate the Email

**Email Structure (150-200 words MAX):**
1. **Subject Line**: Reference their specific problem or use case
2. **Opening**: Brief context reconnection—ONE sentence referencing the specific problem or blocker
3. **The "Now We Can" Moment**: This is the core. Explain what's changed:
   - If they had a specific problem → show how new capabilities address it
   - If deal just went cold → share a relevant use case or business outcome
4. **Simple CTA**: One clear ask (suggest a call if deal has been cold 6+ months)

**Tone Guidelines:**
- Helpful, not salesy
- Show you remember their specific situation
- Focus on THEIR problems, not our features
- Concise and respectful of their time
- The email is about THEM, not us

{"**IMPORTANT: This deal has been cold for 6+ months. Recommend a call instead of email as primary outreach, or acknowledge the long gap directly.**" if is_very_cold else ""}

## Response Format

Respond with JSON in this exact format:
{{
    "research_summary": {{
        "their_situation": "Brief summary of their context and last touchpoint",
        "problems_blockers": "What they needed or why the deal stalled",
        "call_insights": "Key points from call recordings - pain points, feature requests, objections mentioned",
        "internal_insights": "Key points from internal Slack discussions (if any)",
        "web_insights": "Relevant company news, AI initiatives, or leadership changes to leverage",
        "applicable_capabilities": "New capabilities that address their needs",
        "similar_insights": "Relevant use cases or outcomes to reference"
    }},
    "subject": "Email subject line referencing their specific problem",
    "body": "The email body (150-200 words max)",
    "talking_points": ["Point to discuss if they respond", "How to handle likely objection"],
    "flags": ["Any missing critical information or recommendations"]
}}
"""

    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    response_text = response.content[0].text
    
    try:
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0].strip()
        else:
            json_str = response_text.strip()
        
        return json.loads(json_str)
    except json.JSONDecodeError:
        return {
            "research_summary": {
                "their_situation": "Unable to parse - see raw response",
                "problems_blockers": "Unknown",
                "applicable_capabilities": "Unknown",
                "similar_insights": "None identified"
            },
            "subject": "Following up on our conversation",
            "body": response_text,
            "talking_points": [],
            "flags": ["Failed to parse structured response"]
        }


def main():
    """Main execution flow - processes a single deal for testing."""
    
    # Get deal name from command line or use default
    deal_name = sys.argv[1] if len(sys.argv) > 1 else TEST_DEAL_NAME
    
    print("🧪 TEST MODE - Single Deal Processing")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🎯 Target Deal: {deal_name}")
    print("-" * 50)
    
    # Initialize clients
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    
    if not hubspot_token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable is required")
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is required")
    
    hubspot = HubSpotClient(hubspot_token)
    claude = anthropic.Anthropic(api_key=anthropic_key)
    
    # Initialize Slack client if token is available
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    slack = SlackClient(slack_token) if slack_token else None
    if slack:
        print(f"🔗 Slack integration enabled")
    else:
        print("ℹ️ Slack integration disabled (SLACK_BOT_TOKEN not set)")
    
    # Initialize Fireflies client if API key is available
    fireflies_key = os.getenv("FIREFLIES_API_KEY")
    fireflies = FirefliesClient(fireflies_key) if fireflies_key else None
    if fireflies:
        print(f"📞 Fireflies integration enabled")
    else:
        print("ℹ️ Fireflies integration disabled (FIREFLIES_API_KEY not set)")
    
    # Initialize knowledge base client
    knowledge_base = KnowledgeBaseClient()
    if knowledge_base.initialize():
        print(f"📚 Knowledge base enabled ({knowledge_base.collection.count()} chunks indexed)")
    else:
        print("ℹ️ Knowledge base disabled (run 'python index_knowledge_base.py' to enable)")
        knowledge_base = None
    
    # Find the deal
    print(f"\n📊 Searching for deal: {deal_name}")
    
    deals = hubspot.search_deals_by_name(
        deal_name,
        properties=["dealname", "dealstage", "amount", "closedate", 
                   "notes_last_contacted", "hs_lastmodifieddate"]
    )
    
    if not deals:
        print(f"❌ No deal found matching: {deal_name}")
        return None
    
    deal = deals[0]
    deal_id = deal["id"]
    actual_deal_name = deal["properties"].get("dealname", "Unknown Deal")
    stage = deal["properties"].get("dealstage", "")
    stage_label = stage  # Use stage ID as label
    
    print(f"   ✓ Found: {actual_deal_name} (ID: {deal_id})")
    print(f"   Stage: {stage_label}")
    
    # Get company first (needed for company email check)
    print(f"\n🏢 Fetching company...")
    company = hubspot.get_associated_company(deal_id)
    company_id = company.get("id") if company else None
    company_name = company.get("properties", {}).get("name", "Unknown") if company else "Unknown"
    print(f"   Company: {company_name} (ID: {company_id})")
    
    # Get emails from deal
    print(f"\n📧 Fetching emails...")
    deal_emails = hubspot.get_deal_emails(deal_id)
    print(f"   📁 Deal emails found: {len(deal_emails)}")
    last_deal_email_date = get_last_sent_email_date(deal_emails, verbose=True)
    
    # Get emails from company (account) object
    company_emails = []
    last_company_email_date = None
    if company_id:
        company_emails = hubspot.get_company_emails(company_id)
        print(f"   🏢 Company emails found: {len(company_emails)}")
        last_company_email_date = get_last_sent_email_date(company_emails, verbose=True)
    else:
        print(f"   ⚠️ No company associated with this deal")
    
    # Deduplicate emails (same email might be associated with both deal and company)
    seen_email_ids = set()
    unique_emails = []
    for email in deal_emails + company_emails:
        email_id = email.get("id")
        if email_id and email_id not in seen_email_ids:
            seen_email_ids.add(email_id)
            unique_emails.append(email)
    
    if len(unique_emails) != len(deal_emails) + len(company_emails):
        print(f"   📊 After deduplication: {len(unique_emails)} unique emails")
    
    # Determine most recent email
    last_email_date = None
    email_source = None
    
    if last_deal_email_date and last_company_email_date:
        if last_deal_email_date >= last_company_email_date:
            last_email_date = last_deal_email_date
            email_source = "deal"
        else:
            last_email_date = last_company_email_date
            email_source = "company"
    elif last_deal_email_date:
        last_email_date = last_deal_email_date
        email_source = "deal"
    elif last_company_email_date:
        last_email_date = last_company_email_date
        email_source = "company"
    
    emails = unique_emails
    
    if last_email_date:
        days_ago = (datetime.now(last_email_date.tzinfo) - last_email_date).days
        print(f"   ✉️ Last sent email: {days_ago} days ago (from {email_source})")
    else:
        days_ago = 999
        print(f"   ❌ No sent emails found (checked deal and company)")
    
    # Get contacts and notes
    print(f"\n👤 Fetching contacts and notes...")
    contacts = hubspot.get_associated_contacts(deal_id)
    notes = hubspot.get_deal_notes(deal_id)
    
    contact = contacts[0] if contacts else {}
    contact_props = contact.get("properties", {})
    company_props = company.get("properties", {}) if company else {}
    
    print(f"   Contact: {contact_props.get('firstname', '')} {contact_props.get('lastname', '')}")
    print(f"   Company: {company_props.get('name', 'Unknown')}")
    print(f"   Notes: {len(notes)} found")
    
    # Format notes
    notes_text = "\n".join([
        n.get("properties", {}).get("hs_note_body", "")[:500] 
        for n in notes[:3]
    ]) or "No notes available"
    
    # Get last email subject
    last_email_subject = "No previous emails"
    if emails:
        for email in emails:
            if email.get("properties", {}).get("hs_email_status") == "SENT":
                last_email_subject = email.get("properties", {}).get("hs_email_subject", "No subject")
                break
    
    # Slack search
    print(f"\n💬 Searching Slack...")
    slack_context = "Slack integration not enabled."
    if slack:
        company_name = company_props.get("name", "")
        contact_name = f"{contact_props.get('firstname', '')} {contact_props.get('lastname', '')}".strip()
        
        search_terms = [t for t in [company_name, actual_deal_name, contact_name] if t and t != "Unknown"]
        
        all_slack_messages = []
        for term in search_terms[:2]:
            try:
                messages = slack.search_messages(term, SLACK_CHANNELS, limit=5)
                all_slack_messages.extend(messages)
                print(f"   Searched '{term}': {len(messages)} messages")
            except Exception as e:
                print(f"   ⚠️ Slack search error for '{term}': {e}")
        
        seen_permalinks = set()
        unique_messages = []
        for msg in all_slack_messages:
            if msg.get("permalink") not in seen_permalinks:
                seen_permalinks.add(msg.get("permalink"))
                unique_messages.append(msg)
        
        slack_context = slack.format_slack_context(unique_messages[:10])
        print(f"   Total unique messages: {len(unique_messages)}")
    
    # Fireflies search by account name and company domain
    print(f"\n📞 Searching Fireflies for call transcripts...")
    fireflies_context = "Fireflies integration not enabled."
    account_name = company_props.get("name", "")
    company_domain = extract_domain(company_props.get("website") or "")
    search_terms = [t for t in [account_name, company_domain] if t and t != "Unknown Company"]
    if fireflies and search_terms:
        seen_ids = set()
        all_transcripts = []
        for term in search_terms[:2]:
            try:
                txs = fireflies.search_transcripts_by_title(term, limit=5)
                for t in txs:
                    tid = t.get("id")
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        all_transcripts.append(t)
            except Exception as e:
                print(f"   ⚠️ Fireflies search error for '{term}': {e}")
        if all_transcripts:
            fireflies_context = fireflies.format_fireflies_context(all_transcripts[:5])
            print(f"   ✓ Found {len(all_transcripts)} call transcript(s) (searched: {', '.join(search_terms)})")
            print(f"\n   --- Fireflies Results ---")
            print(f"   {fireflies_context[:500]}..." if len(fireflies_context) > 500 else f"   {fireflies_context}")
            print(f"   --- End Fireflies Results ---\n")
        else:
            fireflies_context = "No call transcripts found for this account."
            print(f"   ℹ️ No call transcripts found")
    else:
        if not fireflies:
            print(f"   ℹ️ Fireflies not configured")
        else:
            print(f"   ℹ️ No account name or domain available for Fireflies search")
    
    # Web search
    print(f"\n🌐 Searching web for company news...")
    web_research = "Web search not performed."
    company_name_for_search = company_props.get("name", "")
    if company_name_for_search and company_name_for_search != "Unknown Company":
        try:
            web_research = search_company_news(claude, company_name_for_search)
            print(f"   Web search completed")
            print(f"\n   --- Web Research Results ---")
            print(f"   {web_research[:500]}..." if len(web_research) > 500 else f"   {web_research}")
            print(f"   --- End Web Research ---\n")
        except Exception as e:
            print(f"   ⚠️ Web search error: {e}")
            web_research = f"Web search failed: {str(e)}"
    
    # Build deal context
    deal_context = {
        "deal_id": deal_id,
        "deal_name": actual_deal_name,
        "stage": stage_label,
        "days_since_contact": days_ago if days_ago < 999 else "30+",
        "contact_name": f"{contact_props.get('firstname', '')} {contact_props.get('lastname', '')}".strip() or "Unknown",
        "contact_email": contact_props.get("email", "No email"),
        "contact_title": contact_props.get("jobtitle", "Unknown"),
        "company_name": company_props.get("name", "Unknown Company"),
        "company_industry": company_props.get("industry", "Unknown"),
        "company_size": company_props.get("numberofemployees", "Unknown"),
        "notes": notes_text,
        "last_email_subject": last_email_subject,
        "slack_context": slack_context,
        "fireflies_context": fireflies_context,
        "web_research": web_research,
    }
    
    # Knowledge base context (RAG)
    print(f"\n📚 Searching knowledge base...")
    kb_context = "Knowledge base not available."
    if knowledge_base:
        kb_context = knowledge_base.get_context_for_deal(deal_context)
        if kb_context and "No relevant" not in kb_context and "not available" not in kb_context:
            print(f"   ✓ Found relevant knowledge base content")
            print(f"\n   --- Knowledge Base Results ---")
            print(f"   {kb_context[:500]}..." if len(kb_context) > 500 else f"   {kb_context}")
            print(f"   --- End Knowledge Base Results ---\n")
        else:
            print(f"   ℹ️ No specific knowledge base matches")
    else:
        print(f"   ℹ️ Knowledge base not configured")
    
    deal_context["knowledge_base_context"] = kb_context
    
    # Generate email
    print(f"\n✍️ Generating follow-up email...")
    
    try:
        email_content = generate_followup_email(claude, deal_context)
        
        print(f"\n" + "=" * 60)
        print("📧 GENERATED EMAIL")
        print("=" * 60)
        
        print(f"\n📋 RESEARCH SUMMARY:")
        research = email_content.get("research_summary", {})
        for key, value in research.items():
            print(f"   {key}: {value}")
        
        print(f"\n📝 SUBJECT: {email_content.get('subject', 'N/A')}")
        print(f"\n📄 BODY:\n{email_content.get('body', 'N/A')}")
        
        if email_content.get("talking_points"):
            print(f"\n💡 TALKING POINTS:")
            for point in email_content["talking_points"]:
                print(f"   • {point}")
        
        if email_content.get("flags"):
            print(f"\n⚠️ FLAGS:")
            for flag in email_content["flags"]:
                print(f"   • {flag}")
        
        print(f"\n" + "=" * 60)
        print("✅ Test completed successfully!")
        
        return {**deal_context, **email_content}
        
    except Exception as e:
        print(f"❌ Error generating email: {e}")
        import traceback
        traceback.print_exc()
        return None


if __name__ == "__main__":
    main()

