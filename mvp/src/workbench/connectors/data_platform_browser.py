"""Read-only CDP observation for Data Platform object pages."""


async def browser_has_exact_page(cdp_url: str, expected_url: str) -> bool:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        return any(
            page.url.rstrip("/") == expected_url.rstrip("/")
            for context in browser.contexts
            for page in context.pages
        )
