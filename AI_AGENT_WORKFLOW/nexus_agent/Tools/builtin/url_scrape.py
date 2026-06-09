# Tools/builtin/scrape_url.py
from pydantic import BaseModel, Field
import requests
from bs4 import BeautifulSoup

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.utils.text import count_token, truncate_text

class ScrapeUrlParams(BaseModel):
    url: str = Field(
        ..., 
        description="The URL to scrape and extract text content from"
    )
    max_chars: int = Field(
        3000,
        ge=500,
        le=50000,
        description="Maximum number of characters to return (default: 3000)"
    )
    timeout: int = Field(
        8,
        ge=1,
        le=30,
        description="Request timeout in seconds (default: 8)"
    )

class ScrapeUrlTool(Tool):
    name = "scrape_url"
    description = (
        "Scrape and return clean text content from a given URL for deeper reading. "
        "Automatically removes scripts, navigation, styles, and footer elements. "
        "Use this to read full articles or web pages after finding them via web search."
    )
    kind = ToolKind.READ
    schema = ScrapeUrlParams
    MAX_TOKEN_COUNT = 100000
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        params = ScrapeUrlParams(**invocation.params)
        
        try:
            # Make HTTP request
            response = requests.get(
                params.url,
                timeout=params.timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; CustomAgent/1.0)"}
            )
            response.raise_for_status()
            
            # Parse HTML
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Remove unwanted elements
            for tag in soup.find_all(["script", "nav", "style", "footer", "header", "aside"]):
                tag.decompose()
            
            # Extract clean text
            text = soup.get_text(separator=" ", strip=True)
            
            # Limit characters
            text = text[:params.max_chars]
            
            # Check token count and truncate if needed
            token_count = count_token(text)
            truncated = False
            if token_count > self.MAX_TOKEN_COUNT:
                text = truncate_text(
                    text, 
                    self.MAX_TOKEN_COUNT, 
                    suffix="\n...[content truncated due to length]"
                )
                truncated = True
            
            return ToolResult.success_result(
                output=text,
                truncated=truncated,
                metadata={
                    "url": params.url,
                    "content_length": len(text),
                    "token_count": token_count,
                    "truncated": truncated,
                    "status_code": response.status_code
                }
            )
            
        except requests.Timeout:
            return ToolResult.error_result(
                f"Request timed out after {params.timeout} seconds",
                metadata={"url": params.url}
            )
        except requests.HTTPError as e:
            return ToolResult.error_result(
                f"HTTP error occurred: {e.response.status_code}",
                metadata={"url": params.url, "status_code": e.response.status_code}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Could not scrape URL: {str(e)}",
                metadata={"url": params.url}
            )