"""Reviewed browser MCP package identity; never float production startup."""

PLAYWRIGHT_MCP_PACKAGE = '@playwright/mcp@0.0.80'

# The browser automation surface is separate from granting code execution in
# the MCP server process or uploading server-side files. Keep this exact deny
# list server-owned; neither MCP descriptions nor model arguments can relax it.
BROWSER_FORBIDDEN_MCP_TOOLS = frozenset({
    'browser_run_code_unsafe',
    'browser_evaluate',
    'browser_file_upload',
    'browser_drop',
    'browser_network_request',
})
