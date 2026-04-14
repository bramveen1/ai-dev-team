# Outlook Calendar + Mail (Microsoft Graph API)

## Account
- **Account:** bram@veenhof.nu
- **Tenant ID:** dc4f4233-1df6-426d-8f6b-5805c5365241
- **Client ID:** 0c54a35a-4763-4fa1-9ba7-670bd1bd7426
- **Scopes:** Calendars.ReadWrite, Mail.Read, Mail.ReadWrite, Mail.Send, Tasks.ReadWrite, profile, openid, email
- **Tokens:** memory/systems/outlook-tokens.json (access_token + refresh_token)
- **Re-authenticated:** 2026-03-23 (device code flow, expanded scopes)

## Auth
- **Token refresh:** POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token with grant_type=refresh_token
- Public client flow — no client_secret needed
- For new scope changes: use device code flow (POST /devicecode → user visits login.microsoft.com/device)

## API Endpoints
- **Calendar view:** GET https://graph.microsoft.com/v1.0/me/calendarView?startDateTime=...&endDateTime=...
- **Create event:** POST https://graph.microsoft.com/v1.0/me/events
- **Unread mail:** GET https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$filter=isRead eq false
- **Send mail:** POST https://graph.microsoft.com/v1.0/me/sendMail
- **To Do lists:** GET https://graph.microsoft.com/v1.0/me/todo/lists
- **To Do tasks:** GET https://graph.microsoft.com/v1.0/me/todo/lists/{listId}/tasks