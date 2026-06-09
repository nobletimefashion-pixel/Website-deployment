from ddgs import DDGS
from pydantic import BaseModel, Field
from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult


class WebSearchParams(BaseModel):
    query: str = Field(
        ..., 
        description="Search query to find recent and reliable information on the web"
    )
    max_results: int = Field(
        10, 
        ge=1, 
        le=20, 
        description="Maximum number of search results to return (default: 10)"
    )

class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web for recent and reliable information on a topic. "
        "Returns titles, URLs, and content snippets from top results. "
        "Use this when you need current information or real-time data."
    )
    kind = ToolKind.NETWORK
    schema = WebSearchParams
    
    
    async def execute(self, invocation:ToolInvokation) -> ToolResult:
        params = WebSearchParams(**invocation.params)
        
        try:
            results = DDGS().text(params.query,region="us-en",safesearch="off",timelimit="y",page=1,backend="auto",)
        except Exception as e:
            return ToolResult.error_result(f"Search failed: {e}")
        
        if not results:
            return ToolResult.success_result(f"No result found for: {params.query}", metadata={
                'results': 0
            })
        
        output_lines = [f"Search results for: {params.query}"]
        
        for i, result in enumerate(results, start=1):
            output_lines.append(f"{i}, Title: {result['title']}")
            output_lines.append(f"     URL: {result['href']}")
            if result.get('body'):
                output_lines.append(f" Content: {result['body']}")
                
            output_lines.append("")
            
        return ToolResult.success_result(
            '\n'.join(output_lines),
            metadata={
                'results': len(results)
            }
        )
