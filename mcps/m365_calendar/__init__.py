"""M365 Calendar MCP server — delegate-access calendar provider for Microsoft Graph.

Exposes tools for reading events and managing tentative calendar entries in a
delegated M365 calendar. Explicitly does NOT expose a confirm/book tool —
enforcing the propose-only trust boundary at the tool level. The LLM cannot
confirm events because there is no tool to call.

Scopes required: Calendars.Read.Shared, Calendars.ReadWrite.Shared, User.Read, offline_access
"""
