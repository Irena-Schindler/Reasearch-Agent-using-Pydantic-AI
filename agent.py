import os
import asyncio
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from typing import List, Optional
from pydantic_ai import Agent, RunContext
from dotenv import load_dotenv
import gradio as gr
from duckduckgo_search import DDGS

# Load variables from `.env` into the current process (e.g., OPENAI_API_KEY).
load_dotenv()

# Use OpenAI GPT-5 Mini through PydanticAI's model naming format.
model_id = 'openai:gpt-5-mini'


def get_agent_output(run_result):
    """Compatibility helper for different PydanticAI result attribute names."""
    # Try common public attributes first.
    for attr in ("output", "data", "result"):
        try:
            value = getattr(run_result, attr)
            if value is not None:
                return value
        except Exception:
            pass

    # Try internal dict-backed attributes used by some versions.
    try:
        data = vars(run_result)
    except Exception:
        data = {}
    for key in ("output", "data", "_output", "_data", "result", "_result"):
        if key in data and data[key] is not None:
            return data[key]

    raise AttributeError(
        f"Unsupported AgentRunResult shape: {type(run_result).__name__}; "
        f"available keys={list(data.keys())}"
    )


def ensure_swot_angle_if_applicable(plan: "ResearchPlan") -> None:
    """Ensure SWOT is always included for company/ticker-style research."""
    # Tickers should always include SWOT as one of the research angles.
    needs_swot = plan.is_ticker
    if not needs_swot:
        # For non-ticker prompts, check if it still looks finance/company related.
        combined = f"{plan.topic} {plan.context}".lower()
        keywords = ("company", "stock", "equity", "market cap", "earnings", "investor")
        needs_swot = any(k in combined for k in keywords)

    if not needs_swot:
        return

    # If SWOT is missing, add it while keeping total angles within the 3-4 target.
    if not any("swot" in angle.lower() for angle in plan.angles):
        if len(plan.angles) >= 4:
            plan.angles[-1] = "SWOT analysis"
        else:
            plan.angles.append("SWOT analysis")

# --- Data Models ---

class ResearchPlan(BaseModel):
    """Plan for the deep research."""
    # True when the input is likely a stock ticker.
    is_ticker: bool = Field(description="Whether the input is a stock ticker or a general query.")
    # Resolved subject (e.g., "Volkswagen").
    topic: str = Field(description="The main topic or company name resolved from the input.")
    # Extra context (sector, industry, domain).
    context: str = Field(description="Context about the topic (e.g., industry, sector).")
    # 3-4 unique research angles to explore in depth.
    angles: List[str] = Field(description="List of 3-4 distinct research angles/keywords to explore.")

class ResearchResult(BaseModel):
    """Raw result from a search query."""
    # Search result title shown by DuckDuckGo.
    title: str
    # Source URL from the search result.
    url: str
    # Short snippet/body returned by search.
    snippet: str
    # Optional fetched page text for deeper extraction.
    content: Optional[str] = None

class AngleData(BaseModel):
    """Extracted data for a specific research angle."""
    # Name of the angle being analyzed (e.g., SWOT analysis).
    angle: str
    # Important facts and numeric takeaways.
    key_facts: List[str] = Field(description="Key facts and numbers extracted.")
    # Claims found in sources for this angle.
    claims: List[str] = Field(description="Specific claims made in the sources.")
    # Supporting source URLs for traceability.
    citations: List[str] = Field(description="URLs of sources supporting the facts/claims.")

class FinalReport(BaseModel):
    """The final synthesized report."""
    # Final report returned as Markdown text.
    markdown_content: str = Field(description="The full report in Markdown format.")

# --- Tools ---

async def search_duckduckgo(query: str, max_results: int = 5) -> List[ResearchResult]:
    """Searches DuckDuckGo and returns a list of results."""
    # Store normalized search results.
    results = []
    try:
        # DDGS is synchronous, so run it in a worker thread to keep async flow responsive.
        def run_search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        
        search_results = await asyncio.to_thread(run_search)
        
        # Convert raw dict results into typed `ResearchResult` objects.
        for r in search_results:
            results.append(ResearchResult(
                title=r.get('title', ''),
                url=r.get('href', ''),
                snippet=r.get('body', '')
            ))
    except Exception as e:
        print(f"Error searching DuckDuckGo for '{query}': {e}")
    return results

async def fetch_page_content(url: str) -> Optional[str]:
    """Fetches and cleans the text content of a webpage."""
    try:
        # Use async HTTP client for non-blocking page fetches.
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Remove noisy page elements so extracted text is cleaner.
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()
            
            # Flatten cleaned HTML to plain text.
            text = soup.get_text(separator=' ', strip=True)
            # Cap content length so prompts remain manageable.
            return text[:10000] 
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

# --- Agents ---

# 1. Planning Agent
planning_agent = Agent(
    model_id,
    output_type=ResearchPlan,
    instructions=(
        "You are a Research Planner. Your goal is to analyze a user request and create a structured research plan. "
        "Determine if the input is a stock ticker or a general query. "
        "If it's a ticker, resolve it to the company name and provide context (industry, etc.). "
        "Generate 3-4 distinct, non-overlapping research angles (keywords) to investigate. "
        "For stocks, include angles like 'SWOT analysis', 'recent performance', 'market positioning', 'guidance'. "
        "For general queries, break it down into key sub-topics."
    )
)

