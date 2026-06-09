# Tools/builtin/browser.py
import asyncio
import base64
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal

from nexus_agent.Tools.base import Tool, ToolInvokation, ToolKind, ToolResult
from nexus_agent.utils.path import resolve_path

try:
    from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class BrowserParams(BaseModel):
    action: Literal[
        "navigate",
        "screenshot",
        "click",
        "type",
        "extract_text",
        "extract_links",
        "wait_for",
        "scroll",
        "execute_js",
        "fill_form",
        "get_html",
        "close"
    ] = Field(
        ...,
        description="The browser action to perform"
    )
    
    # Navigate
    url: str | None = Field(
        None,
        description="URL to navigate to (required for navigate action)"
    )
    
    # Screenshot
    screenshot_path: str | None = Field(
        None,
        description="Path to save screenshot (defaults to screenshots/screenshot_<timestamp>.png)"
    )
    full_page: bool = Field(
        True,
        description="Capture full page screenshot (default: True)"
    )
    
    # Click/Type/Fill
    selector: str | None = Field(
        None,
        description="CSS selector for element to interact with"
    )
    text: str | None = Field(
        None,
        description="Text to type or fill"
    )
    
    # Wait for
    wait_type: Literal["selector", "timeout", "load"] | None = Field(
        "load",
        description="What to wait for: selector, timeout (ms), or load state"
    )
    wait_value: str | int | None = Field(
        None,
        description="Selector string or timeout milliseconds"
    )
    
    # Scroll
    scroll_direction: Literal["down", "up", "top", "bottom"] | None = Field(
        "down",
        description="Scroll direction"
    )
    scroll_amount: int | None = Field(
        500,
        description="Pixels to scroll (for up/down)"
    )
    
    # Execute JavaScript
    javascript: str | None = Field(
        None,
        description="JavaScript code to execute in browser context"
    )
    
    # Form filling
    form_data: dict[str, str] | None = Field(
        None,
        description="Dictionary of selector: value pairs for form filling"
    )
    
    # General options
    timeout: int = Field(
        30000,
        ge=1000,
        le=120000,
        description="Operation timeout in milliseconds (default: 30000)"
    )
    headless: bool = Field(
        True,
        description="Run browser in headless mode (default: True)"
    )
    wait_after_action: int = Field(
        1000,
        ge=0,
        le=10000,
        description="Milliseconds to wait after action completes (default: 1000)"
    )


