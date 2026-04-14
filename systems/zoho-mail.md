# Zoho Mail

## Direct Access
- **Address:** lisa@pathtohired.com
- **Provider:** Zoho Mail EU
- **Login:** https://mail.zoho.eu
- **Password:** 4@!dCz4JxX4N77hMkJDi

## Maton Gateway (for API access)
- **API Key:** 0jY1m3ZjJlnCOkcbKVJIffjfuVY2t23HCHsCdSfaaAQvEhiRu1wsEtfgQXcX0sG3rj3-lY42_1emRsCETkYMoRMED5JvNAxWwSw
- **Account ID:** 8329259000000002002
- **Inbox folder ID:** 8329259000000002014
- **Gateway base URL:** https://gateway.maton.ai/zoho-mail/
- **Connections dashboard:** https://ctrl.maton.ai
- **Auth header:** `Authorization: Bearer $MATON_API_KEY`

## Common API Calls
```bash
# List inbox
GET /zoho-mail/api/accounts/8329259000000002002/messages/view?folderId=8329259000000002014&limit=10

# Search
GET /zoho-mail/api/accounts/8329259000000002002/messages/search?searchKey=<query>

# Get message content
GET /zoho-mail/api/accounts/8329259000000002002/folders/8329259000000002014/messages/<messageId>/content
```