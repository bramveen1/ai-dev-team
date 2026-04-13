# Outlook Integration

## Overview
The team uses Microsoft Outlook for calendar management and email notifications.

## Key Endpoints
- Calendar API: Used for scheduling stand-ups and retrospectives
- Email API: Used for sending deployment notifications

## Authentication
- OAuth 2.0 with Microsoft identity platform
- Tokens stored securely, refreshed automatically

## Usage Notes
- Rate limit: 10,000 requests per 10 minutes per app
- Batch requests supported for bulk operations