class BrowserTool(Tool):
    name = "browser"
    description = (
        "Automate browser interactions using Playwright. "
        "Can navigate to URLs, take screenshots, click elements, fill forms, extract text/links, "
        "execute JavaScript, and more. Useful for web scraping, testing, and automation tasks. "
        "Maintains a persistent browser session during execution."
    )
    kind = ToolKind.SHELL
    schema = BrowserParams
    
    def __init__(self, config):
        super().__init__(config)
        self._playwright = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._context = None
    
    async def execute(self, invocation: ToolInvokation) -> ToolResult:
        if not PLAYWRIGHT_AVAILABLE:
            return ToolResult.error_result(
                "Playwright is not installed. Install with: pip install playwright && playwright install chromium"
            )
        
        params = BrowserParams(**invocation.params)
        
        try:
            # Initialize browser if needed
            if self._browser is None or not self._browser.is_connected():
                await self._initialize_browser(params.headless)
            
            # Ensure we have a page
            if self._page is None:
                self._page = await self._context.new_page()
            
            # Execute the requested action
            result = await self._execute_action(params, invocation.cwd)
            
            # Wait after action if specified
            if params.wait_after_action > 0:
                await asyncio.sleep(params.wait_after_action / 1000)
            
            return result
        
        except PlaywrightTimeoutError:
            return ToolResult.error_result(
                f"Browser action '{params.action}' timed out after {params.timeout}ms",
                metadata={"action": params.action, "timeout": params.timeout}
            )
        except Exception as e:
            return ToolResult.error_result(
                f"Browser action failed: {str(e)}",
                metadata={"action": params.action}
            )
    
    async def _initialize_browser(self, headless: bool):
        """Initialize Playwright browser instance."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        self._context = await self._browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
    
    async def _execute_action(self, params: BrowserParams, cwd: Path) -> ToolResult:
        """Execute the specific browser action."""
        
        if params.action == "navigate":
            return await self._navigate(params)
        
        elif params.action == "screenshot":
            return await self._screenshot(params, cwd)
        
        elif params.action == "click":
            return await self._click(params)
        
        elif params.action == "type":
            return await self._type(params)
        
        elif params.action == "extract_text":
            return await self._extract_text(params)
        
        elif params.action == "extract_links":
            return await self._extract_links(params)
        
        elif params.action == "wait_for":
            return await self._wait_for(params)
        
        elif params.action == "scroll":
            return await self._scroll(params)
        
        elif params.action == "execute_js":
            return await self._execute_js(params)
        
        elif params.action == "fill_form":
            return await self._fill_form(params)
        
        elif params.action == "get_html":
            return await self._get_html(params)
        
        elif params.action == "close":
            return await self._close()
        
        return ToolResult.error_result(f"Unknown action: {params.action}")
    
    async def _navigate(self, params: BrowserParams) -> ToolResult:
        """Navigate to a URL."""
        if not params.url:
            return ToolResult.error_result("url is required for navigate action")
        
        response = await self._page.goto(params.url, timeout=params.timeout)
        
        return ToolResult.success_result(
            f"Navigated to {params.url}",
            metadata={
                "url": params.url,
                "status": response.status if response else None,
                "title": await self._page.title()
            }
        )
    
    async def _screenshot(self, params: BrowserParams, cwd: Path) -> ToolResult:
        """Take a screenshot of the current page."""
        import datetime
        
        if params.screenshot_path:
            screenshot_path = resolve_path(cwd, params.screenshot_path)
        else:
            screenshots_dir = cwd / "screenshots"
            screenshots_dir.mkdir(exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = screenshots_dir / f"screenshot_{timestamp}.png"
        
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        
        await self._page.screenshot(
            path=str(screenshot_path),
            full_page=params.full_page,
            timeout=params.timeout
        )
        
        # Get file size
        file_size = screenshot_path.stat().st_size
        size_kb = file_size / 1024
        
        return ToolResult.success_result(
            f"Screenshot saved to {screenshot_path}",
            metadata={
                "path": str(screenshot_path),
                "size_kb": round(size_kb, 2),
                "full_page": params.full_page,
                "url": self._page.url
            }
        )
    
    async def _click(self, params: BrowserParams) -> ToolResult:
        """Click an element."""
        if not params.selector:
            return ToolResult.error_result("selector is required for click action")
        
        await self._page.click(params.selector, timeout=params.timeout)
        
        return ToolResult.success_result(
            f"Clicked element: {params.selector}",
            metadata={"selector": params.selector, "url": self._page.url}
        )
    
    async def _type(self, params: BrowserParams) -> ToolResult:
        """Type text into an element."""
        if not params.selector:
            return ToolResult.error_result("selector is required for type action")
        if not params.text:
            return ToolResult.error_result("text is required for type action")
        
        await self._page.type(params.selector, params.text, timeout=params.timeout)
        
        return ToolResult.success_result(
            f"Typed text into {params.selector}",
            metadata={
                "selector": params.selector,
                "text_length": len(params.text),
                "url": self._page.url
            }
        )
    
    async def _extract_text(self, params: BrowserParams) -> ToolResult:
        """Extract text content from page or specific element."""
        if params.selector:
            text = await self._page.text_content(params.selector, timeout=params.timeout)
        else:
            text = await self._page.evaluate("document.body.innerText")
        
        return ToolResult.success_result(
            text or "",
            metadata={
                "selector": params.selector or "body",
                "length": len(text) if text else 0,
                "url": self._page.url
            }
        )
    
    async def _extract_links(self, params: BrowserParams) -> ToolResult:
        """Extract all links from the page."""
        links = await self._page.evaluate("""
            () => {
                return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    text: a.innerText.trim(),
                    href: a.href,
                    title: a.title
                }));
            }
        """)
        
        formatted_links = []
        for link in links:
            formatted_links.append(
                f"Text: {link['text'][:100]}\n"
                f"URL: {link['href']}\n"
                f"Title: {link.get('title', 'N/A')}"
            )
        
        output = "\n" + "="*80 + "\n"
        output += "\n---\n".join(formatted_links)
        
        return ToolResult.success_result(
            output,
            metadata={
                "links_count": len(links),
                "url": self._page.url
            }
        )
    
    async def _wait_for(self, params: BrowserParams) -> ToolResult:
        """Wait for a condition."""
        if params.wait_type == "selector":
            if not params.wait_value:
                return ToolResult.error_result("wait_value (selector) is required")
            await self._page.wait_for_selector(str(params.wait_value), timeout=params.timeout)
            return ToolResult.success_result(f"Element appeared: {params.wait_value}")
        
        elif params.wait_type == "timeout":
            ms = int(params.wait_value) if params.wait_value else 1000
            await asyncio.sleep(ms / 1000)
            return ToolResult.success_result(f"Waited for {ms}ms")
        
        elif params.wait_type == "load":
            await self._page.wait_for_load_state("load", timeout=params.timeout)
            return ToolResult.success_result("Page loaded")
        
        return ToolResult.error_result(f"Unknown wait_type: {params.wait_type}")
    
    async def _scroll(self, params: BrowserParams) -> ToolResult:
        """Scroll the page."""
        if params.scroll_direction == "down":
            await self._page.evaluate(f"window.scrollBy(0, {params.scroll_amount})")
        elif params.scroll_direction == "up":
            await self._page.evaluate(f"window.scrollBy(0, -{params.scroll_amount})")
        elif params.scroll_direction == "top":
            await self._page.evaluate("window.scrollTo(0, 0)")
        elif params.scroll_direction == "bottom":
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        
        return ToolResult.success_result(
            f"Scrolled {params.scroll_direction}",
            metadata={"direction": params.scroll_direction}
        )
    
    async def _execute_js(self, params: BrowserParams) -> ToolResult:
        """Execute JavaScript in browser context."""
        if not params.javascript:
            return ToolResult.error_result("javascript is required for execute_js action")
        
        result = await self._page.evaluate(params.javascript)
        
        return ToolResult.success_result(
            str(result) if result is not None else "JavaScript executed successfully",
            metadata={
                "javascript": params.javascript[:100] + "..." if len(params.javascript) > 100 else params.javascript,
                "result_type": type(result).__name__
            }
        )
    
    async def _fill_form(self, params: BrowserParams) -> ToolResult:
        """Fill a form with multiple fields."""
        if not params.form_data:
            return ToolResult.error_result("form_data is required for fill_form action")
        
        filled = []
        for selector, value in params.form_data.items():
            await self._page.fill(selector, value, timeout=params.timeout)
            filled.append(f"{selector}: {value[:50]}...")
        
        return ToolResult.success_result(
            f"Filled {len(filled)} form fields",
            metadata={
                "fields_filled": len(filled),
                "selectors": list(params.form_data.keys())
            }
        )
    
    async def _get_html(self, params: BrowserParams) -> ToolResult:
        """Get HTML content of page or element."""
        if params.selector:
            html = await self._page.inner_html(params.selector, timeout=params.timeout)
        else:
            html = await self._page.content()
        
        # Truncate if too large
        max_length = 50000
        truncated = False
        if len(html) > max_length:
            html = html[:max_length] + "\n...[HTML truncated]"
            truncated = True
        
        return ToolResult.success_result(
            html,
            truncated=truncated,
            metadata={
                "selector": params.selector or "full page",
                "length": len(html),
                "truncated": truncated
            }
        )
    
    async def _close(self) -> ToolResult:
        """Close the browser."""
        if self._page:
            await self._page.close()
            self._page = None
        
        if self._context:
            await self._context.close()
            self._context = None
        
        if self._browser:
            await self._browser.close()
            self._browser = None
        
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        
        return ToolResult.success_result("Browser closed")
    
    async def cleanup(self):
        """Cleanup method to ensure browser is closed."""
        try:
            await self._close()
        except:
            pass