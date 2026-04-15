"""M365 Mail MCP server — delegate-access email provider for Microsoft Graph.

Exposes tools for reading messages and managing drafts in a delegated M365 mailbox.
Explicitly does NOT expose a send tool — enforcing the no-send trust boundary at the
tool level. The LLM cannot send mail because there is no tool to call.

Scopes required: Mail.Read.Shared, Mail.ReadWrite.Shared, User.Read, offline_access
"""