# 2. Extraction Agent (extracts facts/claims + citations per angle).
extraction_agent = Agent(
    model_id,
    output_type=AngleData,
    instructions=(
        "You are a Research Analyst. You will be given a specific research angle and a list of search results (some with full content). "
        "Your goal is to extract key facts, numbers, and claims relevant to the angle. "
        "Track the source URL for each fact/claim. "
        "Focus on primary sources (financials, official reports) and reputable news."
    )
)

# 3. Synthesis Agent (combines all angle outputs into one report).
synthesis_agent = Agent(
    model_id,
    output_type=FinalReport,
    instructions=(
        "You are a Senior Research Editor. Your goal is to synthesize collected data into a comprehensive, professional report. "
        "You will receive data for several research angles. "
        "Produce a single detailed report in Markdown. "
        "Structure: "
        "1. Executive Summary "
        "2. Sections for each research angle (with key findings) "
        "3. Evidence/Citations (bullet points with URL + Title) "
        "4. Risks/Uncertainties & Conflicting Info "
        "5. 'What to Watch Next' list. "
        "Ensure the tone is objective and professional."
    )
)

# --- Orchestration ---

async def deep_research(user_input: str, progress=gr.Progress()) -> str:
    """Orchestrates the deep research process."""
    
    # Step 1: Build a structured research plan from the user's input.
    progress(0.1, desc="Planning research...")
    try:
        plan_run = await planning_agent.run(user_input)
        # Extract agent output with compatibility helper.
        plan = get_agent_output(plan_run)
        # Enforce SWOT for ticker/company-style research.
        ensure_swot_angle_if_applicable(plan)
        print(f"Plan generated: {plan}")
    except Exception as e:
        return f"Error generating plan: {e}"

    # Step 2: Run each research angle in parallel.
    progress(0.3, desc="Conducting research...")
    
    async def process_angle(angle: str) -> AngleData:
        # Build a focused query by combining topic + angle.
        query = f"{plan.topic} {angle}"
        # Run DuckDuckGo search for this angle.
        results = await search_duckduckgo(query)
        
        # Fetch top pages for deeper context (limited for speed).
        fetch_tasks = [fetch_page_content(r.url) for r in results[:2]]
        contents = await asyncio.gather(*fetch_tasks)
        
        # Attach fetched page text back to result objects.
        for i, content in enumerate(contents):
            if content:
                results[i].content = content
        
        # Prepare extraction input containing metadata + optional page text.
        angle_prompt = f"Topic: {plan.topic}\nAngle: {angle}\n\nSearch Results:\n"
        for r in results:
            angle_prompt += f"- Title: {r.title}\n  URL: {r.url}\n  Snippet: {r.snippet}\n"
            if r.content:
                # Include trimmed content to control prompt size.
                angle_prompt += f"  Content: {r.content[:2000]}...\n"
            angle_prompt += "\n"
            
        try:
            # Ask extraction agent to produce structured angle data.
            extraction_run = await extraction_agent.run(angle_prompt)
            return get_agent_output(extraction_run)
        except Exception as e:
            print(f"Error extracting for angle '{angle}': {e}")
            # Return safe fallback so one failed angle does not break full report.
            return AngleData(angle=angle, key_facts=[], claims=[], citations=[])

    # Launch one task per angle and wait for all to finish.
    angle_tasks = [process_angle(angle) for angle in plan.angles]
    angle_data_list = await asyncio.gather(*angle_tasks)
    
    # Step 3: Synthesize all angle outputs into one final Markdown report.
    progress(0.8, desc="Synthesizing report...")
    
    # Build synthesis context that includes all extracted findings.
    report_prompt = f"Topic: {plan.topic}\nContext: {plan.context}\n\nCollected Data:\n"
    for data in angle_data_list:
        report_prompt += f"\nAngle: {data.angle}\n"
        report_prompt += "Facts:\n" + "\n".join([f"- {f}" for f in data.key_facts]) + "\n"
        report_prompt += "Claims:\n" + "\n".join([f"- {c}" for c in data.claims]) + "\n"
        report_prompt += "Citations:\n" + "\n".join([f"- {c}" for c in data.citations]) + "\n"
        
    try:
        report_run = await synthesis_agent.run(report_prompt)
        # Extract report object in a version-safe way.
        final_report_obj = get_agent_output(report_run)
        # Return markdown content for display in Gradio chat.
        final_report = final_report_obj.markdown_content
        return final_report
    except Exception as e:
        return f"Error synthesizing report: {e}"

# --- Gradio Interface ---

async def chat(message, history):
    # Gradio calls this for each user message.
    return await deep_research(message)

demo = gr.ChatInterface(
    fn=chat,
    title="Deep Research Agent",
    description="Enter a stock ticker (e.g., VLKAF) or a general query. The agent will plan, research, and generate a detailed report.",
    examples=["VLKAF", "Future of quantum computing", "TSLA SWOT analysis"],
)

if __name__ == "__main__":
    # Start local Gradio server.
    demo.launch()

